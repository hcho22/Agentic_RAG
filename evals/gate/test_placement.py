"""US-105 tests: the determinism → CI-placement rule.

Two layers (mirrors ``test_pinned_security.py`` / the repo's other eval tests):

1. **Offline unit checks that ALWAYS run** - no DB, no secrets, no heavy deps
   (only stdlib + PyYAML + the import-light ``evals.gate`` package). They encode
   the US-105 validation test directly:

   * **Step 1 - a deterministic breach blocks the PR.** The E4 zero-leak binary
     assert (the deterministic per-PR security gate) raises on a leaking
     ``no_access`` fixture; and a ``red`` finding on a deterministic quality gate
     the buyer opted into ``per_pr:`` blocks the merge.
   * **Step 2 - the loader rejects per-PR ``fail`` on a non-deterministic gate.**
     ``per_pr: {faithfulness: fail}`` (and every other LLM-judged target) is a
     :class:`PlacementError` at load - from a dict AND a real YAML file - while a
     deterministic target (``recall_at_5`` / ``retrieval_metrics``) loads fine.
   * **Step 3 - a judge-driven regression files an issue, never blocks a merge.**
     A ``red`` RAGAS-drop finding never blocks the merge (``blocks_merge`` is
     False regardless of ``per_pr``) yet fails the *scheduled* run under a
     ``ragas: fail`` knob (``files_issue`` is True) - the file-issue action. The
     ``false_resolve`` ceiling behaves the same: scheduled file-issue, never a
     per-PR merge block.

   Plus the structural rule itself: every ``security`` output places per-PR and
   may block; every RAGAS/escalation output places scheduled and may not; the
   three suites' determinism is computed from the registry so it can never drift.

2. **Best-effort integration guard** - when the heavy ``runner`` module is
   importable, drive a leaking ``no_access`` fixture through the REAL
   ``_aggregate_viewer_filter`` and confirm the deterministic per-PR gate trips
   on the runner's own ``security_no_access`` table (validation step 1 against the
   real exit-path input). Skips cleanly (no failure) when runner's deps
   (asyncpg / openai / httpx) are absent.

Run:
    python -m evals.gate.test_placement
"""

from __future__ import annotations

import os
import sys
import tempfile
from typing import Tuple, Type, Union

from evals.gate.classes import (
    DETERMINISTIC,
    NON_DETERMINISTIC,
    SecurityGateError,
    gate_class,
    quality_outputs,
    security_outputs,
)
from evals.gate.declaration import load_gate_declaration
from evals.gate.placement import (
    PER_PR,
    SCHEDULED,
    PlacementError,
    finding_blocks_merge,
    finding_files_issue,
    may_block_merge,
    placement_for,
    suite_is_deterministic,
    target_is_deterministic,
    validate_per_pr_target,
)
from evals.gate.security import assert_no_access_zero_leak
from evals.gate.verdict import (
    QUALITY_SUITES,
    RAGAS_METRICS,
    SuiteVerdicts,
    false_resolve_ceiling_finding,
)
from evals.retrieval.ragas_gates import GateFinding

_ExcSpec = Union[Type[BaseException], Tuple[Type[BaseException], ...]]


def _assert_raises(exc_type: _ExcSpec, fn, *, what: str) -> BaseException:
    try:
        fn()
    except exc_type as exc:
        return exc
    names = getattr(exc_type, "__name__", str(exc_type))
    raise AssertionError(f"expected {names} from {what}, none raised")


def _write_yaml(text: str) -> str:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(text)
        return fh.name


def _finding(severity: str, metric: str, *, tag: str = "regression") -> GateFinding:
    return GateFinding(
        severity=severity, tag=tag, metric=metric, cell="full_access:pre_filter",
        message=f"{metric} {severity}",
    )


# ---------------------------------------------------------------------------
# Layer 1 - the US-105 validation test (always runs)
# ---------------------------------------------------------------------------


