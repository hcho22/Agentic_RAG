"""US-101 tests: the gate-class registry.

Two layers (mirrors the repo's other eval tests, e.g. test_e6.py):

1. **Offline unit checks** that ALWAYS run — no DB, no secrets, no heavy deps.
   They encode the US-101 validation test directly: every eval output resolves to
   exactly one class; security members reject a loudness knob and cannot be
   declared with one; `false_resolve` is quality-but-pinned-ceiling; the tunable
   escalation rates are plain quality.

2. **Best-effort drift guard** — when the heavy runner module happens to be
   importable, cross-check that the registry's recall `k` set matches
   runner.py::RECALL_KS. It skips cleanly (no failure) when runner's deps
   (asyncpg / openai / httpx) are absent, so the offline layer is never gated on
   a heavy import. The four RAGAS metric names are always cross-checked because
   ragas.py is stdlib-only.

Run:
    python -m evals.gate.test_classes
"""

from __future__ import annotations

import os
import sys

from evals.gate.classes import (
    CEILING_IS_INVARIANT,
    DEFAULT_LOUDNESS,
    DETERMINISTIC,
    NON_DETERMINISTIC,
    QUALITY,
    SECURITY,
    GateClass,
    SecurityGateError,
    all_outputs,
    gate_class,
    is_registered,
    quality_outputs,
    registry,
    security_outputs,
)
from evals.retrieval.ragas import RAGAS_METRICS


def _assert_raises(exc_type: "type[BaseException]", fn, *, what: str) -> None:
    try:
        fn()
    except exc_type:
        return
    raise AssertionError(f"expected {exc_type.__name__} from {what}, none raised")


def _validation_test() -> None:
    """The US-101 PRD validation test, step by step."""

    # -- Step 1: E4 zero-leak is a security invariant AND querying its loudness
    #    knob raises (there is no knob — it is pinned `fail`, US-102).
    e4 = gate_class("E4_zero_leak")
    assert e4.category == SECURITY, e4
    assert e4.is_security and not e4.is_quality
    assert e4.determinism == DETERMINISTIC, e4  # deterministic → per-PR (US-105)
    _assert_raises(
        SecurityGateError, lambda: e4.loudness, what="reading a security output's loudness"
    )

    # Every security member behaves the same way.
    for name in security_outputs():
        gc = gate_class(name)
        assert gc.category == SECURITY
        _assert_raises(
            SecurityGateError, lambda gc=gc: gc.loudness, what=f"{name}.loudness"
        )
        assert not gc.ceiling_pinned, f"{name} should not straddle a ceiling"

    # The exact security roster the AC pins: E4 / E6 / AU4 / E7-P1b.
    assert set(security_outputs()) == {
        "E4_zero_leak",
        "e6_workspace_boundary",
        "au4_auth_attacks",
        "e7_p1b_non_disclosure",
    }, security_outputs()

    # -- Step 2: recall@5 is a plain quality metric with a readable loudness knob.
    r5 = gate_class("recall_at_5")
    assert r5.category == QUALITY, r5
    assert r5.determinism == DETERMINISTIC, r5  # arithmetic → per-PR eligible
    assert r5.loudness == DEFAULT_LOUDNESS == "comment", r5.loudness
    assert not r5.ceiling_pinned

    # -- Step 3: false_resolve is quality AND straddles a pinned ceiling.
    fr = gate_class("false_resolve")
    assert fr.category == QUALITY, fr
    assert fr.straddle == CEILING_IS_INVARIANT, fr
    assert fr.ceiling_pinned, "false_resolve's ceiling breach must be pinned"
    assert fr.determinism == NON_DETERMINISTIC, fr  # LLM faithfulness leg

    # -- Step 4: deflection_rate / false_escalate_rate are plain tunable quality,
    #    while false_resolve is NOT silenceable (its ceiling is pinned).
    for name in ("deflection_rate", "false_escalate_rate"):
        gc = gate_class(name)
        assert gc.category == QUALITY, gc
        assert not gc.ceiling_pinned, f"{name} is a plain tunable metric"
        # A tunable metric's loudness is queryable (does not raise).
        assert gc.loudness == "comment", gc.loudness
    assert gate_class("false_resolve").ceiling_pinned, "false_resolve stays pinned"


