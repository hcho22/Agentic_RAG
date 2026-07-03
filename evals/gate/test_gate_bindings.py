"""US-103 tests: project bindings extracted into a buyer-authored declaration.

The detection LAYER in ``evals/retrieval/ragas_gates.py`` is kept wholesale; only
its project-specific *bindings* (cells, the cross-family judge map / cell, the
threshold constants) move out of module scope into a :class:`GateBindings` config
object that ``evals/gate/gate.yaml`` serializes and ``evals/gate/declaration.py``
parses. This suite is the regression guard: it proves the genericized,
declaration-driven path is byte-identical to the legacy hardcoded path, and that
the optional corroboration binding degrades exactly as specified.

Always runs — no DB, no secrets, no heavy deps (stdlib + PyYAML + the import-light
``evals.gate`` / ``evals.retrieval.ragas_gates`` modules). It encodes the US-103
validation test directly:

  * ``default_bindings()`` reproduces today's constants, and the SHIPPED
    ``gate.yaml`` parses to a binding equal to it (AC3 — the shipped default is
    byte-identical to the legacy gate);
  * ``check_score_regressions`` run with the legacy constants (no bindings) and
    driven by the default ``gate.yaml`` produces IDENTICAL ``GateFinding`` lists
    (severity / tag / cross_family_corroborated / auto_close_weeks — and message)
    over the same RAGAS-drop + corroborated-Claude-drop fixtures (the validation
    test's core);
  * OMITTING the corroboration binding turns a would-be cross-family red into
    ``single-judge-red`` (AC4), with no other finding changing;
  * ``check_operational_gates`` / ``check_diagnostic_gates`` are likewise
    identical across the legacy and declaration-driven paths;
  * loading a declaration with an unknown cell / metric / section is a hard error
    (AC5 — no silent skip).

Run:
    python -m evals.gate.test_gate_bindings
"""

from __future__ import annotations

import dataclasses
import os
import sys
from pathlib import Path
from typing import Any, Tuple, Type, Union

from evals.gate.declaration import load_gate_declaration
from evals.retrieval.ragas_gates import (
    GateBindings,
    check_diagnostic_gates,
    check_operational_gates,
    check_score_regressions,
    default_bindings,
)

# The shipped default declaration.
GATE_YAML = Path(__file__).with_name("gate.yaml")

# The cross-family judge cell (== the default CLAUDE_JUDGE_CELL) and a second
# declared cell the judge does NOT cover.
JUDGE_CELL = "full_access:pre_filter"
OTHER_CELL = "partial_access:pre_filter"

_ExcSpec = Union[Type[BaseException], Tuple[Type[BaseException], ...]]


def _assert_raises(exc_type: _ExcSpec, fn, *, what: str) -> BaseException:
    try:
        fn()
    except exc_type as exc:
        return exc
    names = getattr(exc_type, "__name__", str(exc_type))
    raise AssertionError(f"expected {names} from {what}, none raised")


def _key(f: Any) -> "tuple[str, str, str, str, bool, int]":
    """The identity of a finding for order-insensitive comparison / display."""
    return (
        f.severity,
        f.tag,
        f.metric,
        f.cell,
        f.cross_family_corroborated,
        f.auto_close_weeks,
    )


# ---------------------------------------------------------------------------
# Fixtures — a RAGAS-drop + corroborated-Claude-drop history the score gate reads.
# ---------------------------------------------------------------------------


def _block(mean_strict: float, coverage: float = 1.0) -> "dict[str, Any]":
    return {"mean_strict": mean_strict, "coverage": coverage, "api_errors": 0}


def _by_cell(
    faith: float, ans_rel: float, ctx_prec: float, ctx_rec: float, coverage: float = 1.0
) -> "dict[str, Any]":
    """A `ragas.aggregates.by_cell` for one snapshot.

    The judged cell carries the four metric means; the OTHER cell is held flat so
    it never drops (keeping the finding set focused on the judged cell).
    """
    return {
        JUDGE_CELL: {
            "faithfulness": _block(faith, coverage),
            "answer_relevancy": _block(ans_rel, coverage),
            "context_precision": _block(ctx_prec, coverage),
            "context_recall": _block(ctx_rec, coverage),
        },
        OTHER_CELL: {
            "faithfulness": _block(0.90),
            "answer_relevancy": _block(0.90),
            "context_precision": _block(0.90),
            "context_recall": _block(0.90),
        },
    }