def _placement_rule_matches_the_determinism_registry() -> None:
    """AC1/AC2/AC3: the four-workflow split reduces to ``placement_for(gate)`` -
    deterministic ⇒ per-PR (may block), non-deterministic ⇒ scheduled (may not).
    Cross-checked against the US-101 registry so the policy can never drift."""

    # Every SECURITY invariant (E4/E6/AU4/E7-P1b) is deterministic → per-PR, may
    # block a merge (they do so via their own binary asserts, US-102).
    for name in security_outputs():
        assert gate_class(name).determinism == DETERMINISTIC, name
        assert placement_for(name) == PER_PR, name
        assert may_block_merge(name) is True, name

    # The retrieval metrics are deterministic → per-PR-eligible; the RAGAS scores
    # and escalation rates are LLM-judged → scheduled, never merge-blocking.
    deterministic_quality = {"mrr", "ndcg_at_5"} | {
        n for n in quality_outputs() if n.startswith("recall_at_")
    }
    for name in quality_outputs():
        det = gate_class(name).determinism == DETERMINISTIC
        expect_per_pr = name in deterministic_quality
        assert det == expect_per_pr, (name, det)
        assert placement_for(name) == (PER_PR if expect_per_pr else SCHEDULED), name
        assert may_block_merge(name) is expect_per_pr, name

    # Every RAGAS metric + escalation rate is explicitly non-deterministic.
    for name in list(RAGAS_METRICS) + [
        "deflection_rate", "false_escalate_rate", "false_resolve",
    ]:
        assert gate_class(name).determinism == NON_DETERMINISTIC, name
        assert placement_for(name) == SCHEDULED, name

    # Suite determinism is computed from the registry: exactly retrieval_metrics
    # is deterministic; ragas / escalation are not.
    assert suite_is_deterministic("retrieval_metrics") is True
    assert suite_is_deterministic("ragas") is False
    assert suite_is_deterministic("escalation") is False
    assert set(QUALITY_SUITES) == {"retrieval_metrics", "ragas", "escalation"}

    # A suite name resolves through the same rule as an output name.
    assert target_is_deterministic("retrieval_metrics") is True
    assert target_is_deterministic("ragas") is False
    assert target_is_deterministic("recall_at_5") is True
    assert target_is_deterministic("faithfulness") is False
    # An unknown target is a hard error (no silent skip).
    _assert_raises(
        KeyError, lambda: target_is_deterministic("totally_made_up"),
        what="placement of an unknown target",
    )


def _step1_deterministic_breach_blocks_the_pr() -> None:
    """Validation step 1: a deterministic breach blocks the PR (non-zero exit)."""

    # The deterministic per-PR SECURITY gate: a leaking `no_access` table raises
    # (fails the run non-zero, blocks the merge) - pure binary assert, no judge.
    leak = {
        "pre_filter": {"vector": 1.0, "keyword": 1.0, "hybrid": 0.98},
        "post_filter": {"vector": 1.0, "keyword": 1.0, "hybrid": 1.0},
    }
    _assert_raises(
        SecurityGateError,
        lambda: assert_no_access_zero_leak(leak),
        what="the deterministic per-PR E4 zero-leak gate on a leak fixture",
    )
    # A clean table does NOT block.
    clean = {
        "pre_filter": {"vector": 1.0, "keyword": 1.0, "hybrid": 1.0},
        "post_filter": {"vector": 1.0, "keyword": 1.0, "hybrid": 1.0},
    }
    assert_no_access_zero_leak(clean)  # must not raise

    # A deterministic QUALITY gate the buyer opted into `per_pr:` blocks the merge
    # on a red finding; a yellow diagnostic never blocks.
    decl = load_gate_declaration({"per_pr": {"recall_at_5": "fail"}})
    assert decl.blocks_merge(_finding("red", "recall_at_5")) is True
    assert decl.blocks_merge(_finding("yellow", "recall_at_5")) is False
    # A whole-suite opt-in blocks any of its outputs.
    suite_decl = load_gate_declaration({"per_pr": {"retrieval_metrics": "fail"}})
    for metric in ("recall_at_1", "recall_at_10", "mrr", "ndcg_at_5"):
        assert suite_decl.blocks_merge(_finding("red", metric)) is True, metric
    # Without an opt-in, a deterministic red finding does NOT block the merge
    # (retrieval metrics stay advisory per-PR - the delta-vs-main comment, US-035).
    empty = load_gate_declaration({})
    assert empty.blocks_merge(_finding("red", "recall_at_5")) is False


