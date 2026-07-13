"""US-117 validation test: --reranker bake-off wiring in the eval runner.

Pure / offline — no DB, no network. Exercises `evals.retrieval.runner.run_query`
with the search backends stubbed, pinning the two invariants a reviewer cannot
see from the diff alone:

  * `--reranker none` (NullReranker) AND the no-flag path (`reranker=None`) are a
    BYTE-FOR-BYTE pass-through of the pre-US-117 behaviour: the candidate pool
    stays at `top_k` (no wider fetch), and the retrieved rows are returned
    untouched and identical to each other.
  * a real reranker mirrors `backend/main.py::_retrieve_for_agent`: it widens the
    pool to `max(RERANK_INPUT_K, top_k)`, reorders, and trims back to `top_k`.

Run:
    python -m evals.retrieval.test_us117_reranker
"""

from __future__ import annotations

import asyncio
import os
import sys

from evals.retrieval import runner
from reranking import NullReranker, get_rerank_input_k

SearchDocumentsResult = runner.SearchDocumentsResult
TOP_K = runner.TOP_K


def _mk(i: int) -> "SearchDocumentsResult":
    """A minimal retrieval row with a stable, order-revealing id."""
    return SearchDocumentsResult(
        id=f"chunk-{i:02d}",
        document_id="doc-1",
        chunk_index=i,
        content=f"content number {i}",
        similarity=1.0 - i / 100.0,
        filename="f.md",
        cosine_similarity=1.0 - i / 100.0,
        granting_principal_id=None,
        granting_principal_display=None,
    )


class _StubSearch:
    """Records the top_k it was asked for and returns that many ordered rows."""

    def __init__(self) -> None:
        self.last_top_k: int | None = None

    async def __call__(self, *, top_k: int, **_: object) -> list["SearchDocumentsResult"]:
        self.last_top_k = top_k
        return [_mk(i) for i in range(top_k)]


class _ReverseReranker:
    """A real (non-null) reranker: reverses input order, trims to top_k."""

    name = "reverse"

    async def rerank(self, query, candidates, top_k):  # noqa: ANN001
        return list(reversed(candidates))[:top_k]


async def _run(reranker):
    stub = _StubSearch()
    orig = runner.hybrid_search
    runner.hybrid_search = stub  # type: ignore[assignment]
    try:
        out = await runner.run_query(
            "hybrid",
            openai_client=None,
            http=None,
            supabase_url="http://x",
            supabase_headers={},
            question="q",
            reranker=reranker,
        )
    finally:
        runner.hybrid_search = orig  # type: ignore[assignment]
    return out, stub.last_top_k


def test_none_flag_and_no_flag_are_identical_passthrough() -> None:
    """The core acceptance criterion: `--reranker none` == no-flag, byte-for-byte."""
    no_flag_out, no_flag_pool = asyncio.run(_run(None))
    none_out, none_pool = asyncio.run(_run(NullReranker()))

    # 1. Neither widens the candidate pool — the pre-US-117 fetch size.
    assert no_flag_pool == TOP_K, f"no-flag fetched {no_flag_pool}, want {TOP_K}"
    assert none_pool == TOP_K, f"--reranker none fetched {none_pool}, want {TOP_K}"

    # 2. Identical output, in identical order, untouched by any reranker.
    no_flag_ids = [r.id for r in no_flag_out]
    none_ids = [r.id for r in none_out]
    assert no_flag_ids == none_ids, (
        f"pass-through mismatch: no-flag {no_flag_ids} != none {none_ids}"
    )
    # 3. Order is the raw retrieval order (0..TOP_K-1), nothing reordered.
    assert none_ids == [f"chunk-{i:02d}" for i in range(TOP_K)], none_ids
    # 4. model dumps are byte-identical (scores etc. untouched).
    assert [r.model_dump() for r in no_flag_out] == [r.model_dump() for r in none_out]
    print(f"ok: --reranker none is a byte-for-byte pass-through — pool={none_pool}, ids={none_ids}")


def test_real_reranker_widens_pool_reorders_and_trims() -> None:
    """A non-null reranker mirrors _retrieve_for_agent: widen → rerank → trim."""
    out, pool = asyncio.run(_run(_ReverseReranker()))
    expected_pool = max(get_rerank_input_k(), TOP_K)

    assert pool == expected_pool, f"reranker fetched {pool}, want widened {expected_pool}"
    assert len(out) == TOP_K, f"expected trim to {TOP_K}, got {len(out)}"
    # Reverse reranker over [0..pool-1] then trimmed → highest indices first.
    got = [r.chunk_index for r in out]
    want = list(reversed(range(pool)))[:TOP_K]
    assert got == want, f"reranked order {got} != expected {want}"
    print(f"ok: real reranker widened pool {TOP_K}->{pool}, reordered, trimmed to {len(out)} — order={got}")


def main() -> None:
    test_none_flag_and_no_flag_are_identical_passthrough()
    test_real_reranker_widens_pool_reorders_and_trims()
    print("\nPASS: 2 US-117 reranker-wiring test groups")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "backend"))
    main()
