"""US-043: deterministic 10k-chunk Wikipedia corpus + ACL seed.

Powers the scale benchmark — `evals/permissions_scale/runner.py` — that
charts pre-filter recall@5 against HNSW `ef_search` at three permission
selectivities (50% / 10% / 1%). Owned by a *separate* sentinel user from
`db_seed.corpus_seed.CORPUS_USER_ID` so the two seed sets coexist in the
same database without colliding (the retrieval correctness eval and the
permissions scale eval each pin their own user).

Determinism stack:

- HuggingFace dataset is loaded with a pinned `revision` from
  `evals/permissions_scale/scale_gold.yaml::corpus.hf_revision`.
- Text is concatenated by the dataset's row order and chunked via the
  production `backend.chunking.chunk_text` (default 500/50). The first
  `corpus.total_chunks` chunks are kept; documents are slabs of
  `corpus.chunks_per_document` chunks each.
- `stable_id = wikipedia-{doc_idx:04d}:{chunk_index:04d}`.
- `document.id = uuid5(NAMESPACE_URL, "agentic-rag/wikipedia/wikipedia-{NNNN}")`.
- `chunks.id    = uuid5(NAMESPACE_URL, "agentic-rag/wikipedia/{stable_id}")`.
- ACL rows: for each viewer, the visible-chunks set is the K chunks
  with the lowest `blake2b(salt || viewer_id || global_chunk_index)`,
  where K = `viewers[i].visible_chunks` from the YAML. Hash-and-take-K
  gives *exactly* K visible chunks per viewer, deterministic across
  runs, and statistically independent across viewers.

Idempotency: identifies prior wikipedia-seeded rows by
`documents.user_id = WIKIPEDIA_USER_ID AND metadata->>'wikipedia_seed' =
'true'` and deletes them before re-inserting (chunks + chunk_acl cascade
via the FKs). Running this twice in a row is a no-op modulo HuggingFace
dataset content drift (pinned by revision).

Run:
    python -m db_seed.wikipedia_seed

Reads:
    CORPUS_SEED_DATABASE_URL  (or DATABASE_URL fallback) — writable Postgres
    OPENAI_API_KEY            — required for embedding generation
    WIKITEXT_REVISION         — optional override of the YAML's hf_revision
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import struct
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import asyncpg
import yaml
from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from chunking import chunk_text  # noqa: E402
from embeddings import embed_texts, to_pgvector  # noqa: E402

log = logging.getLogger("agentic_rag.db_seed.wikipedia")

CONFIG_PATH = ROOT / "evals" / "permissions_scale" / "scale_gold.yaml"

# Sentinel user that owns the wikipedia corpus. Distinct from
# CORPUS_USER_ID (00...001) so the retrieval correctness eval and this
# scale eval can populate the same DB without their data colliding.
WIKIPEDIA_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000043")
WIKIPEDIA_USER_EMAIL = "wikipedia-seed@local.test"


def stable_id(doc_idx: int, chunk_in_doc: int) -> str:
    return f"wikipedia-{doc_idx:04d}:{chunk_in_doc:04d}"


def document_uuid(doc_idx: int) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"agentic-rag/wikipedia/wikipedia-{doc_idx:04d}")


def chunk_uuid(doc_idx: int, chunk_in_doc: int) -> uuid.UUID:
    sid = stable_id(doc_idx, chunk_in_doc)
    return uuid.uuid5(uuid.NAMESPACE_URL, f"agentic-rag/wikipedia/{sid}")


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    with path.open() as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise RuntimeError(f"{path} did not parse to a dict")
    for key in ("corpus", "viewers", "seed_salt"):
        if key not in cfg:
            raise RuntimeError(f"{path} missing required key: {key}")
    return cfg


def fetch_wikitext(
    repo_id: str,
    config: str,
    split: str,
    revision: str,
    target_chunks: int,
    chunk_size_estimate: int = 500,
) -> tuple[str, str]:
    """Pull enough rows from the dataset to cover `target_chunks` chunks.

    Returns (concatenated_text, resolved_revision_sha). Imports `datasets`
    lazily so seeders that don't need wikipedia (e.g. corpus_seed) keep a
    minimal dep footprint.

    Heuristic: each wikitext row is ~1 short paragraph ≈ 50 tokens. To get
    ~`target_chunks` chunks at `chunk_size_estimate`-token chunks, we need
    roughly `target_chunks * chunk_size_estimate / 50` rows; we pull 2× that
    as a safety margin and the chunker truncates the tail.
    """
    try:
        from datasets import load_dataset  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "`datasets` package is required: pip install -r "
            "evals/permissions_scale/requirements.txt"
        ) from e

    rows_needed = max(50_000, target_chunks * chunk_size_estimate // 50 * 2)
    log.info(
        "wikipedia_seed: loading %s/%s split=%s revision=%s (streaming first %d rows)",
        repo_id, config, split, revision, rows_needed,
    )

    ds = load_dataset(
        repo_id,
        config,
        split=split,
        revision=revision,
        streaming=True,
    )
    pieces: list[str] = []
    rows_collected = 0
    for row in ds:
        text = row.get("text", "") if isinstance(row, dict) else ""
        if text.strip():
            pieces.append(text)
            rows_collected += 1
            if rows_collected >= rows_needed:
                break

    resolved_revision = revision
    try:
        # `streaming` mode doesn't expose info.download_checksums; resolved
        # revision lives on the underlying _info or builder. Best-effort:
        # log what we asked for, accept 'main' if nothing else is available.
        info = getattr(ds, "info", None)
        if info is not None and getattr(info, "version", None):
            resolved_revision = str(info.version)
    except Exception:  # noqa: BLE001
        pass

    text = "\n\n".join(pieces)
    log.info(
        "wikipedia_seed: collected %d rows / %d chars from %s",
        rows_collected, len(text), repo_id,
    )
    return text, resolved_revision


def chunk_to_documents(
    text: str,
    target_chunks: int,
    chunks_per_document: int,
) -> list[list[str]]:
    """Chunk `text` and slab the first `target_chunks` chunks into documents.

    Returns a list of documents, each a list of chunk strings. A trailing
    short document (fewer than `chunks_per_document` chunks) is allowed —
    the seeder treats it the same as a full slab.
    """
    chunks = chunk_text(text)
    if len(chunks) < target_chunks:
        raise RuntimeError(
            f"chunk_text produced {len(chunks)} chunks, need >= {target_chunks}; "
            f"fetch more wikitext rows"
        )
    chunks = chunks[:target_chunks]
    documents: list[list[str]] = []
    for i in range(0, len(chunks), chunks_per_document):
        documents.append(chunks[i : i + chunks_per_document])
    return documents


def viewer_visible_indices(
    viewer_id: uuid.UUID,
    visible_count: int,
    total_chunks: int,
    salt: str,
) -> list[int]:
    """Deterministic, exactly-K visible-chunk indices for a viewer.

    Hash each chunk index with `blake2b(salt || viewer_id || index)` and
    take the K with the lowest hash. Pure / deterministic / collision-
    resistant; independent across viewers (different `viewer_id` salt).
    """
    salt_bytes = salt.encode("utf-8")
    viewer_bytes = viewer_id.bytes
    scored: list[tuple[bytes, int]] = []
    for idx in range(total_chunks):
        h = hashlib.blake2b(
            salt_bytes + viewer_bytes + struct.pack(">I", idx),
            digest_size=8,
        ).digest()
        scored.append((h, idx))
    scored.sort()
    return sorted(idx for _, idx in scored[:visible_count])


async def _ensure_users(
    conn: asyncpg.Connection,
    viewers: list[dict[str, Any]],
) -> None:
    """Idempotently insert the wikipedia owner + viewer users into auth.users."""
    rows: list[tuple[uuid.UUID, str]] = [(WIKIPEDIA_USER_ID, WIKIPEDIA_USER_EMAIL)]
    for v in viewers:
        rows.append((uuid.UUID(v["id"]), v["email"]))
    await conn.executemany(
        """
        insert into auth.users (
            id, instance_id, aud, role, email, encrypted_password,
            email_confirmed_at, raw_app_meta_data, raw_user_meta_data,
            created_at, updated_at
        ) values (
            $1, '00000000-0000-0000-0000-000000000000',
            'authenticated', 'authenticated', $2, '',
            now(),
            '{"provider":"wikipedia_seed","providers":["wikipedia_seed"]}'::jsonb,
            '{}'::jsonb,
            now(), now()
        )
        on conflict (id) do nothing
        """,
        rows,
    )


async def _purge_existing(conn: asyncpg.Connection) -> int:
    """Delete prior wikipedia-seeded documents (chunks + chunk_acl cascade)."""
    result = await conn.execute(
        """
        delete from public.documents
         where user_id = $1
           and (metadata->>'wikipedia_seed') = 'true'
        """,
        WIKIPEDIA_USER_ID,
    )
    return int(result.rsplit(" ", 1)[1])


async def _insert_document(
    conn: asyncpg.Connection,
    document_id: uuid.UUID,
    doc_idx: int,
    chunk_count: int,
    byte_size: int,
) -> None:
    metadata = json.dumps({"wikipedia_seed": True, "doc_index": doc_idx})
    await conn.execute(
        """
        insert into public.documents (
            id, user_id, filename, storage_path, byte_size,
            content_type, status, chunks_count, metadata
        ) values (
            $1, $2, $3, $4, $5, 'text/plain', 'ready', $6, $7::jsonb
        )
        """,
        document_id,
        WIKIPEDIA_USER_ID,
        f"wikipedia-{doc_idx:04d}.txt",
        f"wikipedia-seed/wikipedia-{doc_idx:04d}.txt",
        byte_size,
        chunk_count,
        metadata,
    )


async def _insert_chunks(
    conn: asyncpg.Connection,
    document_id: uuid.UUID,
    doc_idx: int,
    chunks: list[str],
    embeddings: list[list[float]],
) -> None:
    if len(chunks) != len(embeddings):
        raise RuntimeError(
            f"chunk/embedding length mismatch: {len(chunks)} vs {len(embeddings)}"
        )
    rows = [
        (
            chunk_uuid(doc_idx, idx),
            document_id,
            WIKIPEDIA_USER_ID,
            idx,
            content,
            to_pgvector(embedding),
            stable_id(doc_idx, idx),
        )
        for idx, (content, embedding) in enumerate(zip(chunks, embeddings))
    ]
    await conn.executemany(
        """
        insert into public.chunks (
            id, document_id, user_id, chunk_index, content, embedding, stable_id
        ) values ($1, $2, $3, $4, $5, $6::vector, $7)
        """,
        rows,
    )


async def _write_acls(
    conn: asyncpg.Connection,
    viewers: list[dict[str, Any]],
    chunk_id_by_global_index: list[uuid.UUID],
    salt: str,
) -> dict[str, int]:
    """Write the per-viewer ACL rows.

    Per viewer: compute the visible global indices, map to chunk_ids, bulk
    insert chunk_acl rows (principal_type='user', granted_by=owner). The
    cascade on chunks(id) → chunk_acl(chunk_id) means the prior _purge_
    has already cleared these — we just need to insert fresh.
    """
    counts: dict[str, int] = {}
    for v in viewers:
        viewer_id = uuid.UUID(v["id"])
        visible = viewer_visible_indices(
            viewer_id=viewer_id,
            visible_count=int(v["visible_chunks"]),
            total_chunks=len(chunk_id_by_global_index),
            salt=salt,
        )
        rows = [
            (chunk_id_by_global_index[idx], "user", viewer_id, WIKIPEDIA_USER_ID)
            for idx in visible
        ]
        if rows:
            await conn.executemany(
                """
                insert into public.chunk_acl
                  (chunk_id, principal_type, principal_id, granted_by)
                values ($1, $2, $3, $4)
                on conflict do nothing
                """,
                rows,
            )
        counts[v["name"]] = len(rows)
    return counts


async def seed(config_path: Path = CONFIG_PATH) -> dict[str, Any]:
    url = os.environ.get("CORPUS_SEED_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "set CORPUS_SEED_DATABASE_URL (or DATABASE_URL) to a writable "
            "Postgres connection string"
        )
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required to embed wikipedia chunks")

    cfg = load_config(config_path)
    corpus_cfg = cfg["corpus"]
    viewers = cfg["viewers"]
    salt = cfg["seed_salt"]
    revision = os.environ.get("WIKITEXT_REVISION") or corpus_cfg["hf_revision"]
    total_chunks = int(corpus_cfg["total_chunks"])
    chunks_per_doc = int(corpus_cfg["chunks_per_document"])

    started = time.perf_counter()

    text, resolved_revision = fetch_wikitext(
        repo_id=corpus_cfg["hf_repo_id"],
        config=corpus_cfg["hf_config"],
        split=corpus_cfg["hf_split"],
        revision=revision,
        target_chunks=total_chunks,
    )

    documents = chunk_to_documents(text, total_chunks, chunks_per_doc)
    log.info(
        "wikipedia_seed: chunked into %d documents (%d chunks total)",
        len(documents), sum(len(d) for d in documents),
    )

    openai_client = AsyncOpenAI(api_key=api_key)
    conn = await asyncpg.connect(url)

    chunk_id_by_global_index: list[uuid.UUID] = []
    try:
        await _ensure_users(conn, viewers)
        purged = await _purge_existing(conn)
        if purged:
            log.info("wikipedia_seed: purged %d previously-seeded documents", purged)

        for doc_idx, chunks in enumerate(documents):
            embeddings = await embed_texts(openai_client, chunks)
            doc_byte_size = sum(len(c.encode("utf-8")) for c in chunks)
            document_id = document_uuid(doc_idx)
            await _insert_document(conn, document_id, doc_idx, len(chunks), doc_byte_size)
            await _insert_chunks(conn, document_id, doc_idx, chunks, embeddings)
            for chunk_in_doc in range(len(chunks)):
                chunk_id_by_global_index.append(chunk_uuid(doc_idx, chunk_in_doc))
            log.info(
                "wikipedia_seed: doc %d/%d done (%d chunks)",
                doc_idx + 1, len(documents), len(chunks),
            )

        acl_counts = await _write_acls(conn, viewers, chunk_id_by_global_index, salt)
    finally:
        await conn.close()

    elapsed = round(time.perf_counter() - started, 2)
    return {
        "seed_version": cfg.get("seed_version", 1),
        "documents": len(documents),
        "chunks": sum(len(d) for d in documents),
        "viewer_acl_counts": acl_counts,
        "hf_revision_requested": revision,
        "hf_revision_resolved": resolved_revision,
        "elapsed_s": elapsed,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="US-043 wikipedia seeder")
    parser.add_argument(
        "--config", type=Path, default=CONFIG_PATH,
        help=f"Path to scale_gold.yaml (default: {CONFIG_PATH})",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    summary = asyncio.run(seed(args.config))
    print("wikipedia seed complete:")
    for key, value in summary.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
