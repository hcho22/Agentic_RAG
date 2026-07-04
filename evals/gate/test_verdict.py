"""US-104 tests: the per-suite `off|comment|fail` verdict layer.

One always-runs layer (no DB, no secrets, no heavy deps — only stdlib + PyYAML +
the import-light `evals.gate` package). It encodes the US-104 validation test
directly and covers the surrounding invariants:

  * the `(severity, knob) -> action` table (AC1) for every combination + bad input;
  * the validation test proper: a red + a yellow `GateFinding` under `fail`,
    `comment`, `off`, and a `false_resolve` ceiling-breach finding under `comment`;
  * detection output is IDENTICAL across knob values (loudness changes only the
    action surface) — the findings themselves are never mutated;
  * the knob is PER-SUITE, not global (AC2): a red RAGAS finding blocks at
    `ragas: fail` while the retrieval-metrics suite stays `comment`, and vice versa;
  * the `false_resolve` ceiling ignores the knob at EVERY value incl. `off` (AC3),
    even when the buyer sets `false_resolve`'s ordinary loudness knob to `comment`;
  * security outputs are in NO suite and carry no knob (US-102 intact);
  * the two shipped postures reproduce (AC4): weekly RAGAS `fail` blocks a red,
    PR all-`comment` never blocks;
  * a real `check_operational_gates` finding routes through the layer (not just a
    hand-built one);
  * the declaration `suites:` section + per-output-over-per-suite layering.

Run:
    python -m evals.gate.test_verdict
"""

from __future__ import annotations

import os
import sys
import tempfile
from typing import Tuple, Type, Union

from evals.gate.classes import quality_outputs, security_outputs
from evals.gate.declaration import GateDeclaration, load_gate_declaration
from evals.gate.verdict import (
    BLOCK,
    COMMENT,
    ESCALATION,
    NONE,
    QUALITY_SUITES,
    RAGAS,
    RETRIEVAL_METRICS,
    TAG_FALSE_RESOLVE_CEILING,
    SuiteVerdicts,
    action_for_finding,
    blocks,
    false_resolve_ceiling_finding,
    posts,
    run_should_block,
    suite_for_finding,
    suite_for_output,
    verdict_action,
)
from evals.gate.classes import SecurityGateError
from evals.retrieval.ragas import RAGAS_METRICS
from evals.retrieval.ragas_gates import GateFinding, check_operational_gates

_ExcSpec = Union[Type[BaseException], Tuple[Type[BaseException], ...]]


def _assert_raises(exc_type: _ExcSpec, fn, *, what: str) -> BaseException:
    try:
        fn()
    except exc_type as exc:
        return exc
    names = getattr(exc_type, "__name__", str(exc_type))
    raise AssertionError(f"expected {names} from {what}, none raised")


# Fixtures — a red and a yellow finding in the RAGAS suite (metric = a RAGAS
# metric), matching the validation test's "A red GateFinding and a yellow
# GateFinding fixture".
def _red_finding(metric: str = "faithfulness") -> GateFinding:
    return GateFinding(
        severity="red",
        tag="score-regression",
        metric=metric,
        cell="full_access:pre_filter",
        message=f"{metric} dropped below its rolling median (red fixture)",
    )


def _yellow_finding(metric: str = "faithfulness") -> GateFinding:
    return GateFinding(
        severity="yellow",
        tag="coverage-drift",
        metric=metric,
        cell="full_access:pre_filter",
        message=f"{metric} coverage drifted (yellow fixture)",
    )


# ---------------------------------------------------------------------------
# AC1 — the (severity, knob) -> action table.
# ---------------------------------------------------------------------------


def _verdict_action_table() -> None:
    # fail: red blocks, yellow comments.
    assert verdict_action("red", "fail") == BLOCK
    assert verdict_action("yellow", "fail") == COMMENT
    # comment: both comment, nothing blocks.
    assert verdict_action("red", "comment") == COMMENT
    assert verdict_action("yellow", "comment") == COMMENT
    # off: nothing posts.
    assert verdict_action("red", "off") == NONE
    assert verdict_action("yellow", "off") == NONE

    # Bad input is a loud error, never a silent no-op.
    _assert_raises(ValueError, lambda: verdict_action("orange", "fail"),
                   what="an unknown severity")
    _assert_raises(ValueError, lambda: verdict_action("red", "loud"),
                   what="an unknown knob")

    # blocks()/posts() helpers.
    assert blocks(BLOCK) and not blocks(COMMENT) and not blocks(NONE)
    assert posts(BLOCK) and posts(COMMENT) and not posts(NONE)
    _assert_raises(ValueError, lambda: blocks("nope"), what="blocks() of a bad action")
    print("AC1 OK: (severity, knob) -> action table + helpers")


