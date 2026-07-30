"""Regression test: the permissions-scale harness must refuse to score a
degenerate run instead of publishing a well-formed table of 0.000.

Context. Gold in this eval is not a fixture - it is the `ef_search_for_gold`
cell of the same sweep (`runner.run_eval`). So when a viewer can see nothing,
gold comes back empty, `recall_at_k`'s old `if not gold_ids: return 0.0` scored
every cell at exactly 0.0, and `aggregate` / `render_summary` emitted a table
indistinguishable from a real measured regression. The nightly published that
table every night from 2026-06-19 onward while `db_seed/wikipedia_seed.py` was
leaving the viewers without a `workspace_membership` row.

Everything here is **offline and always runs**: the degenerate inputs are
constructed directly (stubbed `embed_texts` / `call_match_chunks`), so the test
never touches a database and cannot pass merely because the DB happens to be
unseeded. That is deliberate - the whole failure mode this guards against is a
green suite over a nightly that measured nothing.

Run:
    python -m evals.permissions_scale.test_degenerate_guard
"""

from __future__ import annotations

import asyncio
from typing import Any

from evals.permissions_scale import runner
from evals.permissions_scale.runner import (
    DegenerateRunError,
    _assert_cell_has_signal,
    aggregate,
    recall_at_k,
    run_eval,
)

# A minimal sweep config with the same shape as scale_gold.yaml.
CFG: dict[str, Any] = {
    "viewers": [{"name": "viewer_1pct", "id": "00000000-0000-0000-0000-000000000100"}],
    "ef_search_values": [40, 500],
    "ef_search_for_gold": 500,
    "top_k": 5,
}
QUESTIONS = [{"id": "q21", "question": "how does the refund window work"}]


def _expect_raises(fn, *, must_mention: list[str], what: str) -> None:
    try:
        fn()
    except DegenerateRunError as e:
        message = str(e)
        for token in must_mention:
            assert token in message, (
                f"{what}: message must name {token!r} so the nightly log is "
                f"actionable on its own; got: {message}"
            )
        return
    raise AssertionError(f"{what}: expected DegenerateRunError, none raised")


def _recall_at_k_checks() -> None:
    # The defect itself: empty gold must raise, not score 0.0.
    _expect_raises(
        lambda: recall_at_k(set(), [], 5, context="viewer=viewer_1pct question=q21"),
        must_mention=["empty gold set", "viewer_1pct", "q21"],
        what="recall_at_k(empty gold)",
    )
    # Empty gold is degenerate even when the cell being scored DID return rows -
    # an empty denominator is unscoreable regardless of the numerator.
    _expect_raises(
        lambda: recall_at_k(set(), ["wikipedia-0000:0001"], 5),
        must_mention=["empty gold set"],
        what="recall_at_k(empty gold, non-empty retrieved)",
    )

    # A real miss is NOT degenerate: gold exists, the cell simply missed it.
    # This is the "bad signal" the guard must still let through and report.
    assert recall_at_k({"a", "b"}, ["c", "d"], 5) == 0.0
    assert recall_at_k({"a", "b"}, ["a", "c"], 5) == 0.5
    assert recall_at_k({"a", "b"}, ["a", "b"], 5) == 1.0
    print("  recall_at_k: empty gold raises; a genuine 0.0 miss still scores")


def _cell_guard_checks() -> None:
    # n_returned == 0: the viewer is blind.
    _expect_raises(
        lambda: _assert_cell_has_signal("viewer_1pct", "q21", 40, 0, []),
        must_mention=["viewer=viewer_1pct", "question=q21", "ef_search=40",
                      "workspace_membership"],
        what="_assert_cell_has_signal(n_returned=0)",
    )
    # Rows came back but none mapped into the wikipedia corpus - also no signal,
    # and a distinct cause, so it must not be reported as the blind-viewer case.
    _expect_raises(
        lambda: _assert_cell_has_signal("viewer_50pct", "q35", 500, 5, []),
        must_mention=["viewer=viewer_50pct", "question=q35", "ef_search=500",
                      "stable_id"],
        what="_assert_cell_has_signal(rows returned, none mapped)",
    )
    # A healthy cell passes through silently.
    _assert_cell_has_signal("viewer_1pct", "q21", 40, 5, ["wikipedia-0000:0001"])
    print("  _assert_cell_has_signal: blind viewer and unmappable rows both raise")


