"""US-118 validation test: the non-regression check is one-sided.

Pins the `within_tolerance` semantics of the nightly non-regression check,
entirely offline (no DB, no network). The check is a regression tripwire,
not a two-sided drift alarm: an improvement past the old two-sided tolerance
must render ✓ (the pre-US-118 `abs(delta) <= tol` rendered a permanently-red
✗ on *improved* metrics, training readers to ignore the cell), while a real
drop past NON_REGRESSION_TOLERANCE must still render ✗.

Cases pinned against `_aggregate_viewer_filter` + `render_summary`:
  1. improvement  (delta = +0.190)  -> within_tolerance True,  ✓
  2. noise-floor  (delta = -0.005)  -> within_tolerance True,  ✓  (boundary)
  3. regression   (delta = -0.033)  -> within_tolerance False, ✗

Run:
    python -m evals.retrieval.test_us118_one_sided_nonregression
"""

from __future__ import annotations

import os
import sys

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from evals.retrieval.runner import (  # noqa: E402
    NON_REGRESSION_BASELINE_RECALL_AT_5,
    NON_REGRESSION_TOLERANCE,
    _aggregate_viewer_filter,
    render_summary,
)

MODES = ("vector", "keyword", "hybrid")


def _per_question_with_full_access_recall(recall_by_mode: dict[str, float]) -> list:
    """One synthetic question whose full_access × pre_filter recall@5 per mode
    is exactly `recall_by_mode` (mean over n=1 is the value itself)."""
    metrics = lambda r: {"recall_at_5": r, "mrr": r, "ndcg_at_5": r}  # noqa: E731
    return [
        {
            "category": "single_chunk",
            "by_viewer": {
                "full_access": {
                    mode: {"pre_filter": metrics(r), "post_filter": metrics(r)}
                    for mode, r in recall_by_mode.items()
                }
            },
        }
    ]


def _non_regression_for(recall_by_mode: dict[str, float]) -> dict:
    per_question = _per_question_with_full_access_recall(recall_by_mode)
    return _aggregate_viewer_filter(per_question, MODES)["non_regression"]


def test_improvement_passes_one_sided() -> None:
    """delta > 0 (the exact pre-US-118 permanently-red case) must pass."""
    recall = {
        mode: round(base + 0.190, 4)
        for mode, base in NON_REGRESSION_BASELINE_RECALL_AT_5.items()
    }
    non_reg = _non_regression_for(recall)
    for mode in MODES:
        cell = non_reg[mode]
        assert cell["delta"] > NON_REGRESSION_TOLERANCE, cell
        assert cell["within_tolerance"] is True, (
            f"{mode}: improvement (delta {cell['delta']:+.3f}) must render ✓ "
            f"under the one-sided check, got within_tolerance=False"
        )


def test_noise_floor_dip_passes() -> None:
    """delta == -tolerance exactly (the noise floor boundary) must pass."""
    recall = {
        mode: round(base - NON_REGRESSION_TOLERANCE, 4)
        for mode, base in NON_REGRESSION_BASELINE_RECALL_AT_5.items()
    }
    non_reg = _non_regression_for(recall)
    for mode in MODES:
        assert non_reg[mode]["within_tolerance"] is True, non_reg[mode]


def test_regression_past_tolerance_flags() -> None:
    """delta < -tolerance (a real drop) must still trip the tripwire."""
    recall = {
        mode: round(base - 0.033, 4)
        for mode, base in NON_REGRESSION_BASELINE_RECALL_AT_5.items()
    }
    non_reg = _non_regression_for(recall)
    for mode in MODES:
        assert non_reg[mode]["within_tolerance"] is False, (
            f"{mode}: a real regression (delta {non_reg[mode]['delta']:+.3f}) "
            f"must render ✗, got within_tolerance=True"
        )


def test_render_summary_one_sided_marks() -> None:
    """The rendered table carries the one-sided header and per-row ✓/✗."""
    base = NON_REGRESSION_BASELINE_RECALL_AT_5
    recall = {
        "vector": round(base["vector"] + 0.190, 4),  # improvement -> ✓
        "keyword": base["keyword"],  # at baseline -> ✓
        "hybrid": round(base["hybrid"] - 0.033, 4),  # regression -> ✗
    }
    per_question = _per_question_with_full_access_recall(recall)
    aggregates = {
        "by_mode": {m: {"recall_at_5": r, "mrr": r, "ndcg_at_5": r} for m, r in recall.items()},
        "by_mode_category": {},
        "n_questions": 1,
    }
    aggregates.update(_aggregate_viewer_filter(per_question, MODES))
    summary = render_summary(aggregates, MODES)

    assert "full_access recall@5 vs pinned baseline" in summary
    assert "Δ ≥ −0.005?" in summary
    assert "Within ±0.005?" not in summary
    non_reg_lines = [
        line
        for line in summary.splitlines()
        if ("✓" in line or "✗" in line) and line.startswith("|")
    ]
    by_mode = {line.split("|")[1].strip(): line for line in non_reg_lines}
    assert "✓" in by_mode["vector"] and "✗" not in by_mode["vector"], by_mode["vector"]
    assert "✓" in by_mode["keyword"], by_mode["keyword"]
    assert "✗" in by_mode["hybrid"], by_mode["hybrid"]


def main() -> None:
    tests = [
        test_improvement_passes_one_sided,
        test_noise_floor_dip_passes,
        test_regression_past_tolerance_flags,
        test_render_summary_one_sided_marks,
    ]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"OK — {len(tests)} tests")


if __name__ == "__main__":
    main()
