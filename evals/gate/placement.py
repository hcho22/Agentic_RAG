"""US-105 (Epic F, ADR-0005): the determinism → CI-placement rule.

US-101 tagged every eval output with a `class` (`security`/`quality`) AND an
orthogonal **determinism** flag (`deterministic`/`non_deterministic`). US-102/103/
104 built the loudness stack over the *class* axis. This module is the last gate
piece: it turns the **determinism** axis into the single rule that decides *where*
a gate may run and *what it may block*:

    A **deterministic** gate (pure arithmetic / a binary assert - recall@k / MRR /
    nDCG, the pinned E4/E6/AU4/E7-P1b invariants, the deterministic retrieval-gate
    tripwire) may run **per-PR** and a ``fail`` there **blocks the merge** (non-zero
    exit on the ``pull_request`` workflow).

    A **non-deterministic** gate (the four RAGAS scores, the runtime faithfulness
    gate, the full E7 deflection / false-resolve sweep - all LLM-judged) is
    structurally placed on a **scheduled** workflow only. Its ``fail`` = fail the
    scheduled run + file one issue per tag; it is **never** offered a per-PR
    ``fail``, because a judge wobble must never red-bar an innocent merge.

The rule is **the determinism axis, not buyer preference** (US-105's thesis): a
buyer cannot opt an LLM-judged gate into per-PR merge-blocking. The load-bearing
enforcement is :func:`validate_per_pr_target`, which the gate-declaration loader
(:mod:`evals.gate.declaration`) calls on every entry of a declaration's ``per_pr:``
section - a non-deterministic target there is a **structural load error**
(:class:`PlacementError`), rejected before the run ever starts (US-105 AC5), not a
runtime surprise.

Two orthogonal knobs, kept deliberately separate so neither can silently subsume
the other:

* **Loudness** (``suites:`` / ``verdicts:``, US-104) governs how a finding
  surfaces on whatever workflow it runs - ``off`` / ``comment`` / ``fail`` over
  ``red`` / ``yellow`` (see :func:`evals.gate.verdict.verdict_action`).
* **Placement** (``per_pr:``, this module) governs whether a *deterministic*
  quality gate's ``red`` finding **blocks the merge** on the per-PR workflow.
  Default: no gate blocks the merge via config - the security invariants block
  per-PR structurally (their own binary asserts, US-102), and the retrieval
  metrics stay advisory (the delta-vs-main comment, US-035). A buyer opts a
  deterministic quality gate into per-PR blocking by naming it under ``per_pr:``.

So the ``false_resolve`` ceiling - a *pinned* invariant (always ``block``, US-104
AC3) but a **non-deterministic** metric - blocks on the **scheduled** workflow
(files an issue), NOT per-PR: that is exactly the accepted faithfulness-leg
detection-latency gap US-106 documents, and the deterministic P1a/P1b
retrieval-leg tripwire is its per-PR mitigation.

Design note - import-light, like the sibling gate modules. It pulls the
determinism flag from the import-safe :mod:`evals.gate.classes` registry and the
quality-output → suite partition from the equally-light :mod:`evals.gate.verdict`;
it imports **none** of the heavy runner / e7_runner modules (asyncpg / openai /
httpx).

Run the tests: ``python -m evals.gate.test_placement``
"""

from __future__ import annotations

from typing import Optional

from evals.gate.classes import (
    DETERMINISTIC,
    SecurityGateError,
    gate_class,
    is_registered,
    quality_outputs,
)
from evals.gate.verdict import (
    BLOCK,
    RED,
    QUALITY_SUITES,
    SuiteVerdicts,
    action_for_finding,
    suite_for_finding,
    suite_for_output,
)
from evals.retrieval.ragas_gates import GateFinding

# ---------------------------------------------------------------------------
# The placement vocabulary (plain string constants - round-trip through YAML with
# no codec, mirroring classes.py's determinism/loudness constants).
# ---------------------------------------------------------------------------

# Where a gate runs and what its `fail` may do.
PER_PR = "per_pr"          # runs on the pull_request workflow; `fail` blocks the merge
SCHEDULED = "scheduled"    # runs on a scheduled workflow only; `fail` files an issue
_PLACEMENTS = frozenset({PER_PR, SCHEDULED})


