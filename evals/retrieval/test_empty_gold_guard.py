"""Regression test: the E4/E6 scorers must refuse an empty gold set instead of
reporting 0.000 for it.

Context. This is the sibling of `evals/permissions_scale/test_degenerate_guard.py`,
closing the gap CLAUDE.md invariant 12 names as still open. Gold here is not
derived from the run - it is authored as content anchors in `retrieval_gold.yaml`
and resolved at eval time - but the consequence of an empty one is identical:
`recall_at_k` / `ndcg_at_5` used to `return 0.0`, so "this question has no gold to
measure against" arrived at the gate wearing the exact shape of "retrieval missed
everything". These metrics feed the GATED E4/E6 numbers, so a golden-set authoring
error scored as a retrieval regression and dragged the gated mean down with no way
to tell it apart from the real thing.

E6's instance was the sharper one and gets its own group below: its negative
assertion IS `recall@10 == 0.0` against Workspace B's gold, so an empty B-gold set
scored a PASS on a tenant-isolation invariant the run never exercised. The
`positive_control_ok` property already caught the case where EVERY question is
blind; what it could not catch is a handful of blind questions hiding inside an
otherwise-healthy positive control.

The distinction the guards have to hold is the same narrow one as in the sibling
test: an empty GOLD set is unscoreable and must raise, but a real miss - gold
exists, the ranking failed to find it - is a genuine measurement and must still
score 0.000. Both halves are pinned below.

Everything here is **offline and always runs**: no DB, no secrets, no network.
The inputs are constructed directly. That is deliberate - the failure mode being
guarded against is a green suite over a run that measured nothing.

Run:
    python -m evals.retrieval.test_empty_gold_guard
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.retrieval.content_anchors import EmptyGoldError  # noqa: E402
from evals.retrieval.e6 import b_gold_for, recall_at_10  # noqa: E402
from evals.retrieval.runner import (  # noqa: E402
    _metrics_block,
    mrr,
    ndcg_at_5,
    recall_at_k,
    resolve_gold_anchors,
)


def _expect_raises(
    fn: Callable[[], Any], *, must_mention: list[str], what: str
) -> None:
    try:
        fn()
    except EmptyGoldError as e:
        message = str(e)
        for token in must_mention:
            assert token in message, (
                f"{what}: message must name {token!r} so the CI log is actionable "
                f"on its own; got: {message}"
            )
        return
    raise AssertionError(f"{what}: expected EmptyGoldError, none raised")


def _scorer_checks() -> None:
    """The three E4 scorers: empty gold raises; a genuine miss still scores."""
    ctx = "question=q07 mode=hybrid viewer=partial_access filter=pre_filter"

    _expect_raises(
        lambda: recall_at_k(set(), ["a", "b"], 5, context=ctx),
        must_mention=["empty gold set", "q07", "hybrid", "partial_access"],
        what="recall_at_k(empty gold)",
    )
    _expect_raises(
        lambda: ndcg_at_5(set(), ["a", "b"], context=ctx),
        must_mention=["empty gold set", "q07"],
        what="ndcg_at_5(empty gold)",
    )
    # `mrr` never had an explicit empty-gold branch - it fell through the loop to
    # the same bare 0.0, which is the identical conflation by a different route.
    _expect_raises(
        lambda: mrr(set(), ["a", "b"], context=ctx),
        must_mention=["empty gold set", "q07"],
        what="mrr(empty gold)",
    )
    # Empty gold is unscoreable even when the cell DID retrieve rows: an empty
    # denominator has no value regardless of the numerator.
    _expect_raises(
        lambda: recall_at_k(set(), [], 5),
        must_mention=["empty gold set"],
        what="recall_at_k(empty gold, empty retrieved)",
    )
    # Every refusal points at the golden set, not at a reseed - gold here is
    # authored, so that is where the fix lives.
    try:
        recall_at_k(set(), [], 5, context=ctx)
    except EmptyGoldError as e:
        assert "golden-set defect" in str(e), str(e)

    # A real miss is NOT degenerate: gold exists, the ranking simply missed it.
    assert recall_at_k({"a", "b"}, ["c", "d"], 5) == 0.0
    assert recall_at_k({"a", "b"}, ["a", "c"], 5) == 0.5
    assert recall_at_k({"a", "b"}, ["a", "b"], 5) == 1.0
    assert ndcg_at_5({"a"}, ["x", "y"]) == 0.0
    assert ndcg_at_5({"a"}, ["a", "y"]) == 1.0
    assert mrr({"a"}, ["x", "y"]) == 0.0
    assert mrr({"a"}, ["x", "a"]) == 0.5
    print("  scorers: empty gold raises on all three; a genuine 0.000 miss scores")


def _metrics_block_checks() -> None:
    """The cell-level guard: refuses before any scorer runs, naming the cell."""
    ctx = "question=q12 mode=keyword viewer=no_access filter=post_filter"
    _expect_raises(
        lambda: _metrics_block(set(), ["a"], 0, context=ctx),
        must_mention=["q12", "keyword", "no_access", "post_filter"],
        what="_metrics_block(empty gold)",
    )
    # A populated gold set produces the full canonical block, unchanged.
    block = _metrics_block({"a", "b"}, ["a", "z"], 0, context=ctx)
    assert block["recall_at_5"] == 0.5, block
    assert block["mrr"] == 1.0, block
    assert "unknown_chunks" not in block, block
    print("  _metrics_block: refuses per-cell by name; healthy cells unchanged")


def _resolution_checks() -> None:
    """The earliest-knowable guard: refuse before a single query is issued."""
    contents = {"refund-policy:0": "Refunds are issued within 30 days of purchase."}

    # Healthy: the anchor resolves, so resolution injects a non-empty gold set.
    questions: list[dict[str, Any]] = [
        {"id": "q01", "gold_anchors": ["Refunds are issued within 30 days"]}
    ]
    resolve_gold_anchors(questions, contents)
    assert questions[0]["gold_stable_ids"] == ["refund-policy:0"], questions

    # A question that arrives at scoring with empty gold must be refused here,
    # before any query runs - not 300 retrieval calls later inside a scorer.
    # `resolve_question` guarantees non-empty today, so this pins the contract
    # against a future resolver that filters or soft-fails instead of raising.
    class _BlindResolver:
        def resolve_all(self, qs: list[dict[str, Any]]) -> None:
            for q in qs:
                q["gold_stable_ids"] = []

    import evals.retrieval.runner as runner_mod

    original = runner_mod.ContentAnchorResolver
    runner_mod.ContentAnchorResolver = lambda _contents: _BlindResolver()  # type: ignore[assignment]
    try:
        _expect_raises(
            lambda: resolve_gold_anchors(
                [{"id": "q01", "gold_anchors": ["x"]},
                 {"id": "q02", "gold_anchors": ["y"]}],
                contents,
            ),
            must_mention=["q01", "q02", "after anchor resolution"],
            what="resolve_gold_anchors(all questions blind)",
        )
    finally:
        runner_mod.ContentAnchorResolver = original  # type: ignore[assignment]
    print("  resolve_gold_anchors: refuses unscoreable questions pre-query")


def _e6_checks() -> None:
    """E6's fail-OPEN instance: an empty B-gold set used to score a PASS."""
    _expect_raises(
        lambda: recall_at_10(set(), ["a", "b"], context="question=q03 mode=vector"),
        must_mention=["empty gold set", "q03", "vector"],
        what="recall_at_10(empty B-gold)",
    )
    # E6's gold is derived from the run (the Workspace-B copy), so unlike E4 the
    # remedy it names is a reseed.
    try:
        recall_at_10(set(), [])
    except EmptyGoldError as e:
        assert "seed_workspace_b" in str(e), str(e)

    # b_gold_for: the projection every E6 loop shares. Refuses by question id.
    import uuid

    b_map = {"refund-policy:0": uuid.uuid4()}
    ok = b_gold_for({"id": "q01", "gold_stable_ids": ["refund-policy:0"]}, b_map)
    assert ok == {str(b_map["refund-policy:0"])}, ok
    _expect_raises(
        lambda: b_gold_for(
            {"id": "q99", "gold_stable_ids": ["loyalty-tiers:2"]}, b_map
        ),
        must_mention=["q99", "Workspace B"],
        what="b_gold_for(question absent from the B copy)",
    )

    # The load-bearing half: a REAL zero - B-gold exists and none of it was
    # retrieved - is E6's PASS condition and must still score 0.0, not raise.
    # If this ever raises, the isolation eval can no longer report a clean run.
    assert recall_at_10({"b-chunk"}, ["a-chunk", "other"]) == 0.0
    assert recall_at_10({"b-chunk"}, ["b-chunk"]) == 1.0
    print("  e6: empty B-gold raises; a real zero-leak still scores 0.0")


def main() -> int:
    print("empty-gold guard (E4 + E6 scorers):")
    _scorer_checks()
    _metrics_block_checks()
    _resolution_checks()
    _e6_checks()
    print("\nPASS: 4 empty-gold guard test groups")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