def _score_fixture() -> "tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]":
    """Return (current, history, custom_judge_history) for check_score_regressions.

    faithfulness: RAGAS drop (0.90 -> 0.80) AND Claude faithfulness drop
                  (4.5 -> 4.0) at the judged cell -> cross-family RED.
    context_precision: RAGAS drop (0.90 -> 0.80), no Claude equivalent
                  -> single-judge-red.
    answer_relevancy / context_recall: held flat -> no finding.
    """
    # 4 flat prior snapshots (median 0.90) — meets MIN_REGRESSION_HISTORY. Each
    # history entry is a prior run's `ragas.aggregates` = {"by_cell": ...}.
    history = [{"by_cell": _by_cell(0.90, 0.90, 0.90, 0.90)} for _ in range(4)]
    # Current run: faithfulness + context_precision fell below the 0.05 drop band.
    current = {
        "ragas": {"aggregates": {"by_cell": _by_cell(0.80, 0.90, 0.80, 0.90)}},
        "aggregates": {"by_mode": {"hybrid": {"faithfulness": 4.0, "helpfulness": 4.5}}},
    }
    # 4 flat prior Claude snapshots (median 4.5 faithfulness / 4.5 helpfulness).
    custom_judge_history = [
        {"by_mode": {"hybrid": {"faithfulness": 4.5, "helpfulness": 4.5}}}
        for _ in range(4)
    ]
    return current, history, custom_judge_history


# ---------------------------------------------------------------------------
# The US-103 validation test
# ---------------------------------------------------------------------------


def _default_bindings_reproduce_the_constants() -> None:
    """AC3: the SHIPPED gate.yaml parses to a binding equal to default_bindings()
    (today's constants) — the shipped default is byte-identical to the legacy gate."""
    assert GATE_YAML.is_file(), f"missing shipped default declaration: {GATE_YAML}"

    db = default_bindings()
    # Corroboration is ON by default (openai generator vs anthropic judge differ).
    assert db.corroboration_enabled is True
    assert db.cell_ids == (JUDGE_CELL, OTHER_CELL)
    assert db.claude_judge_cell == JUDGE_CELL
    assert db.effective_claude_equivalent() == {
        "faithfulness": ("faithfulness", 0.3),
        "answer_relevancy": ("helpfulness", 0.2),
    }

    from_file = load_gate_declaration(GATE_YAML).bindings
    assert from_file == db, _binding_diff(from_file, db)
    # A verdicts-only (or empty) declaration still yields the default bindings.
    assert load_gate_declaration({"verdicts": {"recall_at_5": "fail"}}).bindings == db
    assert load_gate_declaration({}).bindings == db


def _binding_diff(a: GateBindings, b: GateBindings) -> str:
    diffs = []
    for f in dataclasses.fields(a):
        av, bv = getattr(a, f.name), getattr(b, f.name)
        if av != bv:
            diffs.append(f"{f.name}: {av!r} != {bv!r}")
    return "GateBindings differ: " + "; ".join(diffs)


def _legacy_and_declaration_paths_are_identical() -> None:
    """The validation test's core: check_score_regressions with the legacy
    hardcoded constants (no bindings) and driven by the default gate.yaml produces
    IDENTICAL findings (severity / tag / cross_family_corroborated / auto_close_weeks
    — and message)."""
    current, history, cjh = _score_fixture()

    legacy = check_score_regressions(current, history, cjh)  # module constants
    from_yaml = check_score_regressions(
        current, history, cjh, bindings=load_gate_declaration(GATE_YAML).bindings
    )

    # The fixture must actually exercise the gate (else "identical" is vacuous).
    assert legacy, "score-regression fixture produced no findings"
    assert [_key(f) for f in legacy] == [
        ("red", "score-regression", "faithfulness", JUDGE_CELL, True, 1),
        ("red", "single-judge-red", "context_precision", JUDGE_CELL, False, 2),
    ], [_key(f) for f in legacy]

    # Byte-identical across the two code paths — full GateFinding equality (message
    # included), the AC3 regression guard.
    assert legacy == from_yaml, (
        [dataclasses.astuple(f) for f in legacy],
        [dataclasses.astuple(f) for f in from_yaml],
    )