def _step2_loader_rejects_per_pr_fail_on_non_deterministic() -> None:
    """Validation step 2 + AC5: configuring a non-deterministic gate as per-PR
    `fail` is a STRUCTURAL load error (PlacementError), never accepted."""

    # The PRD's exact case: RAGAS faithfulness as per-PR `fail`.
    exc = _assert_raises(
        PlacementError,
        lambda: load_gate_declaration({"per_pr": {"faithfulness": "fail"}}),
        what="per-PR fail on RAGAS faithfulness",
    )
    assert "NON-DETERMINISTIC" in str(exc), str(exc)
    assert "scheduled" in str(exc), str(exc)

    # Every non-deterministic target (LLM-judged output OR a non-deterministic
    # suite) is equally rejected - a buyer cannot opt any of them into per-PR fail.
    non_deterministic_targets = (
        list(RAGAS_METRICS)
        + ["deflection_rate", "false_escalate_rate", "false_resolve"]
        + ["ragas", "escalation"]
    )
    for target in non_deterministic_targets:
        _assert_raises(
            PlacementError,
            lambda t=target: load_gate_declaration({"per_pr": {t: "fail"}}),
            what=f"per-PR fail on non-deterministic target {target!r}",
        )
        # The lower-level structural check raises the same way.
        _assert_raises(
            PlacementError,
            lambda t=target: validate_per_pr_target(t),
            what=f"validate_per_pr_target({target!r})",
        )

    # PlacementError IS a ValueError, so the loader's generic malformed handling
    # (which catches ValueError) still fails the load closed.
    assert issubclass(PlacementError, ValueError)

    # A DETERMINISTIC target loads fine (the positive control) - from a dict and a
    # real YAML file.
    ok = load_gate_declaration({"per_pr": {"recall_at_5": "fail", "retrieval_metrics": "fail"}})
    assert ok.per_pr == frozenset({"recall_at_5", "retrieval_metrics"})

    path = _write_yaml("per_pr:\n  faithfulness: fail\n")
    try:
        _assert_raises(
            PlacementError,
            lambda: load_gate_declaration(path),
            what="a YAML file requesting per-PR fail on faithfulness",
        )
    finally:
        os.unlink(path)

    path_ok = _write_yaml("per_pr:\n  retrieval_metrics: fail\n")
    try:
        assert load_gate_declaration(path_ok).per_pr == frozenset({"retrieval_metrics"})
    finally:
        os.unlink(path_ok)


def _step3_non_deterministic_files_issue_never_blocks_merge() -> None:
    """Validation step 3: a judge-driven regression fails the SCHEDULED run and
    files an issue - it never blocks a merge, regardless of any config."""

    red_ragas = _finding("red", "faithfulness", tag="ragas-score-regression")

    # blocks_merge is False even under the shipped default AND if a buyer somehow
    # tried to route it (they can't - the loader rejects per_pr:{faithfulness}, but
    # the finding-level guard is the load-bearing structural short-circuit anyway).
    assert load_gate_declaration({}).blocks_merge(red_ragas) is False
    assert finding_blocks_merge(red_ragas, frozenset({"faithfulness"})) is False
    assert finding_blocks_merge(red_ragas, frozenset({"ragas"})) is False

    # On a scheduled workflow it files an issue when the RAGAS suite is `fail`
    # (fails the workflow + files one issue per tag), and stays silent-of-block
    # under the `comment` default.
    sched_fail = load_gate_declaration({"suites": {"ragas": "fail"}})
    assert sched_fail.files_issue(red_ragas) is True
    assert load_gate_declaration({}).files_issue(red_ragas) is False  # comment default
    # A yellow RAGAS diagnostic never files an issue (comments only under `fail`).
    assert sched_fail.files_issue(_finding("yellow", "faithfulness")) is False

    # The `false_resolve` ceiling: a pinned invariant (always the block action) but
    # a NON-deterministic metric - so it files an issue on the scheduled run and
    # NEVER blocks a merge (the accepted faithfulness-leg latency gap, US-106).
    ceiling = false_resolve_ceiling_finding(
        rate=0.1, ceiling=0.05, numerator=1, denominator=10,
    )
    assert load_gate_declaration({}).blocks_merge(ceiling) is False
    assert finding_blocks_merge(ceiling, frozenset({"escalation", "false_resolve"})) is False
    assert load_gate_declaration({}).files_issue(ceiling) is True  # pinned, files an issue
    # Direct predicate parity.
    assert finding_files_issue(red_ragas, SuiteVerdicts(knobs={"ragas": "fail"})) is True
    assert finding_files_issue(ceiling, SuiteVerdicts()) is True