def _registry_completeness() -> None:
    """AC1/AC2/AC3: every listed output is registered and resolves to one class."""

    # AC3 quality roster: recall_at_{1,3,5,10} / mrr / ndcg_at_5, the four RAGAS
    # scores, deflection_rate, false_escalate_rate, false_resolve.
    expected_quality = (
        {f"recall_at_{k}" for k in (1, 3, 5, 10)}
        | {"mrr", "ndcg_at_5"}
        | set(RAGAS_METRICS)
        | {"deflection_rate", "false_escalate_rate", "false_resolve"}
    )
    assert set(quality_outputs()) == expected_quality, (
        f"quality roster drift: {set(quality_outputs()) ^ expected_quality}"
    )

    # The four RAGAS scores are all present and quality (AC3, sourced from
    # RAGAS_METRICS so no drift).
    for metric in RAGAS_METRICS:
        assert gate_class(metric).category == QUALITY, metric

    # AC1: every eval output resolves to EXACTLY one class (no name in both rosters,
    # every registered name in exactly one).
    sec, qual = set(security_outputs()), set(quality_outputs())
    assert sec.isdisjoint(qual), sec & qual
    assert sec | qual == set(all_outputs()), "an output escaped classification"
    # Every registered output has a valid class + determinism.
    for name, gc in registry().items():
        assert gc.category in (SECURITY, QUALITY), (name, gc.category)
        assert gc.determinism in (DETERMINISTIC, NON_DETERMINISTIC), (name, gc.determinism)

    # Unknown lookups are a hard error (no silent skip).
    assert not is_registered("nope")
    _assert_raises(KeyError, lambda: gate_class("nope"), what="unknown output lookup")


def _import_time_build_error() -> None:
    """AC5: a `security` row declared with a loudness knob is a build error.

    Exercised by constructing a rogue registry through the same validator the
    module uses at import; a security row with a knob must be rejected.
    """
    from evals.gate import classes as classes_mod

    # A security row carrying an `off`/`comment` (or any) loudness knob must fail
    # the validator — the same assertion that guards the module at import time.
    rogue = GateClass("rogue_leak", SECURITY, DETERMINISTIC, loudness_default="comment")
    _assert_raises(
        SecurityGateError,
        lambda: classes_mod._validate_registry([rogue]),
        what="validating a security row that carries a loudness knob",
    )
    # A security row set to `off` is equally rejected.
    rogue_off = GateClass("rogue_leak2", SECURITY, DETERMINISTIC, loudness_default="off")
    _assert_raises(
        SecurityGateError,
        lambda: classes_mod._validate_registry([rogue_off]),
        what="validating a security row set to `off`",
    )
    # A duplicate name is rejected (exactly-one-class invariant).
    dup = [GateClass("dup", QUALITY, DETERMINISTIC), GateClass("dup", QUALITY, DETERMINISTIC)]
    _assert_raises(
        ValueError, lambda: classes_mod._validate_registry(dup), what="duplicate registration"
    )
    # A straddle on a security row is nonsensical and rejected.
    bad_straddle = GateClass(
        "bad", SECURITY, DETERMINISTIC, straddle=CEILING_IS_INVARIANT
    )
    _assert_raises(
        ValueError,
        lambda: classes_mod._validate_registry([bad_straddle]),
        what="straddle on a security row",
    )
    # The real module registry itself imported cleanly (it is validated at import).
    assert len(all_outputs()) >= 12, all_outputs()


def _frozen_dataclass() -> None:
    """GateClass is immutable — the pinned registry cannot be mutated in place."""
    gc = gate_class("E4_zero_leak")
    try:
        gc.category = QUALITY  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("GateClass should be frozen (immutable)")


def _drift_guard() -> None:
    """Best-effort: cross-check recall Ks against runner.py::RECALL_KS when the
    heavy runner module is importable. Skips cleanly otherwise."""
    registered_ks = sorted(
        int(n.rsplit("_", 1)[1]) for n in all_outputs() if n.startswith("recall_at_")
    )
    try:
        from evals.retrieval.runner import RECALL_KS  # heavy (asyncpg/openai/httpx)
    except Exception as exc:  # ImportError or a missing-dep chain
        print(f"drift guard: runner not importable ({type(exc).__name__}) — skipping recall-K check")
        return
    assert registered_ks == sorted(RECALL_KS), (
        f"recall-K drift: registry {registered_ks} vs runner.RECALL_KS {sorted(RECALL_KS)}"
    )
    print(f"drift guard OK: recall Ks {registered_ks} match runner.RECALL_KS")


def main() -> None:
    _validation_test()
    _registry_completeness()
    _import_time_build_error()
    _frozen_dataclass()
    _drift_guard()
    print(
        f"US-101 gate-class registry OK: "
        f"{len(security_outputs())} security + {len(quality_outputs())} quality outputs"
    )


if __name__ == "__main__":
    # Allow `python -m evals.gate.test_classes` from the repo root.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    main()