# ---------------------------------------------------------------------------
# The US-104 validation test proper (Steps 1-4).
# ---------------------------------------------------------------------------


def _validation_test() -> None:
    red = _red_finding()
    yellow = _yellow_finding()

    # Snapshot the findings so we can prove they are NEVER mutated by the layer
    # (detection output identical across knob values).
    def _snap(f: GateFinding) -> tuple:
        return (f.severity, f.tag, f.metric, f.cell, f.message)

    red_before, yellow_before = _snap(red), _snap(yellow)

    # Step 1 — knob `fail` (RAGAS suite): red -> blocking, yellow -> comment.
    fail = SuiteVerdicts({RAGAS: "fail"})
    assert action_for_finding(red, fail) == BLOCK, "red @ fail must block"
    assert blocks(action_for_finding(red, fail))
    assert action_for_finding(yellow, fail) == COMMENT, "yellow @ fail must comment"
    assert not blocks(action_for_finding(yellow, fail))

    # Step 2 — knob `comment`: both comment, none blocking.
    comment = SuiteVerdicts({RAGAS: "comment"})
    assert action_for_finding(red, comment) == COMMENT
    assert action_for_finding(yellow, comment) == COMMENT
    assert not blocks(action_for_finding(red, comment))
    assert not blocks(action_for_finding(yellow, comment))

    # Step 3 — knob `off`: no action posts.
    off = SuiteVerdicts({RAGAS: "off"})
    assert action_for_finding(red, off) == NONE
    assert action_for_finding(yellow, off) == NONE
    assert not posts(action_for_finding(red, off))

    # Step 4 — a false_resolve ceiling-breach finding under knob `comment` STILL
    # blocks (AC3: the pinned ceiling ignores the escalation suite's knob).
    ceiling = false_resolve_ceiling_finding(
        rate=0.12, ceiling=0.05, numerator=6, denominator=50
    )
    assert ceiling.tag == TAG_FALSE_RESOLVE_CEILING
    esc_comment = SuiteVerdicts({ESCALATION: "comment"})
    assert action_for_finding(ceiling, esc_comment) == BLOCK, (
        "false_resolve ceiling breach must block regardless of the escalation knob"
    )

    # Detection output is identical across knob values — the findings are unchanged.
    assert _snap(red) == red_before, "the layer must not mutate a finding"
    assert _snap(yellow) == yellow_before
    print("validation test OK: fail/comment/off + false_resolve ceiling (Steps 1-4)")


# ---------------------------------------------------------------------------
# AC2 — the knob is PER-SUITE, not a single global flag.
# ---------------------------------------------------------------------------


def _per_suite_independence() -> None:
    ragas_red = _red_finding("faithfulness")            # RAGAS suite
    retrieval_red = _red_finding("recall_at_5")         # retrieval-metrics suite

    assert suite_for_finding(ragas_red) == RAGAS
    assert suite_for_finding(retrieval_red) == RETRIEVAL_METRICS

    # RAGAS at `fail`, retrieval-metrics at its default `comment`: only the RAGAS
    # red blocks; the retrieval red comments. The knobs do NOT flatten together.
    suites = SuiteVerdicts({RAGAS: "fail"})
    assert action_for_finding(ragas_red, suites) == BLOCK
    assert action_for_finding(retrieval_red, suites) == COMMENT

    # Flip it: retrieval-metrics at `fail`, RAGAS at `comment`.
    suites2 = SuiteVerdicts({RETRIEVAL_METRICS: "fail", RAGAS: "comment"})
    assert action_for_finding(retrieval_red, suites2) == BLOCK
    assert action_for_finding(ragas_red, suites2) == COMMENT

    # A cell-level finding (metric == "") defaults to the RAGAS suite.
    cell_level = GateFinding(
        severity="red", tag="coverage-operational-failure", metric="", cell="c",
        message="api errors",
    )
    assert suite_for_finding(cell_level) == RAGAS
    print("AC2 OK: per-suite knobs are independent, not a global flag")


# ---------------------------------------------------------------------------
# AC3 — the false_resolve ceiling ignores the knob at EVERY value.
# ---------------------------------------------------------------------------


def _false_resolve_ceiling_pinned() -> None:
    ceiling = false_resolve_ceiling_finding(
        rate=0.9, ceiling=0.05, numerator=9, denominator=10
    )
    for knob in ("off", "comment", "fail"):
        suites = SuiteVerdicts({ESCALATION: knob})
        assert action_for_finding(ceiling, suites) == BLOCK, (
            f"ceiling breach must block even under escalation={knob}"
        )
    # And with NO suites config at all (all defaults) it still blocks.
    assert action_for_finding(ceiling) == BLOCK
    print("AC3 OK: false_resolve ceiling breach blocks under off/comment/fail")


