"""US-116 validation test: adaptive alpha wired into hybrid_search.

US-115 shipped the seam (pure `predict_alpha` + weighted `_rrf_fuse`) inert;
this pins the live wiring US-116 adds:

  * `get_hybrid_fusion_alpha()` — the `HYBRID_FUSION_ALPHA` env knob. `auto`
    (default) selects per-query weighting; a fixed float in [0, 1] pins it;
    junk or out-of-range raises (mirrors `get_rrf_k()`).
  * `hybrid_search` — resolves the policy to a vector-leg weight and fuses with
    `weights=(alpha, 1 - alpha)`. `HYBRID_FUSION_ALPHA=0.5` reproduces legacy
    equal-weight RRF byte-for-byte (the ops escape hatch); a fixed non-0.5 float
    and an `auto` identifier-dense query both tilt the fused order toward the
    lexical leg.

The two retrieval legs are monkeypatched to canned disjoint rankings so the
fused ordering is a pure function of the weights — no DB, no network, no secrets,
like `test_alpha_fusion.py`. Runs anywhere.

Run:
    python -m backend.test_us116_alpha_wiring
"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import retrieval  # noqa: E402
from retrieval import (  # noqa: E402
    SearchDocumentsResult,
    _rrf_fuse,
    get_hybrid_fusion_alpha,
    get_rrf_k,
    predict_alpha,
)


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _mk(chunk_id: str, sim: float, *, keyword: bool = False) -> SearchDocumentsResult:
    row = SearchDocumentsResult(
        **{
            "id": chunk_id,
            "document_id": f"doc-{chunk_id}",
            "chunk_index": 0,
            "content": f"content {chunk_id}",
            "similarity": sim,
            "filename": f"{chunk_id}.txt",
            "granting_principal_id": None,
            "granting_principal_display": None if keyword else "owner",
        }
    )
    if not keyword:
        return row.model_copy(update={"cosine_similarity": sim})
    return row


@contextmanager
def _env(value: str | None) -> Iterator[None]:
    """Set (or clear) HYBRID_FUSION_ALPHA for the duration of the block."""
    key = "HYBRID_FUSION_ALPHA"
    old = os.environ.get(key)
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old


@contextmanager
def _legs(
    vector: list[SearchDocumentsResult], keyword: list[SearchDocumentsResult]
) -> Iterator[None]:
    """Monkeypatch both retrieval legs to canned rankings.

    hybrid_search resolves `search_documents` / `keyword_search` as module
    globals at call time, so reassigning them on the module intercepts the calls.
    """
    orig_sd = retrieval.search_documents
    orig_kw = retrieval.keyword_search

    async def fake_sd(**kwargs: Any) -> list[SearchDocumentsResult]:
        return list(vector)

    async def fake_kw(**kwargs: Any) -> list[SearchDocumentsResult]:
        return list(keyword)

    retrieval.search_documents = fake_sd  # type: ignore[assignment]
    retrieval.keyword_search = fake_kw  # type: ignore[assignment]
    try:
        yield
    finally:
        retrieval.search_documents = orig_sd  # type: ignore[assignment]
        retrieval.keyword_search = orig_kw  # type: ignore[assignment]


def _run_hybrid(
    query: str,
    vector: list[SearchDocumentsResult],
    keyword: list[SearchDocumentsResult],
) -> list[SearchDocumentsResult]:
    with _legs(vector, keyword):
        return asyncio.run(
            retrieval.hybrid_search(
                openai_client=None,  # type: ignore[arg-type]
                http=None,  # type: ignore[arg-type]
                supabase_url="",
                supabase_headers={},
                query=query,
                top_k=10,
            )
        )


def _sig(rows: list[SearchDocumentsResult]) -> list[tuple[str, float, float | None]]:
    return [(r.id, r.similarity, r.cosine_similarity) for r in rows]


# --- get_hybrid_fusion_alpha: env parsing ----------------------------------


def test_env_defaults_to_auto() -> None:
    with _env(None):
        _check(get_hybrid_fusion_alpha() == "auto", "unset env should default to 'auto'")
    with _env(""):
        _check(get_hybrid_fusion_alpha() == "auto", "empty env should default to 'auto'")
    for raw in ("auto", "AUTO", "  auto  "):
        with _env(raw):
            _check(get_hybrid_fusion_alpha() == "auto", f"{raw!r} should parse to 'auto'")
    print("ok: HYBRID_FUSION_ALPHA unset/blank/'auto' resolves to the auto policy")


def test_env_fixed_float_in_range() -> None:
    for raw, expected in (("0.5", 0.5), ("0", 0.0), ("1", 1.0), ("0.3", 0.3), (" 0.7 ", 0.7)):
        with _env(raw):
            got = get_hybrid_fusion_alpha()
            _check(got == expected, f"{raw!r} should parse to {expected}, got {got!r}")
    print("ok: HYBRID_FUSION_ALPHA accepts a fixed float in [0, 1]")


def test_env_rejects_junk_and_out_of_range() -> None:
    for raw in ("high", "0.5x", "auto0", "1.5", "-0.1", "2"):
        with _env(raw):
            try:
                get_hybrid_fusion_alpha()
            except ValueError:
                continue
            raise AssertionError(f"HYBRID_FUSION_ALPHA={raw!r} should have raised ValueError")
    print("ok: HYBRID_FUSION_ALPHA rejects junk and out-of-range floats (fail-closed)")


# --- hybrid_search: the wiring ---------------------------------------------


def test_fixed_alpha_tilts_fused_order() -> None:
    """A fixed HYBRID_FUSION_ALPHA float reaches _rrf_fuse as weights: a
    vector-only top hit and a keyword-only top hit swap as alpha crosses 0.5."""
    vector = [_mk("X", 0.9)]  # vector-only top hit
    keyword = [_mk("C", 5.0, keyword=True)]  # keyword-only top hit

    with _env("0.9"):
        heavy_vector = [r.id for r in _run_hybrid("q", vector, keyword)]
    with _env("0.1"):
        heavy_keyword = [r.id for r in _run_hybrid("q", vector, keyword)]

    _check(
        heavy_vector.index("X") < heavy_vector.index("C"),
        f"alpha=0.9 (vector up) should rank X above C, got {heavy_vector}",
    )
    _check(
        heavy_keyword.index("C") < heavy_keyword.index("X"),
        f"alpha=0.1 (keyword up) should rank C above X, got {heavy_keyword}",
    )
    print("ok: fixed HYBRID_FUSION_ALPHA float tilts the fused order via weights")


def test_alpha_half_reproduces_legacy_rrf() -> None:
    """FR-6: HYBRID_FUSION_ALPHA=0.5 reproduces the legacy unweighted fusion
    byte-for-byte — the escape hatch the US-116 validation step relies on."""
    vector = [_mk("A", 0.62), _mk("B", 0.41)]
    keyword = [_mk("B", 5.0, keyword=True), _mk("C", 3.0, keyword=True)]

    legacy = _rrf_fuse([vector, keyword], top_k=10, k=get_rrf_k(), weights=None)
    with _env("0.5"):
        pinned = _run_hybrid("anything", vector, keyword)

    _check(
        _sig(pinned) == _sig(legacy),
        f"alpha=0.5 diverged from legacy RRF:\n  {_sig(pinned)}\n  {_sig(legacy)}",
    )
    print("ok: HYBRID_FUSION_ALPHA=0.5 reproduces legacy equal-weight RRF exactly")


def test_auto_identifier_query_lifts_lexical_leg() -> None:
    """Under the default auto policy, an identifier-dense query yields alpha < 0.5
    (predict_alpha), tilting fusion toward the lexical leg so a keyword-only hit
    outranks an otherwise-symmetric vector-only hit."""
    query = "ERR-4102 WEBHOOK_RETRY_MAX"
    _check(predict_alpha(query) < 0.5, "sanity: identifier query should give alpha < 0.5")

    vector = [_mk("X", 0.9)]
    keyword = [_mk("C", 5.0, keyword=True)]
    with _env(None):  # auto
        order = [r.id for r in _run_hybrid(query, vector, keyword)]

    _check(
        order.index("C") < order.index("X"),
        f"auto + identifier query should lift the keyword-only C above X, got {order}",
    )
    print("ok: auto policy lifts the lexical leg on identifier-dense queries")


def test_auto_neutral_prose_is_legacy() -> None:
    """Neutral prose maps to alpha exactly 0.5, so the auto policy must produce
    the legacy fusion byte-for-byte — adaptive fusion is inert on prose."""
    query = "please tell me about the refund and return process"
    _check(predict_alpha(query) == 0.5, "sanity: neutral prose should give alpha == 0.5")

    vector = [_mk("A", 0.62), _mk("B", 0.41)]
    keyword = [_mk("B", 5.0, keyword=True), _mk("C", 3.0, keyword=True)]
    legacy = _rrf_fuse([vector, keyword], top_k=10, k=get_rrf_k(), weights=None)
    with _env(None):  # auto
        auto = _run_hybrid(query, vector, keyword)

    _check(
        _sig(auto) == _sig(legacy),
        f"auto + neutral prose diverged from legacy RRF:\n  {_sig(auto)}\n  {_sig(legacy)}",
    )
    print("ok: auto policy is byte-identical to legacy fusion on neutral prose")


def main() -> int:
    tests = [
        test_env_defaults_to_auto,
        test_env_fixed_float_in_range,
        test_env_rejects_junk_and_out_of_range,
        test_fixed_alpha_tilts_fused_order,
        test_alpha_half_reproduces_legacy_rrf,
        test_auto_identifier_query_lifts_lexical_leg,
        test_auto_neutral_prose_is_legacy,
    ]
    for t in tests:
        t()
    print(f"\nPASS: {len(tests)} US-116 alpha-wiring test groups")
    return 0


if __name__ == "__main__":
    sys.exit(main())
