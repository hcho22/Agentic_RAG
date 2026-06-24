"""US-047 validation test: the cosine-defined retrieval gate (ADR-0003).

Exercises the pure `escalation.retrieval_gate` directly — no LLM, no reranker,
no DB/network/secrets — so it runs anywhere, like `test_chat_mode_default.py`.

Covers the PRD validation test:
  (a) top1 cosine 0.62 with 4 rows >= threshold -> strong;
  (b) top1 cosine 0.18 -> weak (top1 below tau_sim);
  (c) empty list -> weak;
with tau_sim=0.4, n_min=2, match_threshold=0.3, plus:
  * determinism — identical inputs always yield identical decisions;
  * the failure indicator: the gate reads `cosine_similarity`, never the RRF
    `similarity`, so a high-RRF / low-cosine row is weak and a low-RRF /
    high-cosine row is strong;
  * keyword-only rows (cosine None) are weak even in bulk;
  * the n_min boundary (top1 clears tau but too few rows clear threshold);
  * `>=` boundaries on both tau_sim and match_threshold.

Run:
    python -m backend.test_escalation_gate
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from escalation import retrieval_gate  # noqa: E402
from retrieval import SearchDocumentsResult  # noqa: E402

TAU = 0.4
N_MIN = 2
THRESH = 0.3


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _res(cosine: float | None, similarity: float | None = None) -> SearchDocumentsResult:
    """A result with `cosine_similarity` and `similarity` set independently, so
    a test can pin a high RRF `similarity` against a low cosine (and vice-versa)
    to prove the gate never reads the wrong field. `similarity` defaults to the
    cosine (the vector-mode invariant) when not overridden."""
    sim = similarity if similarity is not None else (cosine if cosine is not None else 0.0)
    return SearchDocumentsResult(
        id=f"c{cosine}-{sim}",
        document_id="d",
        chunk_index=0,
        content="x",
        similarity=sim,
        filename="f.txt",
        cosine_similarity=cosine,
    )


def test_prd_case_a_strong() -> None:
    """(a) top1 cosine 0.62 with 4 rows >= 0.3 -> strong; decision fields exact."""
    results = [_res(0.62), _res(0.55), _res(0.40), _res(0.31)]
    d = retrieval_gate(results, TAU, N_MIN, THRESH)
    _check(d.strong is True, f"(a) must be strong, got {d!r}")
    _check(d.top1_cosine == 0.62, f"(a) top1_cosine must be 0.62, got {d.top1_cosine!r}")
    _check(d.n_cleared == 4, f"(a) n_cleared must be 4, got {d.n_cleared}")
    _check(d.reason == "strong", f"(a) reason must be 'strong', got {d.reason!r}")
    print("ok: (a) strong retrieval -> strong=True, top1=0.62, n_cleared=4")


def test_prd_case_b_weak_top1() -> None:
    """(b) top1 cosine 0.18 (below tau_sim) -> weak, reason names the tau miss."""
    d = retrieval_gate([_res(0.18), _res(0.10)], TAU, N_MIN, THRESH)
    _check(d.strong is False, f"(b) must be weak, got {d!r}")
    _check(d.top1_cosine == 0.18, f"(b) top1_cosine must be 0.18, got {d.top1_cosine!r}")
    _check(d.n_cleared == 0, f"(b) n_cleared must be 0 (none clear 0.3), got {d.n_cleared}")
    _check("top1_cosine" in d.reason, f"(b) reason should name the tau miss, got {d.reason!r}")
    print("ok: (b) top1 below tau_sim -> weak")


def test_prd_case_c_empty() -> None:
    """(c) empty list -> weak, top1_cosine None, n_cleared 0."""
    d = retrieval_gate([], TAU, N_MIN, THRESH)
    _check(d.strong is False, f"(c) empty must be weak, got {d!r}")
    _check(d.top1_cosine is None, f"(c) empty top1_cosine must be None, got {d.top1_cosine!r}")
    _check(d.n_cleared == 0, f"(c) empty n_cleared must be 0, got {d.n_cleared}")
    _check(d.reason == "weak: empty_results", f"(c) reason got {d.reason!r}")
    print("ok: (c) empty results -> weak, top1 None")


def test_determinism() -> None:
    """Identical inputs always yield an identical decision (no randomness, no
    I/O). The decision is also frozen (immutable)."""
    results = [_res(0.62), _res(0.55), _res(0.40), _res(0.31)]
    first = retrieval_gate(results, TAU, N_MIN, THRESH)
    for _ in range(5):
        again = retrieval_gate(results, TAU, N_MIN, THRESH)
        _check(again == first, f"gate must be deterministic: {again!r} != {first!r}")
    # The decision is frozen — a consumer can't mutate it after the fact.
    # pydantic v2 raises ValidationError (a ValueError subclass) on assignment.
    try:
        first.strong = False  # type: ignore[misc]
    except ValueError:
        pass
    else:
        raise AssertionError("RetrievalGateDecision must be frozen (immutable)")
    print("ok: identical inputs -> identical decision; decision is frozen")


def test_reads_cosine_not_rrf_similarity() -> None:
    """The failure indicator made executable: a row with a high RRF `similarity`
    (0.99) but a low cosine (0.10) must be WEAK — the gate must not threshold the
    RRF score. Conversely a hybrid-shaped row (small RRF `similarity`, high
    cosine) must be STRONG."""
    high_rrf_low_cosine = [_res(0.10, similarity=0.99), _res(0.09, similarity=0.98)]
    weak = retrieval_gate(high_rrf_low_cosine, TAU, N_MIN, THRESH)
    _check(
        weak.strong is False,
        f"high-RRF/low-cosine must be weak (gate must read cosine, not similarity), got {weak!r}",
    )

    # Real hybrid shape: RRF `similarity` ~0.03 (small) but cosine high.
    low_rrf_high_cosine = [_res(0.70, similarity=0.033), _res(0.50, similarity=0.016)]
    strong = retrieval_gate(low_rrf_high_cosine, TAU, N_MIN, THRESH)
    _check(
        strong.strong is True,
        f"low-RRF/high-cosine must be strong (cosine clears tau), got {strong!r}",
    )
    print("ok: gate thresholds cosine_similarity, never the RRF similarity")


def test_keyword_only_rows_are_weak() -> None:
    """Keyword-only rows carry cosine None (US-046). Even a bulk of them is weak
    — no cosine to clear tau_sim — with the no-vector-cosine reason."""
    d = retrieval_gate([_res(None), _res(None), _res(None), _res(None)], TAU, N_MIN, THRESH)
    _check(d.strong is False, f"keyword-only must be weak, got {d!r}")
    _check(d.top1_cosine is None, f"keyword-only top1_cosine must be None, got {d.top1_cosine!r}")
    _check(d.n_cleared == 0, f"keyword-only n_cleared must be 0, got {d.n_cleared}")
    _check(d.reason == "weak: no_vector_cosine", f"reason got {d.reason!r}")

    # Mixed: one weak vector hit + keyword-only Nones — the None rows neither
    # count toward n_cleared nor supply top1.
    mixed = retrieval_gate([_res(None), _res(0.20), _res(None)], TAU, N_MIN, THRESH)
    _check(mixed.strong is False, f"mixed below-tau must be weak, got {mixed!r}")
    _check(mixed.top1_cosine == 0.20, f"mixed top1 must ignore None rows, got {mixed.top1_cosine!r}")
    _check(mixed.n_cleared == 0, f"mixed n_cleared must be 0, got {mixed.n_cleared}")
    print("ok: keyword-only (cosine None) rows are weak and excluded from top1/n_cleared")


def test_n_min_boundary() -> None:
    """top1 clears tau_sim but only one row clears match_threshold -> weak, and
    the reason names the count miss (not the tau miss)."""
    d = retrieval_gate([_res(0.62), _res(0.20)], TAU, N_MIN, THRESH)
    _check(d.strong is False, f"one-cleared must be weak under n_min=2, got {d!r}")
    _check(d.n_cleared == 1, f"n_cleared must be 1, got {d.n_cleared}")
    _check("n_cleared" in d.reason, f"reason should name the count miss, got {d.reason!r}")
    print("ok: top1 clears tau but n_cleared < n_min -> weak (count-miss reason)")


def test_inclusive_boundaries() -> None:
    """Both thresholds are `>=`: a cosine exactly at tau_sim clears it, and a
    cosine exactly at match_threshold counts toward n_cleared."""
    # top1 == tau exactly, and two rows exactly at the match_threshold.
    d = retrieval_gate([_res(0.40), _res(0.30)], TAU, N_MIN, THRESH)
    _check(d.strong is True, f"tau/threshold exact-equality must clear (>=), got {d!r}")
    _check(d.n_cleared == 2, f"both boundary rows must count, got {d.n_cleared}")
    print("ok: tau_sim and match_threshold are inclusive (>=)")


def main() -> int:
    tests = [
        test_prd_case_a_strong,
        test_prd_case_b_weak_top1,
        test_prd_case_c_empty,
        test_determinism,
        test_reads_cosine_not_rrf_similarity,
        test_keyword_only_rows_are_weak,
        test_n_min_boundary,
        test_inclusive_boundaries,
    ]
    for t in tests:
        t()
    print(f"\nPASS: {len(tests)} retrieval-gate (US-047) test groups")
    return 0


if __name__ == "__main__":
    sys.exit(main())
