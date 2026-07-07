"""US-110: generic corpus seeder — corpus only, optional real-grant manifest.

The eval-specific `db_seed.corpus_seed` seeds the shipped e-commerce demo
corpus into a fixed owner / Default Workspace so the retrieval eval resolves
its content anchors reproducibly. This module is the **genericized** version a
buyer points at their **production** corpus: a docs folder plus an *optional*
manifest of **real** workspaces / principals / grants → the production
`chunk_text` + `embed_texts` paths → `documents` + `chunks` (+ real
`chunk_acl` / `workspace_membership` / group rows from the manifest).

Two invariants make it safe against a production corpus (US-110):

1. **It seeds corpus + real grants and nothing eval-specific.** It has *no*
   reference to the synthetic eval viewers (`PARTIAL_VIEWER_ID` /
   `NO_ACCESS_VIEWER_ID`) or the derived `full / partial / no_access` ACL
   matrix — those are built transiently by the eval runner at run time
   (`evals/retrieval/runner.py::ensure_viewer_users` / `reset_viewer_acls`),
   never baked into a seed. A production seed therefore carries zero test
   principals. This is structural: the constants simply do not exist here.

2. **No manifest ⇒ owner-only corpus.** With no manifest the seeder inserts
   `documents` + `chunks` owned by a single owner and writes an *empty*
   `chunk_acl` — so the corpus is owner-only, consistent with the no-backfill
   rollout. Grants appear only when a manifest explicitly requests them.

Re-runnable / idempotent: prior rows from *this* seed are identified by
`documents.metadata->>'generic_seed_label'` ALONE (scoped so a buyer seed never
clobbers the eval corpus, which is marked `corpus_seed=true`) and deleted
before re-inserting — so re-seeding a label replaces all its documents even if
the manifest owner id changed across re-seeds; chunks + their `chunk_acl`
cascade via the documents FK.
The resulting `(stable_id, md5(content))` pairs are byte-identical across
re-seeds (the PRD validation criterion) because `stable_id = "{slug}:{idx}"`
and `content` come only from the deterministic `chunk_text`.

The production code path is measured, not re-implemented: `chunk_text` and
`embed_texts` are imported from `backend/` unchanged — the eval must exercise
the same chunker + embedder a PR would change, and so must any seed a buyer
evaluates against.

Run:
    python -m db_seed.generic_seed --docs-dir ./my_corpus \
        [--manifest ./manifest.yaml] [--seed-label acme]

Reads:
    GENERIC_SEED_DATABASE_URL  (or DATABASE_URL fallback) — writable Postgres URL
    Embedder connection — resolved from the embedder-role ProviderConfig
        (EMBEDDER_* / OPENAI_API_KEY / AZURE_OPENAI_*; see backend/model_config.py)

Manifest format: see `db_seed/manifest.example.yaml`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import asyncpg

# Reuse the shared, already-tested primitives from the eval seeder so the two
# seeders stay in lockstep on chunking-path setup, embedder construction, the
# slug/stable_id shape, and the embedding_config stamp. Importing corpus_seed
# performs the `backend/` sys.path insertion and the chunking/embeddings import.
from db_seed.corpus_seed import (
    build_embedder_client,
    filename_slug,
    stable_id,
    stamp_embedding_config,
)

# The production chunking + embeddings code paths (same imports corpus_seed made
# reachable). The seeder MUST go through these — otherwise the eval would
# measure a different code path from the one PRs change.
from chunking import chunk_text  # noqa: E402
from embeddings import embed_texts, get_embedding_model, to_pgvector  # noqa: E402

log = logging.getLogger("agentic_rag.db_seed.generic")

# Text document extensions the seeder ingests. Real ingestion parses richer
# formats via the parser layer, but the seeder operates in the text domain that
# `chunk_text` consumes directly (a buyer converts PDFs etc. up front, exactly
# as the production ingest endpoint hands parsed text to the chunker).
DOC_GLOBS: tuple[str, ...] = ("*.md", "*.txt")

# Defaults for the no-manifest, owner-only path. These are a single *real* owner
# and workspace, NOT synthetic eval principals: the corpus is owned by this
# owner and, with an empty chunk_acl, is visible only to them (owner-only). A
# buyer overrides both via a manifest.
DEFAULT_OWNER_ID = uuid.UUID("00000000-0000-0000-0000-0000000ce001")
DEFAULT_OWNER_EMAIL = "generic-seed-owner@local.test"
DEFAULT_WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-0000000ce0a0")
DEFAULT_WORKSPACE_NAME = "Generic Seed Workspace"


# ---------------------------------------------------------------------------
# Deterministic identity
# ---------------------------------------------------------------------------


def _seed_namespace(seed_label: str) -> str:
    """UUID-derivation namespace, scoped by seed label so two distinct buyer
    seeds in one DB never collide on a shared filename slug."""
    return f"agentic-rag/generic/{seed_label}"


def document_uuid(seed_label: str, slug: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"{_seed_namespace(seed_label)}/{slug}")


def chunk_uuid(seed_label: str, slug: str, chunk_index: int) -> uuid.UUID:
    return uuid.uuid5(
        uuid.NAMESPACE_URL, f"{_seed_namespace(seed_label)}/{slug}:{chunk_index}"
    )


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ManifestUser:
    id: uuid.UUID
    email: str
    role: str  # workspace membership role: 'admin' | 'member'


@dataclass(frozen=True)
class ManifestGroup:
    id: uuid.UUID
    name: str
    members: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class ManifestGrant:
    document: str  # filename or filename_slug of a seeded document
    principal_type: str  # 'user' | 'group'
    principal_id: uuid.UUID


@dataclass(frozen=True)
class Manifest:
    """A parsed, validated manifest. Everything here is *real* production data:
    workspaces, principals, memberships, and document→principal grants. There is
    no representation for synthetic eval viewers or a derived ACL matrix — those
    concepts do not exist in this module."""

    owner_id: uuid.UUID
    owner_email: str
    workspace_id: uuid.UUID
    workspace_name: str
    users: tuple[ManifestUser, ...] = ()
    groups: tuple[ManifestGroup, ...] = ()
    grants: tuple[ManifestGrant, ...] = ()


class ManifestError(ValueError):
    """Raised on a malformed manifest. Fail loud — an eval built on a broken
    manifest is worse than no manifest (mirrors the US-107 zero-resolve stance:
    an ambiguous permission fixture is a hard error, never a silent wrong)."""


def default_manifest() -> Manifest:
    """The no-manifest, owner-only manifest: one real owner + workspace, no
    extra principals, no grants ⇒ an empty `chunk_acl`."""
    return Manifest(
        owner_id=DEFAULT_OWNER_ID,
        owner_email=DEFAULT_OWNER_EMAIL,
        workspace_id=DEFAULT_WORKSPACE_ID,
        workspace_name=DEFAULT_WORKSPACE_NAME,
    )


def _require(mapping: dict, key: str, ctx: str) -> object:
    if not isinstance(mapping, dict) or key not in mapping:
        raise ManifestError(f"{ctx}: missing required key {key!r}")
    return mapping[key]


def _as_uuid(value: object, ctx: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ManifestError(f"{ctx}: {value!r} is not a valid UUID") from exc


def parse_manifest(data: dict) -> Manifest:
    """Parse + validate a manifest mapping into a `Manifest`.

    Validation is strict and fail-loud: unknown principal types, grants that
    reference an unknown principal, and non-UUID ids are hard errors. Grant
    *document* references are validated later against the actually-seeded
    documents (a grant for a doc not in the folder is a hard error too), because
    only the seed run knows which documents exist.
    """
    if not isinstance(data, dict):
        raise ManifestError("manifest root must be a mapping")

    owner = _require(data, "owner", "manifest")
    owner_id = _as_uuid(_require(owner, "id", "owner"), "owner.id")  # type: ignore[arg-type]
    owner_email = str(_require(owner, "email", "owner"))  # type: ignore[arg-type]

    workspace = _require(data, "workspace", "manifest")
    workspace_id = _as_uuid(_require(workspace, "id", "workspace"), "workspace.id")  # type: ignore[arg-type]
    workspace_name = str(_require(workspace, "name", "workspace"))  # type: ignore[arg-type]

    principals = data.get("principals") or {}
    if not isinstance(principals, dict):
        raise ManifestError("manifest.principals must be a mapping (users / groups)")

    users: list[ManifestUser] = []
    for i, raw in enumerate(principals.get("users") or []):
        ctx = f"principals.users[{i}]"
        uid = _as_uuid(_require(raw, "id", ctx), f"{ctx}.id")
        email = str(_require(raw, "email", ctx))
        role = str(raw.get("role", "member"))
        if role not in ("admin", "member"):
            raise ManifestError(f"{ctx}.role: {role!r} must be 'admin' or 'member'")
        users.append(ManifestUser(id=uid, email=email, role=role))

    groups: list[ManifestGroup] = []
    for i, raw in enumerate(principals.get("groups") or []):
        ctx = f"principals.groups[{i}]"
        gid = _as_uuid(_require(raw, "id", ctx), f"{ctx}.id")
        name = str(_require(raw, "name", ctx))
        members = tuple(
            _as_uuid(m, f"{ctx}.members[{j}]")
            for j, m in enumerate(raw.get("members") or [])
        )
        groups.append(ManifestGroup(id=gid, name=name, members=members))

    # Principal ids grants may legitimately reference: the owner, any listed
    # user, or any listed group. A grant to anyone else is a hard error — it
    # would silently do nothing (no matching principal ⇒ no visibility).
    known_user_ids = {owner_id} | {u.id for u in users}
    known_group_ids = {g.id for g in groups}

    grants: list[ManifestGrant] = []
    for i, raw in enumerate(data.get("grants") or []):
        ctx = f"grants[{i}]"
        document = str(_require(raw, "document", ctx))
        ptype = str(_require(raw, "principal_type", ctx))
        if ptype not in ("user", "group"):
            raise ManifestError(
                f"{ctx}.principal_type: {ptype!r} must be 'user' or 'group'"
            )
        pid = _as_uuid(_require(raw, "principal_id", ctx), f"{ctx}.principal_id")
        if ptype == "user" and pid not in known_user_ids:
            raise ManifestError(
                f"{ctx}: grants to user {pid} but no such user (or owner) is "
                "declared in the manifest"
            )
        if ptype == "group" and pid not in known_group_ids:
            raise ManifestError(
                f"{ctx}: grants to group {pid} but no such group is declared "
                "in the manifest"
            )
        grants.append(
            ManifestGrant(document=document, principal_type=ptype, principal_id=pid)
        )

    # A group member must be a declared user (or the owner) so its
    # principal_membership row is meaningful.
    for g in groups:
        for m in g.members:
            if m not in known_user_ids:
                raise ManifestError(
                    f"group {g.name!r}: member {m} is not a declared user (or owner)"
                )

    return Manifest(
        owner_id=owner_id,
        owner_email=owner_email,
        workspace_id=workspace_id,
        workspace_name=workspace_name,
        users=tuple(users),
        groups=tuple(groups),
        grants=tuple(grants),
    )


def load_manifest(path: Path) -> Manifest:
    """Load + parse a manifest from a `.yaml`/`.yml`/`.json` file."""
    if not path.is_file():
        raise ManifestError(f"manifest file not found: {path}")
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        import yaml  # local import: only the manifest path needs PyYAML

        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    return parse_manifest(data)


# ---------------------------------------------------------------------------
# Corpus loading
# ---------------------------------------------------------------------------


def validate_grant_documents(
    manifest: Manifest, corpus: list[tuple[str, str]]
) -> None:
    """Fail fast (before any paid embed / DB insert) if a manifest grant names a
    document not present in the loaded corpus. The corpus slugs are already known
    at this point, so a typo'd `grants[].document` should never cost a full embed
    or leave a partially-seeded DB. `_apply_grants` keeps the same check as a
    backstop against a slug that slips through here."""
    corpus_slugs = {filename_slug(filename) for filename, _ in corpus}
    for grant in manifest.grants:
        slug = filename_slug(grant.document)
        if slug not in corpus_slugs:
            raise RuntimeError(
                f"manifest grant references document {grant.document!r} "
                f"(slug {slug!r}) which is not present in the seeded docs folder"
            )


def load_docs(docs_dir: Path) -> list[tuple[str, str]]:
    """Return `[(filename, content)]` sorted by filename for deterministic order.

    A slug collision (two files whose stems slugify to the same value) is a hard
    error — otherwise they would share a document UUID and silently clobber each
    other.
    """
    if not docs_dir.is_dir():
        raise RuntimeError(f"docs directory missing: {docs_dir}")
    files: list[Path] = []
    for pattern in DOC_GLOBS:
        files.extend(docs_dir.glob(pattern))
    files = sorted(set(files), key=lambda p: p.name)
    if not files:
        raise RuntimeError(
            f"no {'/'.join(DOC_GLOBS)} files in {docs_dir}"
        )
    seen: dict[str, str] = {}
    corpus: list[tuple[str, str]] = []
    for f in files:
        slug = filename_slug(f.name)
        if slug in seen:
            raise RuntimeError(
                f"filename slug collision: {f.name!r} and {seen[slug]!r} both "
                f"slugify to {slug!r}"
            )
        seen[slug] = f.name
        corpus.append((f.name, f.read_text(encoding="utf-8")))
    return corpus


# ---------------------------------------------------------------------------
# DB writes (service-role / asyncpg — the seeder is never run in production)
# ---------------------------------------------------------------------------


async def _ensure_user(conn: asyncpg.Connection, user_id: uuid.UUID, email: str) -> None:
    """Insert a real user into auth.users if absent. The email→profiles mirror
    trigger (US-037) populates public.profiles automatically. Idempotent."""
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
            '{"provider":"generic_seed","providers":["generic_seed"]}'::jsonb,
            '{}'::jsonb,
            now(), now()
        )
        on conflict (id) do nothing
        """,
        user_id,
        email,
    )