class PlacementError(ValueError):
    """Raised when a config places a **non-deterministic** gate on the per-PR
    merge path (US-105 AC5).

    Subclasses :class:`ValueError` so the loader's existing malformed-declaration
    handling catches it, while a caller that wants the specific structural-error
    signal can still catch :class:`PlacementError` by type. It is a **build-time
    structural error** (the config is rejected before the run), never a runtime
    verdict - a judge-driven gate is simply not offered a per-PR ``fail``.
    """


# ---------------------------------------------------------------------------
# Suite determinism - computed from the registry so it can never drift. A suite is
# deterministic iff EVERY quality output in it is deterministic; adding a
# non-deterministic metric to a currently-deterministic suite flips it here
# automatically (no hand-maintained list to forget).
# ---------------------------------------------------------------------------


def _suite_members() -> "dict[str, tuple[str, ...]]":
    """Invert the quality-output → suite partition into suite → outputs."""
    members: "dict[str, list[str]]" = {suite: [] for suite in QUALITY_SUITES}
    for name in quality_outputs():
        members[suite_for_output(name)].append(name)
    return {suite: tuple(names) for suite, names in members.items()}


_SUITE_MEMBERS: "dict[str, tuple[str, ...]]" = _suite_members()


def suite_outputs(suite: str) -> "tuple[str, ...]":
    """Every quality output in ``suite`` (raises ``KeyError`` for an unknown suite)."""
    if suite not in _SUITE_MEMBERS:
        raise KeyError(
            f"{suite!r} is not a quality suite; known suites: {sorted(_SUITE_MEMBERS)}"
        )
    return _SUITE_MEMBERS[suite]


def is_quality_suite(name: str) -> bool:
    """True when ``name`` is one of the three quality suites (US-104)."""
    return name in _SUITE_MEMBERS


def output_is_deterministic(output: str) -> bool:
    """True when an eval output is a deterministic (arithmetic / binary) gate.

    Raises ``KeyError`` for an unregistered output (mirrors ``gate_class``).
    """
    return gate_class(output).determinism == DETERMINISTIC


def suite_is_deterministic(suite: str) -> bool:
    """True when EVERY quality output in ``suite`` is deterministic.

    So ``retrieval_metrics`` (recall@k / mrr / ndcg) is deterministic while
    ``ragas`` and ``escalation`` (LLM-judged) are not. Computed from the registry,
    so a future non-deterministic metric added to a deterministic suite flips this
    automatically. Raises ``KeyError`` for an unknown suite.
    """
    return all(output_is_deterministic(o) for o in suite_outputs(suite))


# ---------------------------------------------------------------------------
# The name → placement rule (works for a suite OR a registered output).
# ---------------------------------------------------------------------------


def target_is_deterministic(name: str) -> bool:
    """True when ``name`` - a quality suite OR a registered eval output - is a
    deterministic gate (and so eligible to block a merge per-PR).

    A ``security``-class output is deterministic but carries no tunable knob, so a
    caller must not route it through the placement config - see
    :func:`validate_per_pr_target`, which rejects a security output up front.
    Raises ``KeyError`` for a name that is neither a known suite nor a registered
    output.
    """
    if is_quality_suite(name):
        return suite_is_deterministic(name)
    if is_registered(name):
        return output_is_deterministic(name)
    raise KeyError(
        f"{name!r} is neither a quality suite {sorted(QUALITY_SUITES)} nor a "
        "registered eval output"
    )


def placement_for(name: str) -> str:
    """The structural CI placement of a gate - :data:`PER_PR` or :data:`SCHEDULED`.

    Deterministic gates place per-PR (their ``fail`` may block a merge);
    non-deterministic gates place on a scheduled workflow only. This is the single
    rule US-105 AC3 formalizes: the four-workflow split reduces to
    ``placement_for(gate)``.
    """
    return PER_PR if target_is_deterministic(name) else SCHEDULED


def may_block_merge(name: str) -> bool:
    """True when a ``fail`` on ``name`` is *allowed* to block a merge per-PR.

    Exactly the deterministic gates. This is the predicate US-105 enforces: a
    non-deterministic gate can never block a merge, whatever a buyer configures.
    """
    return target_is_deterministic(name)