def _aggregate_checks() -> None:
    # No question contributed a cell for this (viewer × ef_search) pair. The old
    # `if n > 0` skipped it, leaving render_summary to print its dash
    # placeholder for the cell; there is no
    # legitimate reason for a configured viewer to have zero measured questions.
    _expect_raises(
        lambda: aggregate([{"id": "q21", "by_viewer": {}}], CFG),
        must_mention=["viewer_1pct", "ef_search="],
        what="aggregate(no measured questions)",
    )
    _expect_raises(
        lambda: aggregate([], CFG),
        must_mention=["viewer_1pct"],
        what="aggregate(no questions at all)",
    )
    # A populated run still aggregates.
    populated = [{
        "id": "q21",
        "by_viewer": {"viewer_1pct": {
            "40": {"recall_at_5": 0.6, "n_returned": 5, "top_stable_ids": ["a"]},
            "500": {"recall_at_5": 1.0, "n_returned": 5, "top_stable_ids": ["a"]},
        }},
    }]
    agg = aggregate(populated, CFG)
    assert agg["recall_at_5_by_viewer_ef"]["viewer_1pct"] == {"40": 0.6, "500": 1.0}
    print("  aggregate: an unmeasured cell raises instead of rendering a blank")


def _stub_embed(_client, texts):
    async def _run():
        return [[0.1] * 4 for _ in texts]
    return _run()


def _make_stub_match_chunks(rows_per_call: list[dict[str, Any]]):
    async def _stub(_client, _http, _url, _headers, _literal, match_count, ef_search):
        return rows_per_call[:match_count]
    return _stub


async def _run_eval_checks() -> None:
    """`run_eval` must refuse the degenerate run before scoring ever happens.

    This is the "caller cannot route around the guard" case: the guard fires on
    the raw match_chunks response, so a degenerate run dies here rather than
    reaching recall_at_k, aggregate, or render_summary at all.
    """
    original_embed = runner.embed_texts
    original_call = runner.call_match_chunks
    runner.embed_texts = _stub_embed
    try:
        # Degenerate: every match_chunks call returns zero rows - exactly what
        # the nightly saw with the viewers missing their membership rows.
        runner.call_match_chunks = _make_stub_match_chunks([])
        try:
            await run_eval(
                QUESTIONS, CFG, {"viewer_1pct": {}}, {}, object(), object(), "http://x",
            )
        except DegenerateRunError as e:
            assert "viewer=viewer_1pct" in str(e) and "question=q21" in str(e), str(e)
        else:
            raise AssertionError(
                "run_eval scored a run in which every cell returned 0 rows"
            )

        # Positive control: with real rows, run_eval scores normally and the
        # guard stays out of the way. ef=40 returns a partly different top-5
        # than the ef=500 gold cell, so recall lands strictly between 0 and 1.
        gold_rows = [{"id": f"id-{i}"} for i in range(5)]
        stable_map = {f"id-{i}": f"wikipedia-0000:{i:04d}" for i in range(8)}

        async def _stub_by_ef(_c, _h, _u, _hd, _lit, match_count, ef_search):
            if ef_search == 500:
                return gold_rows[:match_count]
            return [{"id": "id-0"}, {"id": "id-1"}, {"id": "id-5"},
                    {"id": "id-6"}, {"id": "id-7"}][:match_count]

        runner.call_match_chunks = _stub_by_ef
        per_question = await run_eval(
            QUESTIONS, CFG, {"viewer_1pct": {}}, stable_map,
            object(), object(), "http://x",
        )
        cells = per_question[0]["by_viewer"]["viewer_1pct"]
        assert cells["500"]["recall_at_5"] == 1.0, cells["500"]
        assert cells["40"]["recall_at_5"] == 0.4, cells["40"]
        agg = aggregate(per_question, CFG)
        assert agg["recall_at_5_by_viewer_ef"]["viewer_1pct"]["40"] == 0.4
        print("  run_eval: degenerate run raises; a real sweep still scores")
    finally:
        runner.embed_texts = original_embed
        runner.call_match_chunks = original_call


async def main() -> None:
    print("permissions-scale degenerate-run guard:")
    _recall_at_k_checks()
    _cell_guard_checks()
    _aggregate_checks()
    await _run_eval_checks()
    print("all degenerate-run guard checks passed")


if __name__ == "__main__":
    asyncio.run(main())