async def _ensure_workspace(
    conn: asyncpg.Connection, workspace_id: uuid.UUID, name: str
) -> None:
    await conn.execute(
        """
        insert into public.workspaces (id, name)
        values ($1, $2)
        on conflict (id) do nothing
        """,
        workspace_id,
        name,
    )


async def _ensure_membership(
    conn: asyncpg.Connection,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str,
) -> None:
    await conn.execute(
        """
        insert into public.workspace_membership (workspace_id, user_id, role)
        values ($1, $2, $3)
        on conflict do nothing
        """,
        workspace_id,
        user_id,
        role,
    )


async def _ensure_group(conn: asyncpg.Connection, group: ManifestGroup) -> None:
    """Register a real group principal + its membership rows (US-037)."""
    await conn.execute(
        """
        insert into public.principals (id, name, kind)
        values ($1, $2, 'group')
        on conflict (id) do nothing
        """,
        group.id,
        group.name,
    )
    if group.members:
        await conn.executemany(
            """
            insert into public.principal_membership (principal_id, member_user_id)
            values ($1, $2)
            on conflict do nothing
            """,
            [(group.id, m) for m in group.members],
        )


async def _purge_existing(conn: asyncpg.Connection, seed_label: str) -> int:
    """Delete prior documents from *this* seed only (chunks + chunk_acl cascade
    via the documents FK). Scoped by `generic_seed_label` ALONE so re-seeding a
    label replaces ALL documents carrying it regardless of owner — the advertised
    re-runnable/byte-stable idempotency then holds even when the manifest owner id
    changes across re-seeds. The label is buyer-namespaced to their seed, so it
    still never touches the eval corpus (`corpus_seed=true`) or another labelled
    seed."""
    result = await conn.execute(
        """
        delete from public.documents
         where (metadata->>'generic_seed_label') = $1
        """,
        seed_label,
    )
    return int(result.rsplit(" ", 1)[1])


