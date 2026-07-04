"""US-104 (Epic F, ADR-0005): the verdict layer — a per-suite `off|comment|fail`
loudness knob mapped over the existing `red`/`yellow` severity.

US-101 classified every eval output; US-102 pinned the `security` class to `fail`;
US-103 lifted the detection *bindings* into a buyer-authored declaration. This
module is the last piece: the thin, **detector-agnostic** layer that turns a
detected finding's severity into an *action* under one buyer knob per quality
suite. It does NOT detect anything — the findings are computed exactly as before
(``evals.retrieval.ragas_gates`` and friends); the knob only changes how loud a
finding is, never *which* findings exist (US-104 AC: "Loudness changes the action
surface only; detection output is identical across knob values").

Three concepts:

* **Severity** (``red`` / ``yellow``) — produced by the detector, unchanged.
  ``red`` is a hard regression; ``yellow`` is a diagnostic (see ``GateFinding``).
* **Knob** (``off`` / ``comment`` / ``fail``, default ``comment``) — the buyer's
  per-suite loudness setting, lifted straight from US-101's loudness vocabulary.
* **Action** (``block`` / ``comment`` / ``none``) — what the CI surface does with
  the finding. ``block`` fails the run (non-zero exit / blocked merge); ``comment``
  posts a diagnostic but never blocks; ``none`` stays silent.

The ``(severity, knob) -> action`` table (US-104 AC1) is:

    ================  =======  ==========  =======
    knob \\ severity   red      yellow      (else)
    ================  =======  ==========  =======
    fail              block    comment     —
    comment           comment  comment     —
    off               none     none        —
    ================  =======  ==========  =======

so the two postures the repo already ships are two values of one knob (AC4):

* the **weekly** ``runner.py::amain`` red→exit-non-zero / yellow-never-fails
  posture is ``fail``;
* the **PR** ``ci/diff_results.py`` comment-only / never-blocks posture is
  ``comment``.

The knob is **per quality suite** (AC2), NOT a single global flag — a buyer can
run the RAGAS suite at ``fail`` weekly while the retrieval-metrics suite stays
``comment``. The three suites partition every *quality* output
(:data:`QUALITY_SUITES`); a ``security``-class output is in **no** suite (it is
pinned ``fail`` with no knob, US-102), and ``false_resolve``'s **ceiling breach**
is a pinned invariant that ignores the knob entirely (AC3) — see
:data:`TAG_FALSE_RESOLVE_CEILING` / :func:`action_for_finding`.

Design note — import-light, like the sibling gate modules. It pulls the quality
output → suite partition from the import-safe :mod:`evals.gate.classes` registry
and :data:`evals.retrieval.ragas.RAGAS_METRICS`, and the ``GateFinding`` shape
from the import-light :mod:`evals.retrieval.ragas_gates`. It imports **none** of
the heavy runner / e7_runner modules (asyncpg / openai / httpx). The
false-resolve ceiling verdict lives in ``e7_runner`` (heavy), so
:func:`false_resolve_ceiling_finding` takes plain scalars rather than importing
that type — the caller (US-105 wiring) converts its ``FalseResolveCeilingVerdict``
at the call site.

Run the tests: ``python -m evals.gate.test_verdict``
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional

from evals.gate.classes import (
    _LOUDNESS_VALUES,
    DEFAULT_LOUDNESS,
    SecurityGateError,
    gate_class,
    is_registered,
    quality_outputs,
)
from evals.retrieval.ragas import RAGAS_METRICS
from evals.retrieval.ragas_gates import GateFinding

# ---------------------------------------------------------------------------
# Vocabularies (plain string constants — round-trip through YAML with no codec,
# mirroring classes.py's loudness/determinism constants).
# ---------------------------------------------------------------------------

# Severities the detector emits (mirrors GateFinding.severity). Kept here so the
# verdict function can validate its input without importing the detector's guts.
RED = "red"
YELLOW = "yellow"
_SEVERITIES = frozenset({RED, YELLOW})

# The ACTION surface — what CI does with a finding.
BLOCK = "block"      # fail the run / block the merge (non-zero exit)
COMMENT = "comment"  # post a diagnostic, never blocks
NONE = "none"        # stay silent
_ACTIONS = frozenset({BLOCK, COMMENT, NONE})

# The three QUALITY suites (US-104 AC2). Every quality output belongs to exactly
# one; a security output belongs to none. Names are the buyer-facing knob keys in
# a declaration's `suites:` section.
RETRIEVAL_METRICS = "retrieval_metrics"  # recall@k / mrr / ndcg — deterministic
RAGAS = "ragas"                          # the four RAGAS scores + coverage gates
ESCALATION = "escalation"                # deflection / false_escalate / false_resolve
QUALITY_SUITES: "tuple[str, ...]" = (RETRIEVAL_METRICS, RAGAS, ESCALATION)
_SUITE_SET = frozenset(QUALITY_SUITES)

# The distinguished tag a `false_resolve` CEILING-breach finding carries. US-104
# AC3: a ceiling breach maps to `fail` REGARDLESS of the escalation suite's knob —
# the buyer sets the ceiling VALUE (US-050) but cannot configure the gate to
# ignore a breach of their own tolerance. `action_for_finding` short-circuits this
# tag to BLOCK before any knob is consulted. (This is orthogonal to a buyer setting
# `false_resolve`'s ORDINARY loudness knob, which US-102 still allows — that knob
# governs the metric's non-ceiling surfacing, never the pinned ceiling.)
TAG_FALSE_RESOLVE_CEILING = "false-resolve-ceiling"


# ---------------------------------------------------------------------------
# Quality output → suite partition, built from the US-101 registry so it can
# never drift: a new quality output added to classes.py without a suite here is a
# LOUD import error, not a silent "unclassified" finding.
# ---------------------------------------------------------------------------


def _partition_quality_outputs() -> "dict[str, str]":
    """Assign every quality output to exactly one suite (US-104 AC2).

    Sourced from :func:`evals.gate.classes.quality_outputs` so membership can
    never drift from the registry. A quality output that matches none of the three
    suites is a hard error at import — the drift guard that forces US-105+ to place
    any new metric into a suite deliberately.
    """
    mapping: "dict[str, str]" = {}
    for name in quality_outputs():
        if name.startswith("recall_at_") or name in ("mrr", "ndcg_at_5"):
            mapping[name] = RETRIEVAL_METRICS
        elif name in RAGAS_METRICS:
            mapping[name] = RAGAS
        elif name in ("deflection_rate", "false_escalate_rate", "false_resolve"):
            mapping[name] = ESCALATION
        else:  # pragma: no cover - drift guard; only trips when a metric is added
            raise ValueError(
                f"quality output {name!r} is not assigned to a verdict suite "
                f"(one of {list(QUALITY_SUITES)}). Every quality output must map to "
                "exactly one suite — assign it in evals/gate/verdict.py (US-104)."
            )
    return mapping


_SUITE_OF_OUTPUT: "dict[str, str]" = _partition_quality_outputs()


def suite_for_output(output: str) -> str:
    """The quality suite an eval output belongs to.

    Raises :class:`SecurityGateError` for a ``security``-class output — a security
    invariant is pinned ``fail`` and is in NO suite (US-102), so asking for its
    suite is a programming error, not a silent default. Raises ``KeyError`` for an
    unregistered output (mirrors ``gate_class``).
    """
    gc = gate_class(output)  # KeyError for an unknown output (no silent skip)
    if gc.is_security:
        raise SecurityGateError(
            f"{output!r} is a security-class invariant, pinned `fail`; it belongs "
            "to no verdict suite and carries no loudness knob (US-102)."
        )
    return _SUITE_OF_OUTPUT[output]


def suite_for_finding(finding: GateFinding) -> str:
    """The quality suite a detected finding belongs to.

    A finding whose ``metric`` names a registered quality output resolves via
    :func:`suite_for_output`. A **cell-level** finding (``metric == ""`` — the
    API-error checks, whose total is cell-wide) originates in the RAGAS coverage
    detector, so it defaults to the :data:`RAGAS` suite. A finding carrying a
    ``security``-output metric is a bug (the detector never emits one) and raises.
    """
    metric = finding.metric
    if metric and is_registered(metric):
        # Raises SecurityGateError if a finding somehow names a security output —
        # a GateFinding should never do so; fail loud rather than misclassify.
        return suite_for_output(metric)
    # Cell-level (metric == "") or an unregistered metric string: these come from
    # the RAGAS coverage/operational detector, so they are RAGAS-suite.
    return RAGAS


# ---------------------------------------------------------------------------
# The core (severity, knob) -> action verdict function (US-104 AC1).
# ---------------------------------------------------------------------------


def verdict_action(severity: str, knob: str) -> str:
    """Map a finding's ``severity`` under a suite's ``knob`` to a CI ``action``.

    The pure heart of US-104 (AC1). The mapping:

    * ``fail``    → ``red`` blocks, ``yellow`` comments;
    * ``comment`` → both ``red`` and ``yellow`` comment (nothing blocks);
    * ``off``     → nothing posts.

    Raises ``ValueError`` on an unknown severity or knob — a typo is a loud error,
    never a silent no-op. The knob is one of :data:`evals.gate.classes._LOUDNESS_VALUES`.
    """
    if severity not in _SEVERITIES:
        raise ValueError(
            f"unknown severity {severity!r}; expected one of {sorted(_SEVERITIES)}"
        )
    if knob not in _LOUDNESS_VALUES:
        raise ValueError(
            f"unknown loudness knob {knob!r}; expected one of {sorted(_LOUDNESS_VALUES)}"
        )
    if knob == "off":
        return NONE
    if knob == "comment":
        return COMMENT
    # knob == "fail": red is a hard block, yellow is a diagnostic comment.
    return BLOCK if severity == RED else COMMENT


def blocks(action: str) -> bool:
    """True when ``action`` fails the run / blocks the merge (``block``)."""
    if action not in _ACTIONS:
        raise ValueError(
            f"unknown action {action!r}; expected one of {sorted(_ACTIONS)}"
        )
    return action == BLOCK


def posts(action: str) -> bool:
    """True when ``action`` surfaces the finding (``block`` or ``comment``)."""
    if action not in _ACTIONS:
        raise ValueError(
            f"unknown action {action!r}; expected one of {sorted(_ACTIONS)}"
        )
    return action in (BLOCK, COMMENT)


# ---------------------------------------------------------------------------
# Per-suite knob config (US-104 AC2) + finding → action resolution.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SuiteVerdicts:
    """A buyer's per-suite loudness knobs (US-104 AC2).

    ``knobs`` holds ONLY the suites the buyer set explicitly; an unset suite takes
    the registry default (:data:`evals.gate.classes.DEFAULT_LOUDNESS` = ``comment``)
    via :meth:`knob_for`. So the empty ``SuiteVerdicts()`` is the shipped default:
    every quality suite at ``comment`` (the PR comment-only posture). It is
    validated at construction — an unknown suite or knob is a hard error.
    """

    knobs: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for suite, knob in self.knobs.items():
            if suite not in _SUITE_SET:
                raise ValueError(
                    f"unknown quality suite {suite!r}; expected one of "
                    f"{list(QUALITY_SUITES)}"
                )
            if knob not in _LOUDNESS_VALUES:
                raise ValueError(
                    f"suite {suite!r} has unknown loudness knob {knob!r}; expected "
                    f"one of {sorted(_LOUDNESS_VALUES)}"
                )

    def knob_for(self, suite: str) -> str:
        """The loudness knob for ``suite`` (its explicit value, else ``comment``)."""
        if suite not in _SUITE_SET:
            raise ValueError(
                f"unknown quality suite {suite!r}; expected one of {list(QUALITY_SUITES)}"
            )
        return self.knobs.get(suite, DEFAULT_LOUDNESS)


def action_for_finding(
    finding: GateFinding, suites: "Optional[SuiteVerdicts]" = None
) -> str:
    """Resolve one detected finding to a CI action under the per-suite knobs.

    The single entry point US-104 offers over a bare :class:`SuiteVerdicts`:

    * A **``false_resolve`` ceiling breach** (``finding.tag ==``
      :data:`TAG_FALSE_RESOLVE_CEILING`) maps to :data:`BLOCK` **regardless** of
      the escalation suite's knob — the pinned-invariant short-circuit (AC3).
    * Otherwise the finding's suite (:func:`suite_for_finding`) picks the knob and
      :func:`verdict_action` maps ``(severity, knob) -> action``.

    ``suites`` defaults to the all-``comment`` :class:`SuiteVerdicts` (the shipped
    PR posture). This function is a pure read of the finding — it never mutates it,
    so the detected findings are byte-identical across knob values (AC:
    "Loudness changes the action surface only").
    """
    if suites is None:
        suites = SuiteVerdicts()
    if finding.tag == TAG_FALSE_RESOLVE_CEILING:
        # AC3: the false-resolve ceiling is a pinned safety invariant. A breach
        # blocks the run regardless of the escalation suite's loudness knob — the
        # buyer picks the ceiling VALUE, not whether a breach is enforced.
        return BLOCK
    knob = suites.knob_for(suite_for_finding(finding))
    return verdict_action(finding.severity, knob)


def run_should_block(
    findings: "Iterable[GateFinding]", suites: "Optional[SuiteVerdicts]" = None
) -> bool:
    """True when ANY finding resolves to :data:`BLOCK` under ``suites`` (AC4).

    This is the run-level exit-code predicate the two shipped postures reduce to:

    * with the RAGAS suite at ``fail`` a red RAGAS finding blocks — reproducing
      ``runner.py::amain``'s ``red_findings -> return 1`` (yellow never blocks);
    * with every suite at ``comment`` nothing blocks — reproducing
      ``ci/diff_results.py``'s comment-only posture.
    """
    return any(action_for_finding(f, suites) == BLOCK for f in findings)


def false_resolve_ceiling_finding(
    *,
    rate: "Optional[float]",
    ceiling: float,
    numerator: int,
    denominator: int,
    message: "Optional[str]" = None,
) -> GateFinding:
    """Build the distinguished ``false_resolve`` ceiling-breach finding (AC3).

    The caller (US-059's ``assert_false_resolve_ceiling`` verdict, wired in US-105)
    constructs this only when the ceiling is BREACHED, so the returned finding is
    always ``red`` and carries :data:`TAG_FALSE_RESOLVE_CEILING` — which
    :func:`action_for_finding` maps to :data:`BLOCK` regardless of the escalation
    knob. Takes plain scalars (not ``e7_runner``'s ``FalseResolveCeilingVerdict``)
    to keep this module free of the heavy runner import.
    """
    if message is None:
        rate_str = "—" if rate is None else f"{rate:.4f}"
        message = (
            f"false-resolve ceiling breached: rate {rate_str} "
            f"({numerator}/{denominator}) > ceiling {ceiling:.4f} — pinned safety "
            "invariant, blocks regardless of the escalation loudness knob (US-104 AC3)."
        )
    return GateFinding(
        severity=RED,
        tag=TAG_FALSE_RESOLVE_CEILING,
        metric="false_resolve",
        cell="",
        message=message,
    )
