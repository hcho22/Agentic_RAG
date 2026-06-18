"""US-026 validation test: the single-row `embedding_config` corpus stamp.

Two layers:

1. **Offline pure checks** that always run — the default embedder model is the
   one the migration's `vector(1536)` column was sized for, and the shared
   `stamp_embedding_config` upsert is importable.

2. **DB checks** that run when `CORPUS_SEED_DATABASE_URL` (or `DATABASE_URL`)
   points at a migrated Postgres. All assertions run inside ONE transaction that
   is rolled back at the end, so the test never mutates the corpus stamp (matches
   the rolled-back transactional style of the workspace tests). They prove:
     * the single-row invariant + model/dim round-trip;
     * the stamp `dim` equals the actual `chunks.embedding` column dimension
       (the PRD failure indicator: a stamp whose dim disagrees with the column);
     * insert-if-absent (the production ingest path) does NOT overwrite a model
       drift — the stamp stays as first written, so US-027 can still detect it;
     * bulk re-index (the seeder `do update` path) DOES overwrite, still one row;
     * the singleton CHECK makes a second row impossible.

3. **Optional real-seed check** (gated on `US026_RUN_SEED=1` + `OPENAI_API_KEY`)
   runs the actual corpus seeder and asserts the stamp it writes is exactly
   `(get_embedding_model(), 1536)` — the PRD validation test taken literally.
   Off by default so the suite stays cheap (it embeds the whole corpus).

Run:
    DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:54322/postgres \
        python -m db_seed.test_embedding_config
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import asyncpg

from db_seed.corpus_seed import stamp_embedding_config

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from embeddings import DEFAULT_MODEL, get_embedding_model  # noqa: E402

# The dimension the chunks.embedding column was created with
# (supabase/migrations/20260417140000_add_chunks_embedding.sql) and the model
# the default embedder produces it under.
EXPECTED_DIM = 1536
EXPECTED_MODEL = "text-embedding-3-small"


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _offline_checks() -> None:
    _check(
        DEFAULT_MODEL == EXPECTED_MODEL,
        f"default embedder model should be {EXPECTED_MODEL!r}, got {DEFAULT_MODEL!r}",
    )
    _check(callable(stamp_embedding_config), "stamp_embedding_config must be importable")
    print(f"offline checks passed (default model {DEFAULT_MODEL!r} / dim {EXPECTED_DIM})")


async def _column_dim(conn: asyncpg.Connection) -> int:
    """The declared dimension of chunks.embedding. pgvector stores the dim
    directly in atttypmod (no length-header offset), so it reads back as-is."""
    return await conn.fetchval(
        """
        select atttypmod
          from pg_attribute
         where attrelid = 'public.chunks'::regclass
           and attname = 'embedding'
        """
    )


async def _db_checks() -> None:
    url = os.environ.get("CORPUS_SEED_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        print("SKIP db checks: CORPUS_SEED_DATABASE_URL/DATABASE_URL unset")
        return

    conn = await asyncpg.connect(url)
    tr = conn.transaction()
    await tr.start()
    try:
        # Clean slate WITHIN the rolled-back transaction — never persisted.
        await conn.execute("truncate public.embedding_config")

        # 1. Seed the stamp (bulk-reindex helper) → exactly one row, round-trips.
        await stamp_embedding_config(conn, EXPECTED_MODEL, EXPECTED_DIM)
        rows = await conn.fetch("select model, dim from public.embedding_config")
        _check(len(rows) == 1, f"expected exactly one stamp row, got {len(rows)}")
        _check(rows[0]["model"] == EXPECTED_MODEL, f"model mismatch: {rows[0]['model']!r}")
        _check(rows[0]["dim"] == EXPECTED_DIM, f"dim mismatch: {rows[0]['dim']}")

        # 2. The stamp dim must agree with the actual chunks.embedding column dim.
        col_dim = await _column_dim(conn)
        _check(
            col_dim == EXPECTED_DIM and rows[0]["dim"] == col_dim,
            f"stamp dim {rows[0]['dim']} must equal chunks.embedding column dim {col_dim}",
        )

        # 3. Insert-if-absent (the production ingest path) must NOT overwrite a
        #    model drift — the stamp stays as first written so US-027 can detect
        #    the divergence. This mirrors the endpoint's ON CONFLICT DO NOTHING.
        await conn.execute(
            """
            insert into public.embedding_config (singleton, model, dim)
            values (true, 'text-embedding-ada-002', 1536)
            on conflict (singleton) do nothing
            """
        )
        after = await conn.fetch("select model from public.embedding_config")
        _check(len(after) == 1, "insert-if-absent must not add a second row")
        _check(
            after[0]["model"] == EXPECTED_MODEL,
            f"insert-if-absent must not rewrite the stamp, got {after[0]['model']!r}",
        )

        # 4. Bulk re-index (the seeder do-update path) DOES overwrite — and still
        #    leaves exactly one row.
        await stamp_embedding_config(conn, "text-embedding-3-large", 3072)
        reindexed = await conn.fetch("select model, dim from public.embedding_config")
        _check(len(reindexed) == 1, "re-index must keep exactly one row")
        _check(
            reindexed[0]["model"] == "text-embedding-3-large" and reindexed[0]["dim"] == 3072,
            f"re-index should overwrite the stamp, got {reindexed[0]['model']!r}/{reindexed[0]['dim']}",
        )

        # 5. The singleton CHECK makes a literal second row impossible. Run inside
        #    a savepoint so the expected abort doesn't kill the outer transaction.
        violated = False
        try:
            async with conn.transaction():
                await conn.execute(
                    "insert into public.embedding_config (singleton, model, dim) "
                    "values (false, 'x', 1)"
                )
        except asyncpg.CheckViolationError:
            violated = True
        _check(violated, "singleton=false must violate the embedding_config_singleton CHECK")

        print(
            "db checks passed: single-row invariant, column-dim agreement, "
            "insert-if-absent no-overwrite, seeder overwrite, singleton CHECK"
        )
    finally:
        await tr.rollback()
        await conn.close()


async def _optional_real_seed_check() -> None:
    """The PRD validation test taken literally: run the real corpus seeder and
    assert the stamp it persisted is (get_embedding_model(), 1536) == column dim.
    Gated + opt-in because it embeds the whole corpus (costs OpenAI calls and
    truncates/reseeds the corpus tables)."""
    url = os.environ.get("CORPUS_SEED_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if os.environ.get("US026_RUN_SEED") != "1":
        print("SKIP real-seed check: set US026_RUN_SEED=1 to run the full corpus seed")
        return
    if not url or not os.environ.get("OPENAI_API_KEY"):
        print("SKIP real-seed check: needs DATABASE_URL + OPENAI_API_KEY")
        return

    from db_seed.corpus_seed import seed

    await seed()
    conn = await asyncpg.connect(url)
    try:
        rows = await conn.fetch("select model, dim from public.embedding_config")
        col_dim = await _column_dim(conn)
        _check(len(rows) == 1, f"after seed, expected one stamp row, got {len(rows)}")
        _check(
            rows[0]["model"] == get_embedding_model(),
            f"seeded stamp model {rows[0]['model']!r} != configured {get_embedding_model()!r}",
        )
        _check(
            rows[0]["dim"] == EXPECTED_DIM == col_dim,
            f"seeded stamp dim {rows[0]['dim']} must equal column dim {col_dim}",
        )
        print(f"real-seed check passed: stamp = ({rows[0]['model']!r}, {rows[0]['dim']})")
    finally:
        await conn.close()


async def main() -> None:
    _offline_checks()
    await _db_checks()
    await _optional_real_seed_check()


if __name__ == "__main__":
    asyncio.run(main())