# ---------------------------------------------------------------------------
# Security outputs are in NO suite (US-102 intact).
# ---------------------------------------------------------------------------


def _security_outputs_have_no_suite() -> None:
    for name in security_outputs():
        _assert_raises(
            SecurityGateError,
            lambda n=name: suite_for_output(n),
            what=f"suite_for_output({name!r})",
        )
    # An unknown output is a KeyError, not a silent default.
    _assert_raises(KeyError, lambda: suite_for_output("not_a_metric"),
                   what="suite_for_output of an unknown name")
    print(f"security OK: {len(security_outputs())} security outputs carry no suite")


# ---------------------------------------------------------------------------
# The quality suites PARTITION every quality output (drift guard).
# ---------------------------------------------------------------------------


def _suites_partition_quality_outputs() -> None:
    by_suite: "dict[str, list[str]]" = {s: [] for s in QUALITY_SUITES}
    for name in quality_outputs():
        by_suite[suite_for_output(name)].append(name)
    # Every quality output resolved to exactly one suite (suite_for_output would
    # have raised otherwise), and each suite is non-empty.
    covered = {n for names in by_suite.values() for n in names}
    assert covered == set(quality_outputs()), "every quality output has a suite"
    for suite, names in by_suite.items():
        assert names, f"suite {suite!r} has no members"
    # Spot-check the partition shape.
    assert set(by_suite[RAGAS]) == set(RAGAS_METRICS)
    assert "false_resolve" in by_suite[ESCALATION]
    assert "recall_at_5" in by_suite[RETRIEVAL_METRICS]
    print("partition OK: the 3 suites cover every quality output exactly once")


# ---------------------------------------------------------------------------
# AC4 — the two shipped postures reproduce.
# ---------------------------------------------------------------------------


def _reproduces_shipped_postures() -> None:
    red, yellow = _red_finding(), _yellow_finding()

    # Weekly posture: the RAGAS suite at `fail` reproduces runner.py::amain's
    # red -> exit-non-zero (a red blocks) while yellow never fails.
    weekly = SuiteVerdicts({RAGAS: "fail"})
    assert run_should_block([red, yellow], weekly) is True
    assert action_for_finding(yellow, weekly) == COMMENT  # yellow never blocks
    assert run_should_block([yellow], weekly) is False    # yellow-only never blocks

    # PR posture: every suite at `comment` (the default) reproduces
    # ci/diff_results.py's comment-only — nothing blocks.
    pr = SuiteVerdicts()  # all comment
    assert run_should_block([red, yellow], pr) is False
    assert action_for_finding(red, pr) == COMMENT

    # A false_resolve ceiling breach blocks even under the PR comment-only posture.
    ceiling = false_resolve_ceiling_finding(
        rate=0.2, ceiling=0.05, numerator=2, denominator=10
    )
    assert run_should_block([ceiling], pr) is True
    print("AC4 OK: weekly `fail` blocks a red; PR `comment` blocks nothing")


# ---------------------------------------------------------------------------
# A REAL check_operational_gates finding routes through the layer.
# ---------------------------------------------------------------------------


def _real_detector_finding_routes() -> None:
    # A coverage below the fixed floor produces a real red coverage-pipeline
    # finding on the `full_access:pre_filter` cell for a RAGAS metric.
    aggregates = {
        "by_cell": {
            "full_access:pre_filter": {
                "faithfulness": {"coverage": 0.10, "api_errors": 0},
            }
        }
    }
    findings = check_operational_gates(aggregates)
    reds = [f for f in findings if f.severity == "red"]
    assert reds, "expected a real red coverage finding"
    for f in reds:
        assert suite_for_finding(f) == RAGAS
        assert action_for_finding(f, SuiteVerdicts({RAGAS: "fail"})) == BLOCK
        assert action_for_finding(f, SuiteVerdicts({RAGAS: "comment"})) == COMMENT
        assert action_for_finding(f, SuiteVerdicts({RAGAS: "off"})) == NONE
    print("integration OK: a real check_operational_gates red routes through the layer")


# ---------------------------------------------------------------------------
# Declaration `suites:` section + per-output-over-per-suite layering.
# ---------------------------------------------------------------------------