async def _insert_document(
    conn: asyncpg.Connection,
    *,
    document_id: uuid.UUID,
    owner_id: uuid.UUID,
    workspace_id: uuid.UUID,
    seed_label: str,
    filename: str,
    byte_size: int,
    chunks_count: int,
) -> None:
    metadata = json.dumps(
        {
            "generic_seed": True,
            "generic_seed_label": seed_label,
            "filename_slug": filename_slug(filename),
        }
    )
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
        owner_id,
        workspace_id,
        filename,
        f"generic-seed/{seed_label}/{filename}",
        byte_size,
        chunks_count,
        metadata,
    )


async def _insert_chunks(
    conn: asyncpg.Connection,
    *,
    document_id: uuid.UUID,
    owner_id: uuid.UUID,
    seed_label: str,
    slug: str,
    chunks: list[str],
    embeddings: list[list[float]],
) -> list[uuid.UUID]:
    if len(chunks) != len(embeddings):
        raise RuntimeError(
            f"chunk/embedding length mismatch: {len(chunks)} vs {len(embeddings)}"
        )
    chunk_ids = [chunk_uuid(seed_label, slug, idx) for idx in range(len(chunks))]
    rows = [
        (
            chunk_ids[idx],
            document_id,
            owner_id,
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
    return chunk_ids


async def _apply_grants(
    conn: asyncpg.Connection,
    *,
    manifest: Manifest,
    owner_id: uuid.UUID,
    slug_to_chunk_ids: dict[str, list[uuid.UUID]],
) -> int:
    """Expand each document→principal manifest grant into per-chunk `chunk_acl`
    rows (mirroring `permissions.grant_doc_to_principal`). A grant for a document
    not present in this seed is a hard error. Returns the number of rows written.
    """
    rows: list[tuple[uuid.UUID, str, uuid.UUID, uuid.UUID]] = []
    for grant in manifest.grants:
        slug = filename_slug(grant.document)
        chunk_ids = slug_to_chunk_ids.get(slug)
        if chunk_ids is None:
            raise RuntimeError(
                f"manifest grant references document {grant.document!r} "
                f"(slug {slug!r}) which is not present in the seeded docs folder"
            )
        for cid in chunk_ids:
            rows.append((cid, grant.principal_type, grant.principal_id, owner_id))
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
    return len(rows)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass
class SeedResult:
    documents: int
    chunks: int
    users: int
    groups: int
    chunk_acl_rows: int
    metadata: dict = field(default_factory=dict)


async def seed(
    *,
    docs_dir: Path,
    manifest_path: Path | None = None,
    database_url: str | None = None,
    seed_label: str = "default",
) -> SeedResult:
    """Purge + reseed a corpus (and, if a manifest is given, real grants).

    With no manifest the corpus is owner-only (empty `chunk_acl`). The seeder
    never inserts synthetic eval viewers or the derived ACL matrix — those are a
    runner-only construction (US-110).
    """
    url = (
        database_url
        or os.environ.get("GENERIC_SEED_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
    )
    if not url:
        raise RuntimeError(
            "set GENERIC_SEED_DATABASE_URL (or DATABASE_URL) to a writable "
            "Postgres connection string"
        )

    corpus = load_docs(docs_dir)
    manifest = load_manifest(manifest_path) if manifest_path else default_manifest()
    # Fail fast on a bad grant BEFORE any paid embed or DB write — the corpus
    # slugs are already known, so a typo'd grant never costs an embed loop or
    # leaves a partially-seeded DB.
    validate_grant_documents(manifest, corpus)
    openai_client = build_embedder_client()
    conn = await asyncpg.connect(url)

    total_chunks = 0
    produced_dim: int | None = None
    slug_to_chunk_ids: dict[str, list[uuid.UUID]] = {}
    try:
        # Real principals first (FKs for membership / documents / grants).
        await _ensure_workspace(conn, manifest.workspace_id, manifest.workspace_name)
        await _ensure_user(conn, manifest.owner_id, manifest.owner_email)
        await _ensure_membership(
            conn, manifest.workspace_id, manifest.owner_id, "member"
        )
        for u in manifest.users:
            await _ensure_user(conn, u.id, u.email)
            await _ensure_membership(conn, manifest.workspace_id, u.id, u.role)
        for g in manifest.groups:
            await _ensure_group(conn, g)

        purged = await _purge_existing(conn, seed_label)
        if purged:
            log.info("generic_seed: purged %d previously-seeded documents", purged)

        for filename, content in corpus:
            slug = filename_slug(filename)
            chunks = chunk_text(content)
            if not chunks:
                raise RuntimeError(f"chunking produced 0 chunks for {filename}")
            embeddings = await embed_texts(openai_client, chunks)
            if embeddings and produced_dim is None:
                produced_dim = len(embeddings[0])
            document_id = document_uuid(seed_label, slug)
            await _insert_document(
                conn,
                document_id=document_id,
                owner_id=manifest.owner_id,
                workspace_id=manifest.workspace_id,
                seed_label=seed_label,
                filename=filename,
                byte_size=len(content.encode("utf-8")),
                chunks_count=len(chunks),
            )
            chunk_ids = await _insert_chunks(
                conn,
                document_id=document_id,
                owner_id=manifest.owner_id,
                seed_label=seed_label,
                slug=slug,
                chunks=chunks,
                embeddings=embeddings,
            )
            slug_to_chunk_ids[slug] = chunk_ids
            total_chunks += len(chunks)

        acl_rows = await _apply_grants(
            conn,
            manifest=manifest,
            owner_id=manifest.owner_id,
            slug_to_chunk_ids=slug_to_chunk_ids,
        )

        # US-026: stamp the corpus with the embedder model + produced dim. The
        # seeder just rebuilt this corpus, so it is the authoritative (re)index.
        if produced_dim is not None:
            await stamp_embedding_config(conn, get_embedding_model(), produced_dim)
    finally:
        await conn.close()

    return SeedResult(
        documents=len(corpus),
        chunks=total_chunks,
        users=1 + len(manifest.users),
        groups=len(manifest.groups),
        chunk_acl_rows=acl_rows,
        metadata={"seed_label": seed_label, "has_manifest": manifest_path is not None},
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m db_seed.generic_seed",
        description=(
            "Generic corpus seeder: seed a docs folder (+ optional real-grant "
            "manifest) into documents/chunks/chunk_acl. Never seeds synthetic "
            "eval principals — that scaffolding is built by the eval runner."
        ),
    )
    parser.add_argument(
        "--docs-dir",
        required=True,
        type=Path,
        help="Folder of *.md / *.txt documents to seed.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Optional manifest (YAML/JSON) of real workspaces / principals / "
        "grants. Omit for an owner-only corpus.",
    )
    parser.add_argument(
        "--seed-label",
        default="default",
        help="Label scoping this seed's rows for idempotent re-seeding "
        "(default: 'default').",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    args = _parse_args(argv)
    result = asyncio.run(
        seed(
            docs_dir=args.docs_dir,
            manifest_path=args.manifest,
            seed_label=args.seed_label,
        )
    )
    print("generic seed complete:")
    print(f"  documents:     {result.documents}")
    print(f"  chunks:        {result.chunks}")
    print(f"  users:         {result.users}")
    print(f"  groups:        {result.groups}")
    print(f"  chunk_acl:     {result.chunk_acl_rows}")
    if result.chunk_acl_rows == 0:
        print("  (owner-only corpus — no manifest grants)")


if __name__ == "__main__":
    main(sys.argv[1:])
