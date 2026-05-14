"""US-032 determinism test.

Two parts:

1. **Offline pure-function checks** that always run:
   - `filename_slug` strips extensions and replaces non-alphanumerics.
   - `stable_id` round-trips into the expected shape.
   - `document_uuid` / `chunk_uuid` are deterministic for fixed inputs.
   - `load_corpus()` finds 5+ markdown files.
   - `chunk_text` produces non-empty chunks for every corpus file.

2. **DB roundtrip determinism check** that runs when
   `CORPUS_SEED_DATABASE_URL` (or `DATABASE_URL`) is set and `OPENAI_API_KEY`
   is available:
   - Run `seed()` → snapshot `(stable_id, md5(content))` ordered by stable_id.
   - Run `seed()` again → snapshot.
   - Snapshots must be byte-identical (the PRD acceptance criterion).

Run:
    python -m db_seed.test_corpus_seed
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg

# Import via the package path so `python -m db_seed.test_corpus_seed` works
# without path hackery.
from db_seed.corpus_seed import (
    chunk_uuid,
    document_uuid,
    filename_slug,
    load_corpus,
    seed,
    stable_id,
)

# Reuse the production chunker for the offline non-empty check.
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from chunking import chunk_text  # noqa: E402


def _offline_checks() -> None:
    assert filename_slug("Refund Policy.md") == "refund-policy"
    assert filename_slug("shipping-faq.md") == "shipping-faq"
    assert filename_slug("Some_File NAME.md") == "some-file-name"

    assert stable_id("refund-policy", 0) == "refund-policy:0"
    assert stable_id("warranty-terms", 12) == "warranty-terms:12"

    assert document_uuid("refund-policy") == document_uuid("refund-policy"), (
        "document_uuid must be deterministic for a given slug"
    )
    assert document_uuid("a") != document_uuid("b"), (
        "document_uuid must differ between distinct slugs"
    )
    assert chunk_uuid("refund-policy", 0) == chunk_uuid("refund-policy", 0)
    assert chunk_uuid("refund-policy", 0) != chunk_uuid("refund-policy", 1)

    corpus = load_corpus()
    assert len(corpus) >= 5, f"expected >=5 corpus files, got {len(corpus)}"
    for filename, content in corpus:
        chunks = chunk_text(content)
        assert chunks, f"chunking produced 0 chunks for {filename}"

    print(f"offline checks passed ({len(corpus)} corpus files)")


async def _snapshot(url: str) -> list[tuple[str, str]]:
    conn = await asyncpg.connect(url)
    try:
        rows = await conn.fetch(
            """
            select stable_id, md5(content) as content_hash
              from public.chunks
             where stable_id is not null
             order by stable_id
            """
        )
        return [(r["stable_id"], r["content_hash"]) for r in rows]
    finally:
        await conn.close()


async def _db_roundtrip() -> None:
    url = (
        os.environ.get("CORPUS_SEED_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
    )
    if not url:
        print("SKIP db roundtrip: CORPUS_SEED_DATABASE_URL/DATABASE_URL unset")
        return
    if not os.environ.get("OPENAI_API_KEY"):
        print("SKIP db roundtrip: OPENAI_API_KEY unset")
        return

    counts_a = await seed()
    snap_a = await _snapshot(url)
    counts_b = await seed()
    snap_b = await _snapshot(url)

    assert counts_a == counts_b, f"counts differ across runs: {counts_a} vs {counts_b}"
    assert snap_a == snap_b, (
        f"snapshot differs across runs: {len(snap_a)} vs {len(snap_b)} rows"
    )
    assert snap_a, "snapshot is empty — corpus may not have inserted any chunks"

    # Shape sanity: every stable_id is `{slug}:{int}`.
    for sid, _ in snap_a:
        slug, sep, idx = sid.partition(":")
        assert sep and slug and idx.isdigit(), f"bad stable_id shape: {sid!r}"

    print(
        f"db roundtrip OK: {counts_a['documents']} documents, "
        f"{counts_a['chunks']} chunks, byte-identical across two runs"
    )


async def main() -> None:
    _offline_checks()
    await _db_roundtrip()


if __name__ == "__main__":
    asyncio.run(main())