def _declaration_suites_and_layering() -> None:
    decl = load_gate_declaration(
        {
            "suites": {"ragas": "fail", "escalation": "off"},
            "verdicts": {"faithfulness": "comment"},  # per-output override
        }
    )
    assert isinstance(decl, GateDeclaration)
    # Suite defaults: retrieval-metrics unset -> comment.
    assert decl.suites.knob_for(RETRIEVAL_METRICS) == "comment"
    assert decl.suites.knob_for(RAGAS) == "fail"
    assert decl.suites.knob_for(ESCALATION) == "off"

    # resolve_knob layering: the per-output `faithfulness: comment` OVERRIDES the
    # RAGAS suite's `fail`; an un-overridden RAGAS metric takes the suite knob.
    assert decl.resolve_knob("faithfulness") == "comment"  # per-output wins
    assert decl.resolve_knob("answer_relevancy") == "fail"  # suite knob
    assert decl.resolve_knob("recall_at_5") == "comment"    # default

    # action_for_finding respects the override: a red `faithfulness` finding
    # COMMENTS (per-output override) while a red `answer_relevancy` finding BLOCKS.
    assert decl.action_for_finding(_red_finding("faithfulness")) == COMMENT
    assert decl.action_for_finding(_red_finding("answer_relevancy")) == BLOCK

    # A false_resolve ceiling breach blocks via the declaration too, regardless of
    # escalation=off AND regardless of a `false_resolve: comment` per-output knob
    # (US-102 still allows that knob; the ceiling breach is a distinct tagged
    # finding the layer short-circuits to BLOCK).
    decl2 = load_gate_declaration(
        {"suites": {"escalation": "off"}, "verdicts": {"false_resolve": "comment"}}
    )
    ceiling = false_resolve_ceiling_finding(
        rate=0.3, ceiling=0.05, numerator=3, denominator=10
    )
    assert decl2.action_for_finding(ceiling) == BLOCK
    print("declaration OK: `suites:` + per-output override + ceiling short-circuit")


def _declaration_rejects_bad_suites() -> None:
    # An unknown suite key is a hard error (a typo would silently leave the suite
    # at its default).
    _assert_raises(
        ValueError,
        lambda: load_gate_declaration({"suites": {"ragass": "fail"}}),
        what="an unknown suite key",
    )
    # A security output name is not a suite -> unknown-suite rejection (US-102).
    _assert_raises(
        ValueError,
        lambda: load_gate_declaration({"suites": {"E4_zero_leak": "off"}}),
        what="a security output as a suite key",
    )
    # An invalid knob value is a hard error.
    _assert_raises(
        ValueError,
        lambda: load_gate_declaration({"suites": {"ragas": "loud"}}),
        what="an invalid suite knob",
    )
    # A non-mapping `suites` section is a hard error.
    _assert_raises(
        ValueError,
        lambda: load_gate_declaration({"suites": ["ragas"]}),
        what="a non-mapping suites section",
    )

    # The YAML-1.1 `off` gotcha: an UNQUOTED `off` parses to False and must
    # normalize back to "off" (a buyer's `ragas: off` works unquoted).
    with tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as fh:
        fh.write("suites:\n  ragas: off\n")
        path = fh.name
    try:
        decl = load_gate_declaration(path)
        assert decl.suites.knob_for(RAGAS) == "off", "unquoted `off` -> 'off'"
    finally:
        os.unlink(path)
    print("declaration OK: bad suites rejected; YAML `off` normalized")


def _default_gate_yaml_is_all_comment() -> None:
    # The shipped default gate.yaml omits `suites:`, so every suite is `comment`
    # (the PR posture) — the byte-identical-to-today guard (US-104 default).
    here = os.path.dirname(os.path.abspath(__file__))
    gate_yaml = os.path.join(here, "gate.yaml")
    if not os.path.exists(gate_yaml):  # pragma: no cover - defensive
        print("skip: gate.yaml not found")
        return
    decl = load_gate_declaration(gate_yaml)
    for suite in QUALITY_SUITES:
        assert decl.suites.knob_for(suite) == "comment", (
            f"default gate.yaml must leave {suite} at comment"
        )
    print("default OK: shipped gate.yaml leaves every suite at `comment`")


def main() -> None:
    _verdict_action_table()
    _validation_test()
    _per_suite_independence()
    _false_resolve_ceiling_pinned()
    _security_outputs_have_no_suite()
    _suites_partition_quality_outputs()
    _reproduces_shipped_postures()
    _real_detector_finding_routes()
    _declaration_suites_and_layering()
    _declaration_rejects_bad_suites()
    _default_gate_yaml_is_all_comment()
    print(
        "US-104 verdict layer OK: per-suite off|comment|fail over severity; "
        f"{len(QUALITY_SUITES)} quality suites; false_resolve ceiling pinned"
    )


if __name__ == "__main__":
    # Allow `python -m evals.gate.test_verdict` from the repo root.
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    main()