def _security_output_and_unknown_target_rejected() -> None:
    """A `security` output in `per_pr:` is a build error (pinned `fail`, no knob),
    and an unknown target is a hard ValueError - no silent skip."""

    for name in security_outputs():
        _assert_raises(
            SecurityGateError,
            lambda n=name: load_gate_declaration({"per_pr": {n: "fail"}}),
            what=f"per-PR placement of a security output {name!r}",
        )

    # A non-`fail` value is a category error (that is loudness, not placement).
    for bad in ("comment", "off"):
        exc = _assert_raises(
            ValueError,
            lambda b=bad: load_gate_declaration({"per_pr": {"recall_at_5": b}}),
            what=f"per_pr value {bad!r}",
        )
        assert "suites:" in str(exc) or "verdicts:" in str(exc), str(exc)

    # An unknown target is a hard error naming the known targets.
    _assert_raises(
        ValueError,
        lambda: load_gate_declaration({"per_pr": {"made_up_metric": "fail"}}),
        what="per_pr on an unknown target",
    )
    # A non-mapping `per_pr` section is rejected.
    _assert_raises(
        ValueError,
        lambda: load_gate_declaration({"per_pr": ["recall_at_5"]}),
        what="a non-mapping per_pr section",
    )


def _default_declaration_blocks_no_merge_via_config() -> None:
    """The shipped kit default (gate.yaml, no `per_pr:` section) opts NO quality
    gate into per-PR merge-blocking - today's posture is preserved."""
    here = os.path.dirname(os.path.abspath(__file__))
    gate_yaml = os.path.join(here, "gate.yaml")
    decl = load_gate_declaration(gate_yaml)
    assert decl.per_pr == frozenset(), decl.per_pr
    # No quality finding blocks the merge under the default declaration.
    assert decl.blocks_merge(_finding("red", "recall_at_5")) is False
    assert decl.blocks_merge(_finding("red", "faithfulness")) is False


# ---------------------------------------------------------------------------
# Layer 2 - best-effort integration guard (skips without the heavy runner)
# ---------------------------------------------------------------------------


def _real_aggregation_feeds_the_per_pr_gate() -> None:
    """When runner is importable, prove the REAL `_aggregate_viewer_filter`
    produces a sub-1.0 `security_no_access` cell for a leaking `no_access` run,
    which the deterministic per-PR gate then blocks on (validation step 1 against
    the runner's own exit-path input)."""
    try:
        from evals.retrieval.runner import _aggregate_viewer_filter  # heavy deps
    except Exception as exc:  # ImportError / missing-dep chain
        print(
            f"integration guard: runner not importable ({type(exc).__name__}) "
            "- skipping real-aggregation check"
        )
        return

    def _block(v: float) -> "dict[str, float]":
        return {"recall_at_5": v, "mrr": v, "ndcg_at_5": v, "recall_at_10": v}

    def _q(v: float) -> "dict[str, object]":
        return {
            "category": "policy",
            "by_viewer": {
                "no_access": {
                    "hybrid": {"pre_filter": _block(v), "post_filter": _block(0.0)}
                }
            },
        }

    agg = _aggregate_viewer_filter([_q(0.0), _q(1.0)], ("hybrid",))
    table = agg["security_no_access"]
    assert table["pre_filter"]["hybrid"] == 0.5, table  # 1 of 2 leaked → a leak
    _assert_raises(
        SecurityGateError,
        lambda: assert_no_access_zero_leak(table),
        what="the per-PR gate on a runner-produced leaking table",
    )
    print("integration guard OK: real aggregation of a leak trips the per-PR gate")


def main() -> None:
    _placement_rule_matches_the_determinism_registry()
    _step1_deterministic_breach_blocks_the_pr()
    _step2_loader_rejects_per_pr_fail_on_non_deterministic()
    _step3_non_deterministic_files_issue_never_blocks_merge()
    _security_output_and_unknown_target_rejected()
    _default_declaration_blocks_no_merge_via_config()
    _real_aggregation_feeds_the_per_pr_gate()
    print(
        "US-105 determinism CI-placement OK: deterministic gates may block a merge "
        "per-PR; non-deterministic gates are scheduled-only (per-PR `fail` is a "
        "structural load error); judge-driven regressions file an issue, never "
        "block a merge"
    )


if __name__ == "__main__":
    # Allow `python -m evals.gate.test_placement` from the repo root.
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    main()