def validate_per_pr_target(name: str) -> None:
    """Enforce the US-105 placement rule on one ``per_pr:`` declaration entry.

    Called by the gate-declaration loader for every gate a buyer tries to opt into
    per-PR merge-blocking. Raises:

    * :class:`SecurityGateError` - a ``security``-class output. It is pinned
      ``fail`` and blocks per-PR *structurally* (its own binary assert, US-102); it
      carries no tunable knob and must not appear in the placement config, mirroring
      the ``verdicts:`` / ``suites:`` security pin.
    * :class:`PlacementError` - a **non-deterministic** gate (a RAGAS/escalation
      suite or an LLM-judged output). This is the load-bearing structural rejection
      (US-105 AC5): a judge-driven gate is never offered a per-PR ``fail``.
    * ``KeyError`` - a name that is neither a known suite nor a registered output
      (an unknown target is a hard error, no silent skip).

    Returns ``None`` for a valid deterministic quality target (the loader then adds
    it to the per-PR merge-blocking set).
    """
    if is_registered(name) and gate_class(name).is_security:
        raise SecurityGateError(
            f"{name!r} is a security-class invariant, pinned `fail`; it blocks "
            "per-PR structurally (its own binary assert, US-102) and carries no "
            "tunable knob - it must not appear in the `per_pr:` placement config."
        )
    if not target_is_deterministic(name):
        placement = placement_for(name)  # SCHEDULED
        raise PlacementError(
            f"gate declaration requests a per-PR `fail` on {name!r}, but it is a "
            f"NON-DETERMINISTIC (LLM-judged) gate - its structural placement is "
            f"{placement!r} (scheduled-only). A per-PR `fail` here would let a judge "
            "wobble block an innocent merge (US-105). Non-deterministic gates fail "
            "the scheduled workflow + file an issue; they are never offered a per-PR "
            "`fail`. Remove it from `per_pr:` (only deterministic gates - "
            "retrieval_metrics / recall@k / mrr / ndcg - may block a merge)."
        )


# ---------------------------------------------------------------------------
# Finding-level predicates: does a detected finding block the MERGE (per-PR) or
# fail the SCHEDULED run (file an issue)? These consume the buyer's resolved
# per-PR set + loudness knobs; the declaration wires them onto GateDeclaration.
# ---------------------------------------------------------------------------


def finding_blocks_merge(
    finding: GateFinding,
    per_pr_targets: "frozenset[str]",
) -> bool:
    """True when ``finding`` should block the merge on the **per-PR** workflow.

    A finding blocks the merge iff it is:

    1. ``red`` (a hard regression - a ``yellow`` diagnostic never blocks), AND
    2. on a **deterministic** gate (a non-deterministic finding can NEVER block a
       merge, US-105's core guarantee - this is the structural short-circuit that
       makes the guarantee hold regardless of ``per_pr_targets``), AND
    3. explicitly opted into per-PR blocking - its own metric OR its suite is in
       ``per_pr_targets`` (the buyer's validated ``per_pr:`` set).

    This is decoupled from the loudness knob on purpose: ``per_pr:`` is the only
    switch that turns a deterministic quality gate into a per-PR merge blocker;
    the ``suites:`` / ``verdicts:`` loudness governs the *scheduled* surface (see
    :func:`finding_files_issue`). The security invariants block per-PR via their
    own asserts (US-102), not through this predicate.
    """
    if finding.severity != RED:
        return False
    suite = suite_for_finding(finding)
    # A non-deterministic finding (RAGAS / escalation / the false_resolve ceiling)
    # can never block a merge - the load-bearing US-105 guarantee.
    if not suite_is_deterministic(suite):
        return False
    metric = finding.metric
    if metric and is_registered(metric) and metric in per_pr_targets:
        return True
    return suite in per_pr_targets


def finding_files_issue(
    finding: GateFinding,
    suites: "Optional[SuiteVerdicts]" = None,
) -> bool:
    """True when ``finding`` should fail the **scheduled** run and file an issue.

    On a scheduled workflow a finding whose resolved action is :data:`BLOCK`
    (:func:`evals.gate.verdict.action_for_finding`, folding the per-suite loudness
    knob + the pinned ``false_resolve`` ceiling short-circuit) fails the workflow -
    which the scheduled workflows turn into "file one issue per tag" (today's
    ``retrieval-eval-ragas-weekly.yml`` behavior). This is the scheduled
    counterpart to :func:`finding_blocks_merge`: the SAME non-zero exit, but a
    scheduled run files an issue instead of blocking a merge.
    """
    return action_for_finding(finding, suites) == BLOCK
