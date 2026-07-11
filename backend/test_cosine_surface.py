"""US-046 validation test: pre-fusion raw cosine on retrieval results.

Asserts the ADR-0003 plumbing the escalation retrieval gate (US-047) depends
on: every `SearchDocumentsResult` carries `cosine_similarity` — a calibrated
`[0,1]` vector cosine kept *separate* from `similarity` so the RRF rank artifact
never gets mistaken for a cosine.

Drives the **real** `search_documents` / `keyword_search` / `hybrid_search`
through an `httpx.MockTransport` (the Supabase RPCs are stubbed; no DB) plus a
fake embeddings client (no network/secrets), and exercises the pure `_rrf_fuse`
directly — so this runs anywhere, like `test_chat_mode_default.py`.

Covers:
  * vector rows set `cosine_similarity == similarity` (both are the cosine);
  * keyword-only rows leave it `None` (ts_rank is not a cosine);
  * fusion overwrites `similarity` with the small RRF score but PRESERVES the
    vector cosine (the ADR-0003 plumbing bug the AC's failure-indicator names);
  * a fused chunk seen only in the keyword ranking has `cosine_similarity None`.

Run:
    python -m backend.test_cosine_surface
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from typing import Any, cast

import httpx
from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from retrieval import (  # noqa: E402
    SearchDocumentsResult,
    _rrf_fuse,
    get_rrf_k,
    hybrid_search,
    keyword_search,
    search_documents,
)

SUPABASE_URL = "http://supabase.test"


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


# --- fakes ----------------------------------------------------------------


class _FakeEmbeddings:
    async def create(self, model: str, input: list[str]) -> Any:  # noqa: A002
        # match_chunks is mocked, so the actual vector is irrelevant — we only
        # need the embed step to succeed and return one vector per input.
        data = [
            types.SimpleNamespace(index=i, embedding=[0.1, 0.2, 0.3])
            for i in range(len(input))
        ]
        return types.SimpleNamespace(data=data)


class _FakeOpenAI:
    embeddings = _FakeEmbeddings()


def _fake_client() -> AsyncOpenAI:
    return cast(AsyncOpenAI, _FakeOpenAI())


def _row(chunk_id: str, similarity: float, *, keyword: bool = False) -> dict[str, Any]:
    """A match_chunks / keyword_search RPC row (no `cosine_similarity` column —
    the RPCs don't emit one; the Python layer derives it)."""
    return {
        "id": chunk_id,
        "document_id": f"doc-{chunk_id}",
        "chunk_index": 0,
        "content": f"content {chunk_id}",
        "similarity": similarity,
        "filename": f"{chunk_id}.txt",
        "granting_principal_id": None,
        "granting_principal_display": "owner" if not keyword else None,
    }


def _transport(
    match_rows: list[dict[str, Any]],
    keyword_rows: list[dict[str, Any]],
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/rpc/match_chunks"):
            return httpx.Response(200, json=match_rows)
        if path.endswith("/rpc/keyword_search"):
            return httpx.Response(200, json=keyword_rows)
        return httpx.Response(404, json={"message": f"unexpected path {path}"})

    return httpx.MockTransport(handler)


async def _client(
    match_rows: list[dict[str, Any]],
    keyword_rows: list[dict[str, Any]],
) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=_transport(match_rows, keyword_rows))


# --- tests ----------------------------------------------------------------


def test_field_defaults_none() -> None:
    """The new field is optional and defaults to None, so an RPC row that
    carries no `cosine_similarity` column constructs cleanly (and a keyword row
    therefore stays None unless the caller sets it)."""
    row = SearchDocumentsResult(**_row("a", 0.5))
    _check(
        row.cosine_similarity is None,
        f"cosine_similarity must default to None, got {row.cosine_similarity!r}",
    )
    explicit = SearchDocumentsResult(**_row("a", 0.5), cosine_similarity=0.5)
    _check(explicit.cosine_similarity == 0.5, "explicit cosine_similarity must round-trip")
    print("ok: cosine_similarity is an optional float|None field defaulting to None")


def test_search_documents_carries_vector_cosine() -> None:
    """Vector mode: `cosine_similarity` equals `similarity` on every row (both
    are the match_chunks cosine) and lands in [0,1]."""

    async def run() -> None:
        async with await _client([_row("a", 0.62), _row("b", 0.41)], []) as http:
            results = await search_documents(
                openai_client=_fake_client(),
                http=http,
                supabase_url=SUPABASE_URL,
                supabase_headers={},
                query="q",
            )
        _check(len(results) == 2, f"expected 2 rows, got {len(results)}")
        for r in results:
            _check(
                r.cosine_similarity == r.similarity,
                f"{r.id}: vector cosine must equal similarity "
                f"({r.cosine_similarity!r} != {r.similarity!r})",
            )
            _check(
                r.cosine_similarity is not None and 0.0 <= r.cosine_similarity <= 1.0,
                f"{r.id}: cosine must be a [0,1] float, got {r.cosine_similarity!r}",
            )

    asyncio.run(run())
    print("ok: search_documents sets cosine_similarity == similarity (both cosine, [0,1])")


def test_keyword_search_cosine_is_none() -> None:
    """Keyword mode: rows have no embedding, so `cosine_similarity` is None even
    though `similarity` (ts_rank_cd) is unbounded and > 1."""

    async def run() -> None:
        async with await _client([], [_row("x", 5.0, keyword=True)]) as http:
            results = await keyword_search(
                http=http,
                supabase_url=SUPABASE_URL,
                supabase_headers={},
                query="q",
            )
        _check(len(results) == 1, f"expected 1 row, got {len(results)}")
        r = results[0]
        _check(
            r.cosine_similarity is None,
            f"keyword row must carry cosine None, got {r.cosine_similarity!r}",
        )
        _check(r.similarity == 5.0, "keyword similarity (ts_rank) must pass through unchanged")

    asyncio.run(run())
    print("ok: keyword_search leaves cosine_similarity None (ts_rank is not a cosine)")


def test_hybrid_fusion_preserves_vector_cosine() -> None:
    """The core ADR-0003 plumbing: hybrid overwrites `similarity` with the small
    RRF score but the raw vector cosine survives — and a keyword-only chunk has
    cosine None. This is the AC's failure indicator made executable."""

    async def run() -> None:
        # A: vector-only hit; B: in both rankings; C: keyword-only hit.
        match_rows = [_row("A", 0.62), _row("B", 0.41)]
        keyword_rows = [_row("B", 5.0, keyword=True), _row("C", 3.0, keyword=True)]
        async with await _client(match_rows, keyword_rows) as http:
            fused = await hybrid_search(
                openai_client=_fake_client(),
                http=http,
                supabase_url=SUPABASE_URL,
                supabase_headers={},
                query="q",
            )
        by_id = {r.id: r for r in fused}
        _check(set(by_id) == {"A", "B", "C"}, f"expected A,B,C fused, got {set(by_id)}")

        rrf_ceiling = 2.0 / (get_rrf_k() + 1) + 1e-9
        for r in fused:
            _check(
                0.0 < r.similarity <= rrf_ceiling,
                f"{r.id}: similarity must be the small RRF score (<= {rrf_ceiling:.4f}), "
                f"got {r.similarity}",
            )

        _check(
            by_id["A"].cosine_similarity == 0.62,
            f"A (vector-only) must keep its cosine 0.62, got {by_id['A'].cosine_similarity!r}",
        )
        _check(
            by_id["B"].cosine_similarity == 0.41,
            f"B (in both) must keep the vector-side cosine 0.41, got "
            f"{by_id['B'].cosine_similarity!r}",
        )
        _check(
            by_id["C"].cosine_similarity is None,
            f"C (keyword-only) must have cosine None, got {by_id['C'].cosine_similarity!r}",
        )
        # The plumbing-bug guard: cosine must NOT have been clobbered by the RRF score.
        for cid in ("A", "B"):
            _check(
                by_id[cid].cosine_similarity != by_id[cid].similarity,
                f"{cid}: cosine_similarity equals the RRF score — the raw cosine was lost",
            )

    asyncio.run(run())
    print("ok: hybrid fusion preserves the vector cosine; keyword-only chunk stays None")


def test_rrf_fuse_unit_is_pure() -> None:
    """Direct, mock-free `_rrf_fuse` exercise — the exact site of the plumbing
    bug. Vector ranking is passed first, so its cosine wins for shared chunks."""
    vector = [
        SearchDocumentsResult(**_row("A", 0.62), cosine_similarity=0.62),
        SearchDocumentsResult(**_row("B", 0.41), cosine_similarity=0.41),
    ]
    keyword = [
        SearchDocumentsResult(**_row("B", 5.0, keyword=True)),
        SearchDocumentsResult(**_row("C", 3.0, keyword=True)),
    ]
    fused = _rrf_fuse([vector, keyword], top_k=10, k=60)
    by_id = {r.id: r for r in fused}
    _check(by_id["A"].cosine_similarity == 0.62, "A cosine lost in fusion")
    _check(by_id["B"].cosine_similarity == 0.41, "B vector cosine lost in fusion")
    _check(by_id["C"].cosine_similarity is None, "C (keyword-only) should be cosine None")
    # Determinism: identical inputs → identical decisions.
    again = _rrf_fuse([vector, keyword], top_k=10, k=60)
    _check(
        [(r.id, r.similarity, r.cosine_similarity) for r in fused]
        == [(r.id, r.similarity, r.cosine_similarity) for r in again],
        "_rrf_fuse must be deterministic",
    )
    print("ok: _rrf_fuse preserves the vector cosine deterministically (pure path)")


def test_rrf_fuse_default_weights_unchanged() -> None:
    """US-115: `_rrf_fuse` grew an optional `weights` kwarg. Its default (absent /
    None) must leave the cosine-surface path byte-identical to before — the same
    scores AND the same preserved cosines — so this US-046 pin still holds. An
    explicit equal-weight `(0.5, 0.5)` collapses to the same values (2*0.5==1)."""
    vector = [
        SearchDocumentsResult(**_row("A", 0.62), cosine_similarity=0.62),
        SearchDocumentsResult(**_row("B", 0.41), cosine_similarity=0.41),
    ]
    keyword = [
        SearchDocumentsResult(**_row("B", 5.0, keyword=True)),
        SearchDocumentsResult(**_row("C", 3.0, keyword=True)),
    ]

    def sig(rows: list[SearchDocumentsResult]) -> list[tuple[str, float, float | None]]:
        return [(r.id, r.similarity, r.cosine_similarity) for r in rows]

    default = _rrf_fuse([vector, keyword], top_k=10, k=60)
    none_kw = _rrf_fuse([vector, keyword], top_k=10, k=60, weights=None)
    equal = _rrf_fuse([vector, keyword], top_k=10, k=60, weights=(0.5, 0.5))
    _check(sig(default) == sig(none_kw), "explicit weights=None diverged from the default call")
    _check(sig(default) == sig(equal), "weights=(0.5, 0.5) diverged from the default 1/(k+r) path")
    # Cosine surface still intact under the default kwarg.
    by_id = {r.id: r for r in default}
    _check(by_id["A"].cosine_similarity == 0.62, "A cosine lost with the new kwarg default")
    _check(by_id["B"].cosine_similarity == 0.41, "B vector cosine lost with the new kwarg default")
    _check(by_id["C"].cosine_similarity is None, "C (keyword-only) should stay cosine None")
    print("ok: _rrf_fuse default/None/equal-weight paths are byte-identical (US-115 seam inert)")


def main() -> int:
    tests = [
        test_field_defaults_none,
        test_search_documents_carries_vector_cosine,
        test_keyword_search_cosine_is_none,
        test_hybrid_fusion_preserves_vector_cosine,
        test_rrf_fuse_unit_is_pure,
        test_rrf_fuse_default_weights_unchanged,
    ]
    for t in tests:
        t()
    print(f"\nPASS: {len(tests)} cosine-surface (US-046) test groups")
    return 0


if __name__ == "__main__":
    sys.exit(main())