def _omitting_corroboration_degrades_cross_family_red() -> None:
    """AC4 + the validation test's second expectation: omitting the corroboration
    binding turns the would-be cross-family faithfulness red into single-judge-red,
    changing NO other finding."""
    current, history, cjh = _score_fixture()
    db = default_bindings()

    # A real parsed declaration that OMITS the corroboration block (thresholds +
    # cells reproduce the defaults so nothing else moves).
    no_corr = load_gate_declaration(
        {
            "bindings": {
                "cells": list(db.cell_ids),
                "thresholds": {
                    "coverage_floor": db.coverage_floor,
                    "api_error_ceiling": db.api_error_ceiling,
                    "coverage_drift_pp": db.coverage_drift_pp,
                    "ragas_drop": db.ragas_drop,
                    "min_drift_history": db.min_drift_history,
                    "min_regression_history": db.min_regression_history,
                },
                # corroboration omitted -> disabled
            }
        }
    ).bindings
    assert no_corr.corroboration_enabled is False
    assert no_corr.effective_claude_equivalent() == {}

    degraded = check_score_regressions(current, history, cjh, bindings=no_corr)
    assert [_key(f) for f in degraded] == [
        # the cross-family red is now single-judge-red (auto_close_weeks 1 -> 2)
        ("red", "single-judge-red", "faithfulness", JUDGE_CELL, False, 2),
        # the context_precision single-judge-red is unchanged
        ("red", "single-judge-red", "context_precision", JUDGE_CELL, False, 2),
    ], [_key(f) for f in degraded]

    # The other AC4 trigger — judge_family == generator_family — disables it too.
    same_family = load_gate_declaration(
        {
            "bindings": {
                "cells": list(db.cell_ids),
                "corroboration": {
                    "generator_family": "openai",
                    "judge_family": "openai",  # same family -> not cross-family
                    "judge_cell": JUDGE_CELL,
                    "judge_equivalent": {
                        "faithfulness": {"judge_metric": "faithfulness", "drop": 0.3},
                    },
                },
            }
        }
    ).bindings
    assert same_family.corroboration_enabled is False
    same_family_findings = check_score_regressions(
        current, history, cjh, bindings=same_family
    )
    assert ("red", "score-regression", "faithfulness", JUDGE_CELL, True, 1) not in [
        _key(f) for f in same_family_findings
    ], "same-family corroboration must not produce a cross-family red"


def _operational_and_diagnostic_paths_are_identical() -> None:
    """The operational (fixed-floor) and diagnostic (rolling-window) gates are also
    identical across the legacy and declaration-driven paths."""
    yaml_bindings = load_gate_declaration(GATE_YAML).bindings

    # Operational: a cell below the coverage floor with API errors over the ceiling.
    op_cell = {
        "faithfulness": {"coverage": 0.50, "api_errors": 5},
        "answer_relevancy": {"coverage": 1.0, "api_errors": 5},
        "context_precision": {"coverage": 1.0, "api_errors": 5},
        "context_recall": {"coverage": 1.0, "api_errors": 5},
    }
    op_agg = {"by_cell": {JUDGE_CELL: op_cell}}
    op_legacy = check_operational_gates(op_agg)
    op_yaml = check_operational_gates(op_agg, yaml_bindings)
    assert op_legacy, "operational fixture produced no findings"
    assert op_legacy == op_yaml
    tags = {f.tag for f in op_legacy}
    assert "coverage-pipeline-failure" in tags and "coverage-operational-failure" in tags

    # Diagnostic: coverage drift below the rolling median by > the drift band.
    diag_history = [
        {"by_cell": {JUDGE_CELL: {"faithfulness": {"coverage": 1.0, "api_errors": 0}}}}
        for _ in range(3)
    ]
    diag_current = {
        "by_cell": {JUDGE_CELL: {"faithfulness": {"coverage": 0.90, "api_errors": 0}}}
    }
    diag_legacy = check_diagnostic_gates(diag_current, diag_history)
    diag_yaml = check_diagnostic_gates(diag_current, diag_history, yaml_bindings)
    assert diag_legacy, "diagnostic fixture produced no findings"
    assert diag_legacy == diag_yaml
    assert any(f.tag == "coverage-drift" for f in diag_legacy)


