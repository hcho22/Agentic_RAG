"""US-115 validation test: the deterministic-alpha fusion seam.

Pins the pure, unwired half of adaptive fusion — the seam that must exist and be
verified before US-116 changes any live behavior (core invariant 9: ship the
seam before the call-site). Two surfaces are covered:

  * `predict_alpha(query)` — a pure, deterministic vector-leg weight in
    [ALPHA_MIN, ALPHA_MAX]. Neutral prose returns exactly 0.5; identifier-dense
    queries slide toward the lexical leg; the clamp is never violated.
  * `_rrf_fuse(..., weights=...)` — weighted RRF where `(0.5, 0.5)` and `None`
    reproduce the legacy `1/(k+r)` byte-for-byte, and unequal weights re-order
    fused results WITHOUT ever mutating a per-row `cosine_similarity` (US-046).

No DB, no network, no secrets — like `test_cosine_surface.py`, it runs anywhere.

Run:
    python -m backend.test_alpha_fusion
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from retrieval import (  # noqa: E402
    ALPHA_MAX,
    ALPHA_MIN,
    ALPHA_NEUTRAL,
    SearchDocumentsResult,
    _rrf_fuse,
    predict_alpha,
)


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _row(chunk_id: str, similarity: float, *, keyword: bool = False) -> dict[str, Any]:
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


# --- predict_alpha: purity & determinism ----------------------------------


def test_predict_alpha_is_pure_and_deterministic() -> None:
    """Same input → same output, every time, with no observable side effects."""
    queries = [
        "how do I reset my password",
        'the exact phrase "connection refused" appears',
        "WEBHOOK_RETRY_MAX and ERR-4102 in one query CAT-1234",
        "",
        "   ",
        "v2.1 getUserById 5xx retry_max",
    ]
    for q in queries:
        first = predict_alpha(q)
        for _ in range(5):
            _check(
                predict_alpha(q) == first,
                f"predict_alpha({q!r}) is non-deterministic ({predict_alpha(q)} != {first})",
            )
        _check(isinstance(first, float), f"predict_alpha must return a float, got {type(first)}")
    print("ok: predict_alpha is deterministic and pure across varied queries")


def test_predict_alpha_neutral_prose_is_exactly_half() -> None:
    """A plain natural-language question carries no lexical cue → exactly 0.5."""
    for q in (
        "how do I reset my password",
        "what is the recommended way to configure retries",
        "tell me about the onboarding process for new customers",
        # contractions carry two apostrophes but are ordinary prose, not a
        # quoted phrase — they must stay exactly neutral.
        "I don't know why it's failing",
        # common all-caps prose acronyms are not identifier-shaped tokens.
        "what is the API rate limit",
    ):
        a = predict_alpha(q)
        _check(
            a == ALPHA_NEUTRAL,
            f"neutral prose must map to exactly {ALPHA_NEUTRAL}, got {a} for {q!r}",
        )
    print(f"ok: neutral prose returns exactly {ALPHA_NEUTRAL}")


def test_predict_alpha_empty_query_is_neutral() -> None:
    """Empty / whitespace-only queries have no tokens → the neutral midpoint
    (no division-by-zero, no bias)."""
    for q in ("", "   ", "\t\n"):
        a = predict_alpha(q)
        _check(a == ALPHA_NEUTRAL, f"empty query must be neutral {ALPHA_NEUTRAL}, got {a} for {q!r}")
    print(f"ok: empty/whitespace query returns exactly {ALPHA_NEUTRAL}")


def test_predict_alpha_all_identifier_favors_lexical() -> None:
    """An all-identifier query tilts fusion toward the lexical leg: alpha drops
    strictly below neutral and pins near the lower clamp."""
    for q in (
        "ERR-4102 WEBHOOK_RETRY_MAX",
        "CAT-1234 retry_max getUserById v2.1",
    ):
        a = predict_alpha(q)
        _check(
            a < ALPHA_NEUTRAL,
            f"identifier-dense query must weight lexical up (alpha < {ALPHA_NEUTRAL}), got {a} for {q!r}",
        )
        _check(
            a <= ALPHA_MIN + 0.05,
            f"all-identifier query should pin near {ALPHA_MIN}, got {a} for {q!r}",
        )
    print("ok: all-identifier queries weight the lexical leg up (alpha near ALPHA_MIN)")


def test_predict_alpha_quoted_phrase_favors_lexical() -> None:
    """A quoted literal phrase is a lexical cue even amid prose → alpha < 0.5,
    and strictly below the same sentence without the quotes."""
    quoted = 'why does the log say "connection refused" on startup'
    plain = "why does the log say connection refused on startup"
    aq = predict_alpha(quoted)
    ap = predict_alpha(plain)
    _check(aq < ALPHA_NEUTRAL, f"quoted-phrase query must weight lexical up, got {aq}")
    _check(
        aq < ap,
        f"quoting a phrase must lower alpha vs the unquoted sentence ({aq} !< {ap})",
    )
    print("ok: quoted-phrase presence lowers alpha (favors the lexical leg)")


def test_predict_alpha_respects_clamp_bounds() -> None:
    """Across neutral, mixed, and saturated-lexical queries alpha never leaves
    [ALPHA_MIN, ALPHA_MAX], and a maximally identifier+digit+quoted query does
    not undershoot the lower bound."""
    saturated = 'ERR-4102 WEBHOOK_RETRY_MAX CAT-1234 "5xx-timeout" v2.1 retry_max getUserById'
    corpus = [
        "",
        "plain prose question about the product",
        "mixed WEBHOOK_RETRY_MAX with some ordinary words here",
        saturated,
    ]
    for q in corpus:
        a = predict_alpha(q)
        _check(
            ALPHA_MIN <= a <= ALPHA_MAX,
            f"alpha {a} out of clamp [{ALPHA_MIN}, {ALPHA_MAX}] for {q!r}",
        )
    _check(
        predict_alpha(saturated) == ALPHA_MIN,
        f"a saturated-lexical query should pin at {ALPHA_MIN}, got {predict_alpha(saturated)}",
    )
    print(f"ok: alpha stays within [{ALPHA_MIN}, {ALPHA_MAX}] and pins at {ALPHA_MIN} when saturated")


# --- weighted _rrf_fuse ----------------------------------------------------


def _mk(chunk_id: str, sim: float, *, keyword: bool = False) -> SearchDocumentsResult:
    row = SearchDocumentsResult(**_row(chunk_id, sim, keyword=keyword))
    if not keyword:
        return row.model_copy(update={"cosine_similarity": sim})
    return row


def test_rrf_fuse_equal_weights_match_legacy_byte_for_byte() -> None:
    """weights=(0.5, 0.5) and weights=None must both reproduce the unweighted
    1/(k+r) scores exactly — the legacy-equivalence guarantee US-116 relies on
    for its `HYBRID_FUSION_ALPHA=0.5` escape hatch."""
    vector = [_mk("A", 0.62), _mk("B", 0.41)]
    keyword = [_mk("B", 5.0, keyword=True), _mk("C", 3.0, keyword=True)]

    legacy = _rrf_fuse([vector, keyword], top_k=10, k=60)
    none_kw = _rrf_fuse([vector, keyword], top_k=10, k=60, weights=None)
    equal = _rrf_fuse([vector, keyword], top_k=10, k=60, weights=(0.5, 0.5))

    def sig(rows: list[SearchDocumentsResult]) -> list[tuple[str, float, float | None]]:
        return [(r.id, r.similarity, r.cosine_similarity) for r in rows]

    _check(sig(legacy) == sig(none_kw), "weights=None diverged from the no-kwarg legacy call")
    _check(sig(legacy) == sig(equal), "weights=(0.5, 0.5) diverged from the legacy 1/(k+r) scores")
    print("ok: weights=None and (0.5, 0.5) reproduce legacy RRF scores byte-for-byte")


def test_rrf_fuse_unequal_weights_shift_ranking() -> None:
    """Unequal weights actually tilt the fused ordering — a top-ranked vector-only
    hit and a top-ranked keyword-only hit swap places as the weight moves. Sanity
    that the seam is live, not a no-op. Chunks are disjoint per leg so each item's
    score comes from a single weighted ranking."""
    vector = [_mk("X", 0.9), _mk("A", 0.5)]  # vector-only: X rank 1, A rank 2
    keyword = [_mk("C", 5.0, keyword=True), _mk("D", 3.0, keyword=True)]  # keyword-only

    heavy_vector = _rrf_fuse([vector, keyword], top_k=10, k=60, weights=(0.9, 0.1))
    heavy_keyword = _rrf_fuse([vector, keyword], top_k=10, k=60, weights=(0.1, 0.9))
    order_v = [r.id for r in heavy_vector]
    order_k = [r.id for r in heavy_keyword]
    _check(order_v != order_k, f"weights did not change ordering: {order_v} == {order_k}")
    _check(
        order_v.index("X") < order_v.index("C"),
        f"vector up-weight should lift X (vector rank 1) above C, got {order_v}",
    )
    _check(
        order_k.index("C") < order_k.index("X"),
        f"keyword up-weight should lift C (keyword rank 1) above X, got {order_k}",
    )
    print("ok: unequal weights re-order the fused result as expected")


def test_rrf_fuse_weights_preserve_cosine() -> None:
    """FR-5 / US-046: weighting is a ranking artifact only — per-row
    cosine_similarity is byte-identical under equal and unequal weights, and the
    keyword-only row stays None regardless."""
    vector = [_mk("A", 0.62), _mk("B", 0.41)]
    keyword = [_mk("B", 5.0, keyword=True), _mk("C", 3.0, keyword=True)]

    for weights in (None, (0.5, 0.5), (0.7, 0.3), (0.3, 0.7)):
        fused = _rrf_fuse([vector, keyword], top_k=10, k=60, weights=weights)
        by_id = {r.id: r for r in fused}
        _check(by_id["A"].cosine_similarity == 0.62, f"A cosine changed under weights={weights}")
        _check(by_id["B"].cosine_similarity == 0.41, f"B cosine changed under weights={weights}")
        _check(
            by_id["C"].cosine_similarity is None,
            f"C (keyword-only) cosine not None under weights={weights}",
        )
        # The RRF score must not have leaked into the cosine field.
        for cid in ("A", "B"):
            _check(
                by_id[cid].cosine_similarity != by_id[cid].similarity,
                f"{cid}: cosine equals RRF score under weights={weights} — cosine lost",
            )
    print("ok: fusion weights never mutate per-row cosine_similarity (US-046)")


def test_rrf_fuse_weights_length_must_match() -> None:
    """A weights tuple whose length disagrees with the ranking count is a
    programming error, not silently ignored."""
    vector = [_mk("A", 0.62)]
    keyword = [_mk("B", 5.0, keyword=True)]
    try:
        _rrf_fuse([vector, keyword], top_k=10, k=60, weights=(0.5,))
    except ValueError:
        print("ok: mismatched weights length raises ValueError")
        return
    raise AssertionError("expected ValueError for mismatched weights length")


def main() -> int:
    tests = [
        test_predict_alpha_is_pure_and_deterministic,
        test_predict_alpha_neutral_prose_is_exactly_half,
        test_predict_alpha_empty_query_is_neutral,
        test_predict_alpha_all_identifier_favors_lexical,
        test_predict_alpha_quoted_phrase_favors_lexical,
        test_predict_alpha_respects_clamp_bounds,
        test_rrf_fuse_equal_weights_match_legacy_byte_for_byte,
        test_rrf_fuse_unequal_weights_shift_ranking,
        test_rrf_fuse_weights_preserve_cosine,
        test_rrf_fuse_weights_length_must_match,
    ]
    for t in tests:
        t()
    print(f"\nPASS: {len(tests)} alpha-fusion (US-115) test groups")
    return 0


if __name__ == "__main__":
    sys.exit(main())
