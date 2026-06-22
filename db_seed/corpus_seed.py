"""US-032: deterministic seed for the retrieval-eval text corpus.

Reads markdown files from `db_seed/corpus/`, chunks them via the same
`backend.chunking.chunk_text` the real ingestion path uses, embeds via
OpenAI, and inserts deterministic `documents` + `chunks` rows so the
US-033 golden set can key on `chunks.stable_id` values that survive
re-seeds and clean CI bootstraps.

Re-runnable: identifies existing corpus rows by
`documents.metadata->>'corpus_seed' = 'true'` and deletes them before
re-inserting. Chunks cascade via the documents FK. The resulting
`(stable_id, content)` pairs are byte-identical across runs (the
validation criterion from the PRD).

Determinism stack:

- File contents are committed to the repo.
- `chunk_text()` is deterministic for fixed input + size/overlap defaults.
- `stable_id = "{filename_slug}:{chunk_index}"`.
- `document.id = uuid5(NAMESPACE_URL, "agentic-rag/corpus/{filename_slug}")`.
- `chunks.id = uuid5(NAMESPACE_URL, "agentic-rag/corpus/{slug}:{chunk_index}")`.
- Embeddings are NOT byte-deterministic across OpenAI API calls but the
  validation query (`select stable_id, md5(content) from chunks`) does
  not depend on them.

Run:
    python -m db_seed.corpus_seed

Reads:
    CORPUS_SEED_DATABASE_URL  (or DATABASE_URL fallback) — writable Postgres URL
    Embedder connection — resolved from the embedder-role ProviderConfig
        (EMBEDDER_* / OPENAI_API_KEY / AZURE_OPENAI_*; see backend/model_config.py)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import uuid
from pathlib import Path

import asyncpg
from openai import AsyncOpenAI

# Reuse the production chunking + embeddings code paths. The seeder MUST
# go through the same `chunk_text` and `embed_texts` that the real ingestion
# pipeline uses — otherwise the eval would measure a different code path
# from the one PRs change.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from chunking import chunk_text  # noqa: E402
from embeddings import embed_texts, get_embedding_model, to_pgvector  # noqa: E402
from model_config import ProviderConfig, build_openai_client  # noqa: E402

log = logging.getLogger("agentic_rag.db_seed.corpus")


def build_embedder_client() -> AsyncOpenAI:
    """US-027: build the embedding client from the embedder-role ProviderConfig
    (EMBEDDER_PROVIDER / EMBEDDER_BASE_URL / EMBEDDER_AZURE_* → answerer
    fallback), so the documented re-index remedy embeds via the SAME provider the
    running app uses — not a hardcoded OpenAI host. Fails closed (ValueError) on
    a missing key for the resolved provider, exactly like the backend startup.
    Shared by both seeders so they stay in lockstep."""
    return build_openai_client(ProviderConfig.from_env("embedder"))

CORPUS_DIR = Path(__file__).resolve().parent / "corpus"

# Fixed test user that owns the seeded corpus. The corpus is an eval fixture,
# not real customer data, so service-role inserts under a sentinel user_id
# are the cleanest way to satisfy the chunks.user_id NOT NULL FK without
# coupling to a human Supabase Auth account.
CORPUS_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
CORPUS_USER_EMAIL = "corpus-seed@local.test"

# US-002: the fixed Default Workspace every legacy/eval row migrates into. Must
# match the constant in the init migration (20260617120200_default_workspace_backfill.sql)
# and evals/retrieval/runner.py. The corpus user is created here, *after* that
# migration's auth.users backfill runs, so we add its membership explicitly —
# otherwise the US-003 subtractive membership clause would hide the whole corpus.
DEFAULT_WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000d0")

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def filename_slug(filename: str) -> str:
    """Stable, human-readable slug used in `stable_id` and the document UUID.

    Example: "Refund Policy.md" -> "refund-policy".
    """
    stem = Path(filename).stem.lower()
    return _SLUG_RE.sub("-", stem).strip("-")


def stable_id(slug: str, chunk_index: int) -> str:
    return f"{slug}:{chunk_index}"


def document_uuid(slug: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"agentic-rag/corpus/{slug}")


def chunk_uuid(slug: str, chunk_index: int) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"agentic-rag/corpus/{slug}:{chunk_index}")


def load_corpus() -> list[tuple[str, str]]:
    """Return `[(filename, content)]` sorted by filename for deterministic order."""
    if not CORPUS_DIR.is_dir():
        raise RuntimeError(f"corpus directory missing: {CORPUS_DIR}")
    files = sorted(CORPUS_DIR.glob("*.md"))
    if not files:
        raise RuntimeError(f"no *.md files in {CORPUS_DIR}")
    return [(f.name, f.read_text(encoding="utf-8")) for f in files]


async def _ensure_test_user(conn: asyncpg.Connection) -> None:
    """Insert the corpus test user into auth.users if it isn't already there.

    Direct insert into auth.users bypasses the normal Supabase Auth flow and
    is only acceptable because this is a service-role seed against a local /
    CI database. The seeder is never invoked in production.
    """
    await conn.execute(
        """
        insert into auth.users (
            id, instance_id, aud, role, email, encrypted_password,
            email_confirmed_at, raw_app_meta_data, raw_user_meta_data,
            created_at, updated_at
        ) values (
            $1, '00000000-0000-0000-0000-000000000000',
            'authenticated', 'authenticated', $2, '',
            now(),
            '{"provider":"corpus_seed","providers":["corpus_seed"]}'::jsonb,
            '{}'::jsonb,
            now(), now()
        )
        on conflict (id) do nothing
        """,
        CORPUS_USER_ID,
        CORPUS_USER_EMAIL,
    )


async def _ensure_workspace_membership(conn: asyncpg.Connection) -> None:
    """Add the corpus user to the Default Workspace (US-002).

    The init migration backfills users that exist at migration time, but the
    corpus user is inserted by `_ensure_test_user` *after* migrations run. Once
    the US-003 subtractive membership clause lands, a corpus owner that is not a
    member of its documents' workspace is hidden from itself — E4 would regress
    to all-zero recall. Idempotent.
    """
    await conn.execute(
        """
        insert into public.workspace_membership (workspace_id, user_id, role)
        values ($1, $2, 'member')
        on conflict do nothing
        """,
        DEFAULT_WORKSPACE_ID,
        CORPUS_USER_ID,
    )


async def _purge_existing(conn: asyncpg.Connection) -> int:
    """Delete prior corpus documents (chunks cascade via the documents FK)."""
    result = await conn.execute(
        """
        delete from public.documents
         where user_id = $1
           and (metadata->>'corpus_seed') = 'true'
        """,
        CORPUS_USER_ID,
    )
    # asyncpg returns command tags like "DELETE 7"; parse the count.
    return int(result.rsplit(" ", 1)[1])


async def _insert_document(
    conn: asyncpg.Connection,
    document_id: uuid.UUID,
    filename: str,
    byte_size: int,
    chunks_count: int,
) -> None:
    metadata = json.dumps({
        "corpus_seed": True,
        "filename_slug": filename_slug(filename),
    })
    await conn.execute(
        """
        insert into public.documents (
            id, user_id, workspace_id, filename, storage_path, byte_size,
            content_type, status, chunks_count, metadata
        ) values (
            $1, $2, $3, $4, $5, $6, 'text/markdown', 'ready', $7, $8::jsonb
        )
        """,
        document_id,
        CORPUS_USER_ID,
        DEFAULT_WORKSPACE_ID,
        filename,
        f"corpus-seed/{filename}",
        byte_size,
        chunks_count,
        metadata,
    )


async def _insert_chunks(
    conn: asyncpg.Connection,
    document_id: uuid.UUID,
    slug: str,
    chunks: list[str],
    embeddings: list[list[float]],
) -> None:
    if len(chunks) != len(embeddings):
        raise RuntimeError(
            f"chunk/embedding length mismatch: {len(chunks)} vs {len(embeddings)}"
        )
    rows = [
        (
            chunk_uuid(slug, idx),
            document_id,
            CORPUS_USER_ID,
            idx,
            content,
            to_pgvector(embedding),
            stable_id(slug, idx),
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


async def stamp_embedding_config(
    conn: asyncpg.Connection, model: str, dim: int
) -> None:
    """US-026: upsert the single-row `embedding_config` corpus stamp.

    Shared by the corpus + wikipedia seeders (both produce embeddings via direct
    asyncpg inserts, bypassing the production ingest endpoint — so the stamp has
    to be written here too, or a freshly-seeded corpus would have chunks but no
    stamp and US-027's startup guard would have nothing to compare against).

    Bulk-(re)index semantics: a seeder rebuilds the corpus under the *current*
    embedder, so it OVERWRITES the stamp (`do update`) to match what it just
    produced. That is deliberately different from the production ingest endpoint,
    which is insert-if-absent so a routine per-doc ingest can't silently rewrite
    the recorded model (US-027's drift guard depends on that). Service-role /
    asyncpg bypasses the table's insert-only RLS, so the overwrite is permitted
    on this path.
    """
    await conn.execute(
        """
        insert into public.embedding_config (singleton, model, dim)
        values (true, $1, $2)
        on conflict (singleton) do update
          set model = excluded.model,
              dim = excluded.dim,
              indexed_at = now()
        """,
        model,
        dim,
    )


async def seed() -> dict[str, int]:
    """Truncate + reseed the corpus. Returns row counts."""
    url = os.environ.get("CORPUS_SEED_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "set CORPUS_SEED_DATABASE_URL (or DATABASE_URL) to a writable "
            "Postgres connection string"
        )
    corpus = load_corpus()
    openai_client = build_embedder_client()
    conn = await asyncpg.connect(url)

    total_chunks = 0
    produced_dim: int | None = None
    try:
        await _ensure_test_user(conn)
        await _ensure_workspace_membership(conn)
        purged = await _purge_existing(conn)
        if purged:
            log.info("corpus_seed: purged %d previously-seeded documents", purged)

        for filename, content in corpus:
            slug = filename_slug(filename)
            chunks = chunk_text(content)
            if not chunks:
                raise RuntimeError(f"chunking produced 0 chunks for {filename}")
            embeddings = await embed_texts(openai_client, chunks)
            if embeddings and produced_dim is None:
                produced_dim = len(embeddings[0])
            document_id = document_uuid(slug)
            await _insert_document(
                conn,
                document_id,
                filename,
                len(content.encode("utf-8")),
                len(chunks),
            )
            await _insert_chunks(conn, document_id, slug, chunks, embeddings)
            total_chunks += len(chunks)

        # US-026: stamp the corpus with the embedder model + produced dim. The
        # seeder just rebuilt the whole corpus, so this is the authoritative
        # (re)index — overwrite the stamp to match what was produced.
        if produced_dim is not None:
            await stamp_embedding_config(conn, get_embedding_model(), produced_dim)
    finally:
        await conn.close()

    return {"documents": len(corpus), "chunks": total_chunks}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    counts = asyncio.run(seed())
    print("corpus seed complete:")
    for table, n in counts.items():
        print(f"  {table}: {n}")


if __name__ == "__main__":
    main()