def _unknown_cell_or_metric_is_a_hard_error() -> None:
    """AC5: an unknown cell / metric / section is a hard error (no silent skip)."""
    cases = {
        # unknown metric name in the judge map
        "unknown metric": {
            "bindings": {
                "cells": [JUDGE_CELL],
                "corroboration": {
                    "generator_family": "openai",
                    "judge_family": "anthropic",
                    "judge_cell": JUDGE_CELL,
                    "judge_equivalent": {
                        "made_up_metric": {"judge_metric": "x", "drop": 0.3}
                    },
                },
            }
        },
        # typo'd judge_metric VALUE (the metric key is valid, but the Claude-side
        # metric name is misspelled) — would silently never corroborate, AC5.
        "typo'd judge_metric value": {
            "bindings": {
                "cells": [JUDGE_CELL],
                "corroboration": {
                    "generator_family": "openai",
                    "judge_family": "anthropic",
                    "judge_cell": JUDGE_CELL,
                    "judge_equivalent": {
                        "faithfulness": {"judge_metric": "faithfullness", "drop": 0.3}
                    },
                },
            }
        },
        # judge_cell not among the declared cells (dangling reference)
        "unknown judge cell": {
            "bindings": {
                "cells": [JUDGE_CELL],
                "corroboration": {
                    "generator_family": "openai",
                    "judge_family": "anthropic",
                    "judge_cell": "does_not_exist:pre_filter",
                    "judge_equivalent": {
                        "faithfulness": {"judge_metric": "faithfulness", "drop": 0.3}
                    },
                },
            }
        },
        "unknown bindings section": {"bindings": {"nonsense": 1}},
        "unknown threshold": {"bindings": {"thresholds": {"coverage_flooor": 0.9}}},
        "empty cell list": {"bindings": {"cells": []}},
        "incomplete corroboration": {
            "bindings": {"cells": [JUDGE_CELL], "corroboration": {"judge_cell": JUDGE_CELL}}
        },
        "non-number threshold": {"bindings": {"thresholds": {"ragas_drop": "lots"}}},
    }
    for label, decl in cases.items():
        _assert_raises(
            ValueError,
            lambda decl=decl: load_gate_declaration(decl),
            what=f"loading a declaration with {label}",
        )

    # A buyer MAY declare their own cells (genericization) — a non-default cell set
    # is accepted, and its judge_cell resolves against it.
    ok = load_gate_declaration(
        {
            "bindings": {
                "cells": ["team_a:pre_filter", "team_b:pre_filter"],
                "corroboration": {
                    "generator_family": "openai",
                    "judge_family": "anthropic",
                    "judge_cell": "team_b:pre_filter",
                    "judge_equivalent": {
                        "faithfulness": {"judge_metric": "faithfulness", "drop": 0.25}
                    },
                },
            }
        }
    ).bindings
    assert ok.cell_ids == ("team_a:pre_filter", "team_b:pre_filter")
    assert ok.claude_judge_cell == "team_b:pre_filter"
    assert ok.corroboration_enabled is True


def main() -> None:
    _default_bindings_reproduce_the_constants()
    _legacy_and_declaration_paths_are_identical()
    _omitting_corroboration_degrades_cross_family_red()
    _operational_and_diagnostic_paths_are_identical()
    _unknown_cell_or_metric_is_a_hard_error()
    print(
        "US-103 gate bindings OK: default gate.yaml reproduces the legacy constants; "
        "declaration-driven detection is byte-identical; omitting corroboration "
        "degrades cross-family red to single-judge-red; unknown cell/metric is a hard error"
    )


if __name__ == "__main__":
    # Allow `python -m evals.gate.test_gate_bindings` from the repo root.
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    main()
