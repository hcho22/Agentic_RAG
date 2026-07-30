"""Regression test: the permissions-scale harness must refuse to score a
degenerate run instead of publishing a well-formed table of 0.000.

Context. Gold in this eval is not a fixture - it is the `ef_search_for_gold`
cell of the same sweep (`runner.run_eval`). So when a viewer can see nothing,
gold comes back empty, `recall_at_k`'s old `if not gold_ids: return 0.0` scored
every cell at exactly 0.0, and `aggregate` / `render_summary` emitted a table
indistinguishable from a real measured regression. The nightly published that
table every night from 2026-06-19 onward while `db_seed/wikipedia_seed.py` was
leaving the viewers without a `workspace_membership` row.

The distinction the guard has to hold is narrow: gold coming back empty is
unscoreable and must raise, but a NON-gold cell coming back empty is a measured
HNSW recall collapse - the phenomenon this eval exists to observe - and must
score a real 0.000. Both halves are pinned below.

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
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from evals.permissions_scale import runner
from evals.permissions_scale.runner import (
    DegenerateRunError,
    _assert_cell_has_signal,
    aggregate,
    amain,
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
    # n_returned == 0 in the GOLD cell: the viewer is blind, gold comes back
    # empty, and the whole sweep is unscoreable. This is the incident case.
    _expect_raises(
        lambda: _assert_cell_has_signal(
            "viewer_1pct", "q21", 500, 0, [], is_gold_cell=True,
        ),
        must_mention=["viewer=viewer_1pct", "question=q21", "ef_search=500",
                      "workspace_membership"],
        what="_assert_cell_has_signal(gold cell, n_returned=0)",
    )
    # n_returned == 0 in a NON-gold cell is the opposite: a low-ef graph walk
    # that surfaced no visible candidate is a measured recall collapse - the
    # phenomenon this eval exists to observe - so it must pass through and be
    # scored as a real 0.000 rather than abort the sweep.
    _assert_cell_has_signal("viewer_1pct", "q21", 40, 0, [], is_gold_cell=False)
    # Rows came back in the GOLD cell but none mapped into the wikipedia corpus:
    # gold is still empty, so the sweep is still unscoreable - but the cause is a
    # corpus/map mismatch rather than a blind viewer, so it must not be reported
    # as the blind-viewer case.
    _expect_raises(
        lambda: _assert_cell_has_signal(
            "viewer_50pct", "q35", 500, 5, [], is_gold_cell=True,
        ),
        must_mention=["viewer=viewer_50pct", "question=q35", "ef_search=500",
                      "stable_id"],
        what="_assert_cell_has_signal(gold cell, rows returned, none mapped)",
    )
    # The same shape in a NON-gold cell carries real signal: the viewer retrieved
    # rows whose top-k simply contains none of gold, and recall@5 = 0.0 is the
    # arithmetically correct measured answer. Aborting here would kill the sweep
    # over strictly MORE signal than the empty non-gold cell above, which passes.
    _assert_cell_has_signal("viewer_50pct", "q35", 40, 5, [], is_gold_cell=False)
    # A healthy cell passes through silently.
    _assert_cell_has_signal(
        "viewer_1pct", "q21", 40, 5, ["wikipedia-0000:0001"], is_gold_cell=False,
    )
    print("  _assert_cell_has_signal: only the gold cell must yield a non-empty "
          "mapped set; a non-gold cell may yield anything")


def _aggregate_checks() -> None:
    # No question contributed a cell for this (viewer × ef_search) pair. The old
    # `if n > 0` skipped it, leaving a blank cell in the table; there is no
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


def _degenerate_summary_checks() -> None:
    """The published artifact must say "no numbers", not carry the last good table.

    `summary.md` is git-tracked and the nightly copies it with `if: always()`
    keyed on the file existing, so a run that aborts without rewriting it
    republishes the previous run's table as today's nightly.
    """
    exc = DegenerateRunError(
        "match_chunks returned 0 rows for the gold cell viewer=viewer_1pct "
        "question=q21 ef_search=500 - the viewer can see nothing"
    )
    out = runner.render_degenerate_summary(exc, "2026-07-30T00:00:00+00:00")
    # docs/_embed_eval_summaries.py keys off exactly this marker pair; dropping
    # them would break the embed rather than propagate the failure into it.
    assert out.startswith("<!-- BEGIN EVAL_SUMMARY -->"), out
    assert "<!-- END EVAL_SUMMARY -->" in out, out
    # The message is carried verbatim, so the published nightly is actionable.
    assert str(exc) in out, out
    # And no recall table survives into it.
    assert "ef_search=40 |" not in out and "| Viewer |" not in out, out

    # The same stale-table hazard applies to a run that died of anything else -
    # an unseeded DB, a rejected viewer JWT, an embedder outage - so the
    # catch-all notice has to satisfy the same contract, plus name the type
    # (an arbitrary exception's text need not say what kind of failure it was).
    failed = runner.render_failed_summary(
        RuntimeError("401 Client Error for match_chunks"),
        "2026-07-30T00:00:00+00:00",
    )
    assert failed.startswith("<!-- BEGIN EVAL_SUMMARY -->"), failed
    assert "<!-- END EVAL_SUMMARY -->" in failed, failed
    assert "RuntimeError: 401 Client Error for match_chunks" in failed, failed
    assert "| Viewer |" not in failed, failed

    # This notice is copied into docs/permissions-scale-nightly/<DATE>.md and
    # COMMITTED to a public repo, and Actions secret masking covers log output,
    # not files a step writes - a weaker boundary than a log line (AGENTS.md
    # invariant 3). So an arbitrary exception's text is bounded before it is
    # published: the type survives in full, the message is capped, and the
    # truncation is stated rather than silent.
    long_message = "x" * (runner.MAX_PUBLISHED_EXCEPTION_CHARS * 3)
    bounded = runner.render_failed_summary(
        RuntimeError(long_message), "2026-07-30T00:00:00+00:00",
    )
    assert "RuntimeError:" in bounded, bounded
    assert long_message not in bounded, "the full message must not be published"
    assert "truncated" in bounded, bounded
    assert "job log" in bounded, bounded
    assert "x" * runner.MAX_PUBLISHED_EXCEPTION_CHARS in bounded, bounded
    assert "x" * (runner.MAX_PUBLISHED_EXCEPTION_CHARS + 1) not in bounded, bounded
    print("  render_degenerate_summary / render_failed_summary: overwrite the "
          "table, keep the markers, bound the published exception text")


_FAKE_ENV = {
    "SUPABASE_URL": "http://127.0.0.1:54321",
    "SUPABASE_SERVICE_ROLE_KEY": "test-service-role-key",
    "OPENAI_API_KEY": "test-openai-key",
    "DATABASE_URL": "postgresql://test:test@127.0.0.1:1/test",
}

# A stand-in for the pre-regression table that `summary.md` holds in git.
_HEALTHY_TABLE = (
    "<!-- BEGIN EVAL_SUMMARY -->\n\n"
    "| Viewer | Visible chunks | Selectivity | ef_search=40 |\n"
    "|---|---|---|---|\n"
    "| viewer_1pct | 100 | 1.0% | 1.000 |\n\n"
    "<!-- END EVAL_SUMMARY -->\n"
)


async def _no_stale_summary_checks() -> None:
    """No exit path may leave behind a summary describing an earlier, healthier run.

    The nightly's publish step is `if: always()` and keyed on `summary.md`
    *existing* rather than on it being fresh, so a run that aborts without
    rewriting it republishes the last good table as today's result - healthy
    numbers for a run that measured nothing, which is strictly harder to spot
    than the 0.000 this harness was fixed to stop emitting. Both failure classes
    are exercised: the refusal (`DegenerateRunError`, exit 1) and the catch-all
    (any other exception, re-raised).

    Offline: the only thing stubbed is the chunk_id→stable_id lookup, which is
    the first thing on the eval path to touch the database, so nothing here
    opens a connection or a socket.
    """
    original_fetch = runner.fetch_wikipedia_stable_id_map
    original_argv = sys.argv
    original_env = {k: os.environ.get(k) for k in _FAKE_ENV}
    os.environ.update(_FAKE_ENV)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            summary_path = Path(tmp) / "summary.md"
            out_path = Path(tmp) / "scale.json"
            sys.argv = [
                "runner", "--summary", str(summary_path), "--out", str(out_path),
            ]

            # An unseeded corpus is the canonical degenerate run: refused, exit
            # 1, and the stale table is gone rather than merely untouched.
            summary_path.write_text(_HEALTHY_TABLE, encoding="utf-8")

            async def _empty_map(_url: str) -> dict[str, str]:
                return {}

            runner.fetch_wikipedia_stable_id_map = _empty_map
            rc = await amain()
            assert rc == 1, f"a degenerate run must exit non-zero; got {rc}"
            written = summary_path.read_text(encoding="utf-8")
            assert "DEGENERATE RUN" in written, written
            assert "wikipedia_seed" in written, written
            assert "1.000" not in written and "| Viewer |" not in written, written
            assert not out_path.exists(), "a refused run must write no results JSON"

            # Any other failure on the eval path - a rejected viewer JWT, an
            # embedder outage - leaves the same stale table behind unless the
            # catch-all arm fires. It must still re-raise so the traceback
            # reaches the run log and the job stays red.
            summary_path.write_text(_HEALTHY_TABLE, encoding="utf-8")

            async def _boom(_url: str) -> dict[str, str]:
                raise RuntimeError("connection refused")

            runner.fetch_wikipedia_stable_id_map = _boom
            try:
                await amain()
            except RuntimeError as e:
                assert "connection refused" in str(e), str(e)
            else:
                raise AssertionError("a failed run must not be swallowed")
            written = summary_path.read_text(encoding="utf-8")
            assert "RUN FAILED" in written, written
            assert "RuntimeError: connection refused" in written, written
            assert "1.000" not in written and "| Viewer |" not in written, written
            assert not out_path.exists(), "a failed run must write no results JSON"
        print("  amain: neither a refusal nor a crash leaves the previous run's "
              "table publishable")
    finally:
        runner.fetch_wikipedia_stable_id_map = original_fetch
        sys.argv = original_argv
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


async def _guard_boundary_checks() -> None:
    """The failure guard must cover the measurement phase and nothing else.

    Both edges matter, and both were real defects:

    * BEFORE it - env/config preflight. A local run with nothing exported is a
      mistake about how the run was invoked, not a measurement that failed. If
      the guard covered it, forgetting to export SUPABASE_URL would rewrite the
      git-tracked summary.md holding the last good table and a developer could
      commit the notice.
    * AFTER it - publishing. Once aggregate() has returned a table, the run
      genuinely measured something. A fault past that point (a malformed
      recall_floor block is the live source: KeyError on a missing key,
      ValueError on a non-numeric threshold) must crash with a traceback, NOT
      replace real numbers with "RUN FAILED - no numbers published".
    """
    original_argv = sys.argv
    original_fetch = runner.fetch_wikipedia_stable_id_map
    original_embed = runner.embed_texts
    original_call = runner.call_match_chunks
    original_env = {k: os.environ.get(k) for k in _FAKE_ENV}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            summary_path = Path(tmp) / "summary.md"
            out_path = Path(tmp) / "scale.json"

            # Preflight: no env exported at all.
            for key in _FAKE_ENV:
                os.environ.pop(key, None)
            summary_path.write_text(_HEALTHY_TABLE, encoding="utf-8")
            sys.argv = [
                "runner", "--summary", str(summary_path), "--out", str(out_path),
            ]
            try:
                await amain()
            except RuntimeError as e:
                assert "SUPABASE_URL is required" in str(e), str(e)
            else:
                raise AssertionError("a missing env var must still abort the run")
            assert summary_path.read_text(encoding="utf-8") == _HEALTHY_TABLE, (
                "preflight validation must not touch the git-tracked summary"
            )
            assert not out_path.exists()

            # Publishing: a fully-measured sweep, then a malformed recall_floor.
            os.environ.update(_FAKE_ENV)
            config_path = Path(tmp) / "scale_gold.yaml"
            config_path.write_text(
                "corpus:\n"
                "  total_chunks: 10000\n"
                "viewers:\n"
                "  - id: 00000000-0000-0000-0000-000000000100\n"
                "    name: viewer_1pct\n"
                "    visible_chunks: 100\n"
                "    email: scale-1pct@local.test\n"
                "question_ids: [q21]\n"
                "ef_search_values: [40, 500]\n"
                "ef_search_for_gold: 500\n"
                "top_k: 5\n"
                # min_recall_at_5 deliberately absent - a config typo, which
                # check_recall_floor turns into a KeyError.
                "recall_floor:\n"
                "  ef_search: 40\n"
                "  viewer_name: viewer_1pct\n",
                encoding="utf-8",
            )
            rows = [{"id": f"id-{i}"} for i in range(5)]

            async def _map(_url: str) -> dict[str, str]:
                return {f"id-{i}": f"wikipedia-0000:{i:04d}" for i in range(5)}

            runner.fetch_wikipedia_stable_id_map = _map
            runner.embed_texts = _stub_embed
            runner.call_match_chunks = _make_stub_match_chunks(rows)
            summary_path.write_text(_HEALTHY_TABLE, encoding="utf-8")
            sys.argv = [
                "runner", "--summary", str(summary_path), "--out", str(out_path),
                "--config", str(config_path),
            ]
            try:
                await amain()
            except KeyError as e:
                assert "min_recall_at_5" in str(e), str(e)
            else:
                raise AssertionError("a malformed recall_floor must not be swallowed")
            written = summary_path.read_text(encoding="utf-8")
            assert "RUN FAILED" not in written, (
                "a fault after aggregate() must not destroy a fully-measured "
                f"table: {written}"
            )
            assert "| Viewer |" in written and "1.000" in written, written
            assert out_path.exists(), "the measured results JSON must survive"
        print("  amain: preflight leaves the tracked summary alone; a publish-phase "
              "fault leaves the measured table intact")
    finally:
        runner.fetch_wikipedia_stable_id_map = original_fetch
        runner.embed_texts = original_embed
        runner.call_match_chunks = original_call
        sys.argv = original_argv
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


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
        # the nightly saw with the viewers missing their membership rows. The
        # gold cell going dark is what makes this unscoreable.
        runner.call_match_chunks = _make_stub_match_chunks([])
        try:
            await run_eval(
                QUESTIONS, CFG, {"viewer_1pct": {}}, {}, object(), object(), "http://x",
            )
        except DegenerateRunError as e:
            assert "viewer=viewer_1pct" in str(e) and "question=q21" in str(e), str(e)
            assert "ef_search=500" in str(e), (
                f"the gold cell is what must be named as having gone dark: {e}"
            )
        else:
            raise AssertionError(
                "run_eval scored a run in which every cell returned 0 rows"
            )

        # The narrowing that matters: gold sees the corpus but the low-ef cell
        # surfaces nothing. That is a measured HNSW recall collapse, not a
        # blackout, so the sweep must complete and score the cell a real 0.000.
        gold_only = [{"id": f"id-{i}"} for i in range(5)]
        gold_only_map = {f"id-{i}": f"wikipedia-0000:{i:04d}" for i in range(5)}

        async def _stub_gold_only(_c, _h, _u, _hd, _lit, match_count, ef_search):
            return gold_only[:match_count] if ef_search == 500 else []

        runner.call_match_chunks = _stub_gold_only
        collapsed = await run_eval(
            QUESTIONS, CFG, {"viewer_1pct": {}}, gold_only_map,
            object(), object(), "http://x",
        )
        collapsed_cells = collapsed[0]["by_viewer"]["viewer_1pct"]
        assert collapsed_cells["40"]["n_returned"] == 0, collapsed_cells["40"]
        assert collapsed_cells["40"]["recall_at_5"] == 0.0, collapsed_cells["40"]
        assert collapsed_cells["500"]["recall_at_5"] == 1.0, collapsed_cells["500"]
        collapsed_agg = aggregate(collapsed, CFG)
        assert collapsed_agg["recall_at_5_by_viewer_ef"]["viewer_1pct"] == {
            "40": 0.0, "500": 1.0,
        }, collapsed_agg

        # Same narrowing, one step further out: the low-ef cell returns rows,
        # they just all fall outside the wikipedia stable_id map. Gold is intact,
        # so this is a scoreable miss and must land as a real 0.000 rather than
        # abort a sweep that measured fine.
        async def _stub_unmapped_low_ef(_c, _h, _u, _hd, _lit, match_count, ef_search):
            if ef_search == 500:
                return gold_only[:match_count]
            return [{"id": "foreign-corpus-row"}][:match_count]

        runner.call_match_chunks = _stub_unmapped_low_ef
        unmapped = await run_eval(
            QUESTIONS, CFG, {"viewer_1pct": {}}, gold_only_map,
            object(), object(), "http://x",
        )
        unmapped_cells = unmapped[0]["by_viewer"]["viewer_1pct"]
        assert unmapped_cells["40"]["n_returned"] == 1, unmapped_cells["40"]
        assert unmapped_cells["40"]["top_stable_ids"] == [], unmapped_cells["40"]
        assert unmapped_cells["40"]["recall_at_5"] == 0.0, unmapped_cells["40"]

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
        print("  run_eval: a blind gold cell raises; an empty or unmappable "
              "low-ef cell scores 0.000; a real sweep still scores")
    finally:
        runner.embed_texts = original_embed
        runner.call_match_chunks = original_call


async def main() -> None:
    print("permissions-scale degenerate-run guard:")
    _recall_at_k_checks()
    _cell_guard_checks()
    _aggregate_checks()
    _degenerate_summary_checks()
    await _run_eval_checks()
    await _no_stale_summary_checks()
    await _guard_boundary_checks()
    print("all degenerate-run guard checks passed")


if __name__ == "__main__":
    asyncio.run(main())
