"""US-102 (Epic F, ADR-0005): pin the security/correctness gate to `fail`.

This is the **gate-declaration loader**. A buyer authors a declaration (a YAML
file that ships beside their golden set, US-103) whose ``verdicts:`` block sets a
per-output loudness knob (``off | comment | fail``, US-104) over the **quality**
metrics. This loader enforces the single US-102 invariant:

    A ``security``-class output (E4 / E6 / AU4 / E7-P1b) can NEVER appear in that
    verdict map. Security invariants are pinned ``fail`` and are simply *not
    present in the tunable verdict space* — the only way to stop one is to
    **delete** its eval/golden labels (a loud, tracked diff, US-110), never a
    quiet config flag.

So a declaration that tries ``E4_zero_leak: off`` (or ``e6_workspace_boundary:
comment``) is a **hard load error** with an actionable message, not a silent
downgrade. This is what makes "the security gates cannot be turned off, and here
is the eval that proves it" a true statement a buyer's security reviewer can
audit.

Scope discipline (US-102 + US-103):

* US-102 owns the ``verdicts:`` section — the tunable loudness map — and the
  security pin (a ``security``-class output can never carry a verdict).
* US-103 adds the ``bindings:`` section — the detection layer's project-specific
  bindings (cells, the cross-family judge map / cell, threshold constants) that
  ``evals.retrieval.ragas_gates`` used to hardcode as module constants. This
  loader parses them into a :class:`~evals.retrieval.ragas_gates.GateBindings`
  (the object the three detection functions take), with a hard error on an
  unknown cell / metric / section (no silent skip). The default declaration
  reproduces today's constants byte-for-byte, so the genericized path is
  identical to the legacy one (the regression guard lives in
  ``evals/gate/test_gate_bindings.py``).
* US-104 (landed) adds the ``suites:`` section — one ``off|comment|fail`` knob per
  quality suite — and the ``(severity, knob) -> action`` verdict layer
  (:mod:`evals.gate.verdict`) that consumes both ``suites`` and ``verdicts``. It
  extends ``_KNOWN_SECTIONS`` / this dataclass without relaxing the security pin
  (a security output is in no suite, so a ``suites:`` key can never name one).
* US-105 (landed) adds the ``per_pr:`` section - the **placement** axis
  (:mod:`evals.gate.placement`): the buyer opts a *deterministic* quality gate
  (a suite or output) into per-PR merge-blocking (``fail``). The loader rejects a
  **non-deterministic** target there with :class:`~evals.gate.placement.PlacementError`
  - a **structural** load error (a judge wobble must never red-bar an innocent
  merge, AC5), the placement counterpart to US-102's security pin. Loudness
  (``suites:`` / ``verdicts:``) and placement (``per_pr:``) stay orthogonal: the
  former governs how a finding surfaces on whatever workflow it runs, the latter
  whether a deterministic quality finding blocks the *merge* per-PR.
* A verdict key is a registered **eval-output name** (matching US-102's own
  validation test, which authors ``E4_zero_leak: off``). US-104's ``suites:``
  knobs are the coarse per-suite grouping; the per-output ``verdicts`` map is the
  finer override layered ON TOP (see :meth:`GateDeclaration.action_for_finding` /
  :meth:`GateDeclaration.resolve_knob`). A security-output key stays rejected at
  either granularity, because a security output is in no suite.

The class name for the loudness value is the AC's ``off | comment | fail`` verdict
(spelled ``loudness`` in :mod:`evals.gate.classes`, off the reserved ``class``
keyword there).

Run the tests: ``python -m evals.gate.test_pinned_security``
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Union

from evals.gate.classes import (
    _LOUDNESS_VALUES,
    SecurityGateError,
    gate_class,
    is_registered,
)
from evals.gate.placement import (
    finding_blocks_merge,
    finding_files_issue,
    validate_per_pr_target,
)
from evals.gate.verdict import (
    BLOCK,
    QUALITY_SUITES,
    TAG_FALSE_RESOLVE_CEILING,
    SuiteVerdicts,
    suite_for_finding,
    suite_for_output,
    verdict_action,
)
from evals.retrieval.ragas import RAGAS_METRICS
from evals.retrieval.ragas_gates import GateBindings, GateFinding, default_bindings

# The top-level sections the loader knows. US-102 shipped `verdicts`; US-103 adds
# `bindings` (the detection layer's cells / judge map / thresholds); US-104 adds
# `suites` (the per-suite `off|comment|fail` loudness knobs); US-105 adds `per_pr`
# (the placement axis - which deterministic quality gates block a merge per-PR).
# Kept a frozen set so an unknown / misspelled top-level key is a loud error,
# never a silent skip.
_KNOWN_SECTIONS = frozenset({"verdicts", "bindings", "suites", "per_pr"})

# The per-PR placement verdict. `per_pr:` is exclusively about the merge-blocking
# verdict - a deterministic quality gate named here `fail`s the per-PR workflow
# (blocks the merge). `comment` / `off` are loudness knobs (`suites:` / `verdicts:`),
# not placements, so the only accepted value is `fail`; anything else is a hard
# error pointing the buyer at the loudness sections.
_PER_PR_VALUE = "fail"

# The per-suite knob keys the `suites:` section accepts (US-104). An unknown /
# misspelled suite is a hard error, never a silent skip (a typo'd `ragass: off`
# would otherwise leave the RAGAS suite silently at its `comment` default).
_SUITE_KEYS = frozenset(QUALITY_SUITES)

# The sub-keys each `bindings` block accepts (US-103). An unknown sub-key is a hard
# error — a typo'd threshold name would otherwise be silently ignored, leaving the
# default in force while the buyer believes they changed it.
_BINDINGS_KEYS = frozenset({"cells", "thresholds", "corroboration"})
_THRESHOLD_KEYS = frozenset(
    {
        "coverage_floor",
        "api_error_ceiling",
        "coverage_drift_pp",
        "ragas_drop",
        "min_drift_history",
        "min_regression_history",
    }
)
# `judge_metric` / `drop` name the corroborating judge's metric and its drop
# threshold; `judge_cell` is the one cell it covers; the families gate whether
# corroboration is cross-family (enabled) or same-family (degraded, US-103 AC4).
_CORROBORATION_KEYS = frozenset(
    {"generator_family", "judge_family", "judge_cell", "judge_equivalent"}
)
_JUDGE_EQUIVALENT_KEYS = frozenset({"judge_metric", "drop"})

# The Claude judge's output metric names — the universe a `judge_metric` VALUE must
# name. runner.py's judge tool schema requires exactly these two
# (`generation_keys = ["faithfulness", "helpfulness"]`), and they are the only keys
# `by_mode[RAGAS_MODE]` carries, so a `judge_metric` that is not one of these would
# make `_claude_metric_dropped` look up a missing key, get None, and silently never
# corroborate. Pinned LOCALLY (like classes.py::_RECALL_KS) to keep this loader
# import-light — importing the heavy runner.py (asyncpg/openai/httpx) for two
# strings would defeat that; the drift is guarded by the shipped gate.yaml, which
# uses exactly these values and must equal default_bindings().
_JUDGE_METRIC_NAMES = frozenset({"faithfulness", "helpfulness"})

# The exact, audit-quotable message a downgrade attempt produces. Security
# reviewers grep for this; keep the wording stable.
_PINNED_MESSAGE = (
    "security gates are pinned `fail` and cannot be downgraded; "
    "delete the eval to remove it"
)


@dataclass(frozen=True)
class GateDeclaration:
    """A loaded, validated gate declaration.

    ``verdicts`` holds ONLY the buyer's explicit per-output loudness settings for
    **quality** outputs (a security output can never appear — the loader rejects
    it). It is intentionally the explicit map, not a fully-materialized one; read
    a resolved value (with the registry default filled in) through
    :meth:`verdict_for`, which also re-pins security outputs so a caller can never
    read a loudness for one.

    ``bindings`` is the detection layer's project bindings (US-103) — the cells,
    cross-family judge map / cell, and threshold constants the three
    ``ragas_gates`` detection functions take as config. When the ``bindings:``
    section is absent it defaults to :func:`~evals.retrieval.ragas_gates.default_bindings`
    (today's constants), so a verdicts-only declaration behaves exactly as before.

    ``suites`` is the per-suite loudness knobs (US-104) — one ``off|comment|fail``
    per quality suite (retrieval-metrics / RAGAS / escalation). When the ``suites:``
    section is absent every suite takes its ``comment`` default (the shipped PR
    posture). :meth:`action_for_finding` layers the per-output ``verdicts`` map as
    a finer override on top of these per-suite knobs.

    ``per_pr`` is the placement set (US-105) - the *deterministic* quality gates
    (suite and/or output names) the buyer opted into per-PR merge-blocking. The
    loader guarantees every member is deterministic (a non-deterministic target is
    a :class:`~evals.gate.placement.PlacementError` at load), so
    :meth:`blocks_merge` can trust that a red finding on one of these blocks the
    merge. When the ``per_pr:`` section is absent this is empty - no quality gate
    blocks the merge via config (the security invariants block per-PR through their
    own asserts, US-102; the retrieval metrics stay advisory, US-035).
    """

    verdicts: Mapping[str, str]
    bindings: GateBindings = field(default_factory=default_bindings)
    suites: SuiteVerdicts = field(default_factory=SuiteVerdicts)
    per_pr: "frozenset[str]" = field(default_factory=frozenset)

    def verdict_for(self, output: str) -> str:
        """The resolved per-output loudness verdict for ``output``.

        Returns the buyer's explicit per-output setting when present, else the
        registry default (``comment``). This is the US-102 per-output resolver — it
        does NOT consult the per-suite knobs (use :meth:`resolve_knob` for the full
        US-104 layering). Raises :class:`SecurityGateError` for a ``security``-class
        output — there is no knob to read, it is pinned ``fail`` (mirrors
        ``GateClass.loudness``). Raises ``KeyError`` for an unregistered output.
        """
        gc = gate_class(output)  # KeyError for an unknown output (no silent skip)
        if gc.is_security:
            raise SecurityGateError(
                f"{output!r} is a security-class invariant, pinned `fail`; "
                f"{_PINNED_MESSAGE} (US-102)."
            )
        if output in self.verdicts:
            return self.verdicts[output]
        return gc.loudness  # the registry default for a quality output

    def resolve_knob(self, output: str) -> str:
        """The effective loudness knob for ``output`` under the US-104 layering.

        Precedence (finest wins): an explicit per-output ``verdicts`` setting, else
        the output's per-suite knob (:meth:`SuiteVerdicts.knob_for`), else the
        ``comment`` default. So a buyer can set a whole suite to ``off`` and still
        override one output back to ``fail``. Raises :class:`SecurityGateError` for
        a ``security``-class output (no knob) and ``KeyError`` for an unregistered
        output — the same pins as :meth:`verdict_for`.
        """
        gc = gate_class(output)  # KeyError for an unknown output (no silent skip)
        if gc.is_security:
            raise SecurityGateError(
                f"{output!r} is a security-class invariant, pinned `fail`; "
                f"{_PINNED_MESSAGE} (US-102)."
            )
        if output in self.verdicts:
            return self.verdicts[output]  # per-output override wins
        return self.suites.knob_for(suite_for_output(output))

    def action_for_finding(self, finding: GateFinding) -> str:
        """Resolve one detected finding to a CI action (``block``/``comment``/``none``).

        The US-104 verdict layer, with the declaration's config folded in:

        * a ``false_resolve`` **ceiling breach** (tag
          :data:`~evals.gate.verdict.TAG_FALSE_RESOLVE_CEILING`) maps to ``block``
          regardless of any knob — the pinned-invariant short-circuit (AC3);
        * otherwise the finding's effective knob is resolved with the same
          per-output-over-per-suite precedence as :meth:`resolve_knob` (a per-output
          ``verdicts`` entry for the finding's metric overrides its suite knob), and
          :func:`~evals.gate.verdict.verdict_action` maps ``(severity, knob)``.

        Pure read — the finding is never mutated, so detection output is identical
        across knob values (US-104 AC: "Loudness changes the action surface only").
        """
        if finding.tag == TAG_FALSE_RESOLVE_CEILING:
            return BLOCK
        metric = finding.metric
        if (
            metric
            and is_registered(metric)
            and not gate_class(metric).is_security
            and metric in self.verdicts
        ):
            knob = self.verdicts[metric]  # per-output override
        else:
            knob = self.suites.knob_for(suite_for_finding(finding))
        return verdict_action(finding.severity, knob)

    def blocks_merge(self, finding: GateFinding) -> bool:
        """True when ``finding`` should block the merge on the **per-PR** workflow.

        The US-105 placement predicate: a red finding blocks the merge iff it is on
        a **deterministic** gate (a non-deterministic finding can NEVER block a
        merge - the load-bearing guarantee) that the buyer opted into per-PR
        blocking via this declaration's ``per_pr:`` set (its metric or its suite).
        Independent of the loudness knob - ``per_pr:`` is the only switch that turns
        a deterministic quality gate into a per-PR merge blocker (see
        :meth:`files_issue` for the scheduled surface). The security invariants
        block per-PR through their own binary asserts (US-102), not this predicate.
        """
        return finding_blocks_merge(finding, self.per_pr)

    def files_issue(self, finding: GateFinding) -> bool:
        """True when ``finding`` should fail a **scheduled** run and file an issue.

        The scheduled counterpart to :meth:`blocks_merge`: a finding whose resolved
        loudness action is :data:`~evals.gate.verdict.BLOCK` (folding the per-suite
        knob + the pinned ``false_resolve`` ceiling short-circuit) fails the
        scheduled workflow, which files one issue per tag (today's
        ``retrieval-eval-ragas-weekly.yml`` behavior). Same non-zero exit as a
        per-PR block, but a scheduled run files an issue instead of blocking a
        merge - so an LLM-judged regression is caught weekly, never per-PR.
        """
        return finding_files_issue(finding, self.suites)


def _coerce_source(source: "Union[str, Path, Mapping[str, Any]]") -> "Mapping[str, Any]":
    """Read the declaration into a plain mapping.

    Accepts a path (``str``/``Path`` to a YAML file) or an already-parsed mapping
    (handy for tests). A path is parsed with ``yaml.safe_load``; a non-mapping
    document (e.g. a bare list, or an empty file) is a hard error.
    """
    if isinstance(source, Mapping):
        return source
    path = Path(source)
    import yaml  # type: ignore[import-untyped]  # local import: keep module import-light

    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        # An empty declaration is a valid no-op (all defaults), not an error.
        return {}
    if not isinstance(data, Mapping):
        raise ValueError(
            f"gate declaration {str(path)!r} must be a mapping at the top level, "
            f"got {type(data).__name__}"
        )
    return data


def _threshold_number(
    thresholds: "Mapping[str, Any]", key: str, default: float, *, integral: bool
) -> float:
    """Read one threshold, defaulting to today's constant when absent.

    A present value must be a number of the right kind (an ``int`` for the
    integral thresholds — ``api_error_ceiling`` / ``min_*_history`` — a real
    number otherwise). A YAML boolean (``true``/``false`` parses to a Python
    ``bool``, an ``int`` subclass) is rejected up front so a stray ``on`` is a
    loud error, not a silent ``1``.
    """
    if key not in thresholds:
        return default
    value = thresholds[key]
    kind = "integer" if integral else "number"
    if isinstance(value, bool):
        raise ValueError(
            f"gate-declaration `bindings.thresholds.{key}` must be a {kind}, "
            f"got a boolean ({value!r})"
        )
    if integral:
        if not isinstance(value, int):
            raise ValueError(
                f"gate-declaration `bindings.thresholds.{key}` must be an integer, "
                f"got {type(value).__name__} ({value!r})"
            )
        return value
    if not isinstance(value, (int, float)):
        raise ValueError(
            f"gate-declaration `bindings.thresholds.{key}` must be a number, "
            f"got {type(value).__name__} ({value!r})"
        )
    return float(value)


def _parse_corroboration(
    raw_corr: Any, cell_set: "frozenset[str]"
) -> "tuple[dict[str, tuple[str, float]], str | None, str | None, str | None]":
    """Parse the optional ``corroboration`` binding (US-103 AC4).

    Returns ``(claude_equivalent, judge_cell, generator_family, judge_family)``.
    When the block is **omitted** (``raw_corr is None``) corroboration is disabled
    — an empty map + all-``None`` — so every RAGAS drop degrades to
    single-judge-red. When PRESENT the block must be COMPLETE (all four keys):
    a half-specified block is a loud error, never a silent disable. The metric
    keys are validated against the RAGAS metric universe, each ``judge_metric``
    value against the Claude judge's metric universe (:data:`_JUDGE_METRIC_NAMES`),
    and ``judge_cell`` against the declared cells (an unknown metric key / judge
    metric / cell is a hard error, AC5) — a typo in any of them would otherwise
    silently switch corroboration off for that metric.
    """
    if raw_corr is None:
        return ({}, None, None, None)
    if not isinstance(raw_corr, Mapping):
        raise ValueError(
            "gate-declaration `bindings.corroboration` must be a mapping, got "
            f"{type(raw_corr).__name__}"
        )
    unknown = set(raw_corr) - _CORROBORATION_KEYS
    if unknown:
        raise ValueError(
            f"gate-declaration `bindings.corroboration` has unknown key(s) "
            f"{sorted(unknown)}; known keys are {sorted(_CORROBORATION_KEYS)}"
        )
    missing = _CORROBORATION_KEYS - set(raw_corr)
    if missing:
        raise ValueError(
            "a `bindings.corroboration` block must set every key "
            f"{sorted(_CORROBORATION_KEYS)} (missing {sorted(missing)}); omit the "
            "whole block to disable corroboration (single-judge-red)."
        )

    generator_family = str(raw_corr["generator_family"])
    judge_family = str(raw_corr["judge_family"])
    judge_cell = str(raw_corr["judge_cell"])
    if judge_cell not in cell_set:
        raise ValueError(
            f"gate-declaration `corroboration.judge_cell` {judge_cell!r} is not one "
            f"of the declared cells {sorted(cell_set)} (unknown cell — no silent skip)."
        )

    raw_je = raw_corr["judge_equivalent"]
    if not isinstance(raw_je, Mapping) or not raw_je:
        raise ValueError(
            "gate-declaration `corroboration.judge_equivalent` must be a non-empty "
            "mapping of RAGAS-metric -> {judge_metric, drop}"
        )
    claude_equivalent: "dict[str, tuple[str, float]]" = {}
    for metric, spec in raw_je.items():
        metric = str(metric)
        if metric not in RAGAS_METRICS:
            raise ValueError(
                f"gate-declaration `corroboration.judge_equivalent` names unknown "
                f"metric {metric!r}; keys must be RAGAS metrics {sorted(RAGAS_METRICS)}."
            )
        if not isinstance(spec, Mapping):
            raise ValueError(
                f"gate-declaration `corroboration.judge_equivalent.{metric}` must be "
                f"a mapping {{judge_metric, drop}}, got {type(spec).__name__}"
            )
        unknown_spec = set(spec) - _JUDGE_EQUIVALENT_KEYS
        if unknown_spec:
            raise ValueError(
                f"gate-declaration `corroboration.judge_equivalent.{metric}` has "
                f"unknown key(s) {sorted(unknown_spec)}; expected "
                f"{sorted(_JUDGE_EQUIVALENT_KEYS)}"
            )
        if "judge_metric" not in spec or "drop" not in spec:
            raise ValueError(
                f"gate-declaration `corroboration.judge_equivalent.{metric}` must set "
                "both `judge_metric` and `drop`"
            )
        drop = spec["drop"]
        if isinstance(drop, bool) or not isinstance(drop, (int, float)):
            raise ValueError(
                f"gate-declaration `corroboration.judge_equivalent.{metric}.drop` "
                f"must be a number, got {type(drop).__name__} ({drop!r})"
            )
        judge_metric = str(spec["judge_metric"])
        if judge_metric not in _JUDGE_METRIC_NAMES:
            raise ValueError(
                f"gate-declaration `corroboration.judge_equivalent.{metric}."
                f"judge_metric` names unknown judge metric {judge_metric!r}; the "
                f"Claude judge emits only {sorted(_JUDGE_METRIC_NAMES)} (a typo would "
                "silently never corroborate — no silent skip, AC5)."
            )
        claude_equivalent[metric] = (judge_metric, float(drop))

    return (claude_equivalent, judge_cell, generator_family, judge_family)


def _parse_bindings(raw: Any) -> GateBindings:
    """Parse a ``bindings:`` block into a :class:`GateBindings` (US-103).

    The buyer's cells / cross-family judge map / thresholds, validated into the
    exact config object the ``ragas_gates`` detection functions take. ``cells``
    and every ``thresholds`` field default to today's constant
    (:func:`default_bindings`) when omitted, so a partial ``bindings`` block
    inherits the default for whatever those two sections do not name.
    ``corroboration`` is the EXCEPTION and does NOT inherit: omitting the
    ``corroboration:`` sub-block does not fall back to the default map - it
    DISABLES cross-family corroboration (empty judge map + all-``None`` families)
    so every score regression degrades to single-judge-red (AC4). A custom
    ``bindings:`` block that edits only ``cells`` / ``thresholds`` therefore runs
    single-family; to keep cross-family corroboration the block must RE-DECLARE
    the ``corroboration:`` sub-block in full. Hard errors (no silent skip): an
    unknown section / threshold / corroboration key, a blank or empty cell list,
    an unknown corroboration metric or judge cell (AC5).

    Note on the cell universe: the cells are BUYER-defined (the whole point of the
    genericization), so they are not constrained to the kit's default cell set —
    the only cell check is that ``corroboration.judge_cell`` names one of THIS
    declaration's cells (a dangling judge cell would silently never corroborate).
    """
    if not isinstance(raw, Mapping):
        raise ValueError(
            "gate-declaration `bindings` must be a mapping of "
            f"{sorted(_BINDINGS_KEYS)}; got {type(raw).__name__}"
        )
    unknown = set(raw) - _BINDINGS_KEYS
    if unknown:
        raise ValueError(
            f"gate-declaration `bindings` has unknown section(s) {sorted(unknown)}; "
            f"known sections are {sorted(_BINDINGS_KEYS)}"
        )

    defaults = default_bindings()

    # -- cells (buyer-defined; default to today's cell list when omitted).
    raw_cells = raw.get("cells")
    if raw_cells is None:
        cell_ids = defaults.cell_ids
    else:
        if not isinstance(raw_cells, (list, tuple)) or not raw_cells:
            raise ValueError(
                "gate-declaration `bindings.cells` must be a non-empty list of "
                f"cell-id strings; got {raw_cells!r}"
            )
        cell_ids = tuple(str(c) for c in raw_cells)
        if any(not c.strip() for c in cell_ids):
            raise ValueError(
                "gate-declaration `bindings.cells` contains a blank cell id"
            )
    cell_set = frozenset(cell_ids)

    # -- thresholds (each defaults to today's constant when omitted).
    raw_thresholds = raw.get("thresholds")
    if raw_thresholds is None:
        raw_thresholds = {}
    if not isinstance(raw_thresholds, Mapping):
        raise ValueError(
            "gate-declaration `bindings.thresholds` must be a mapping, got "
            f"{type(raw_thresholds).__name__}"
        )
    unknown_t = set(raw_thresholds) - _THRESHOLD_KEYS
    if unknown_t:
        raise ValueError(
            f"gate-declaration `bindings.thresholds` has unknown key(s) "
            f"{sorted(unknown_t)}; known thresholds are {sorted(_THRESHOLD_KEYS)}"
        )
    coverage_floor = _threshold_number(
        raw_thresholds, "coverage_floor", defaults.coverage_floor, integral=False
    )
    api_error_ceiling = int(
        _threshold_number(
            raw_thresholds, "api_error_ceiling", defaults.api_error_ceiling,
            integral=True,
        )
    )
    coverage_drift_pp = _threshold_number(
        raw_thresholds, "coverage_drift_pp", defaults.coverage_drift_pp,
        integral=False,
    )
    ragas_drop = _threshold_number(
        raw_thresholds, "ragas_drop", defaults.ragas_drop, integral=False
    )
    min_drift_history = int(
        _threshold_number(
            raw_thresholds, "min_drift_history", defaults.min_drift_history,
            integral=True,
        )
    )
    min_regression_history = int(
        _threshold_number(
            raw_thresholds, "min_regression_history",
            defaults.min_regression_history, integral=True,
        )
    )

    # -- corroboration (optional; omitting it degrades to single-judge-red, AC4).
    (
        claude_equivalent,
        judge_cell,
        generator_family,
        judge_family,
    ) = _parse_corroboration(raw.get("corroboration"), cell_set)

    return GateBindings(
        cell_ids=cell_ids,
        coverage_floor=coverage_floor,
        api_error_ceiling=api_error_ceiling,
        coverage_drift_pp=coverage_drift_pp,
        ragas_drop=ragas_drop,
        min_drift_history=min_drift_history,
        min_regression_history=min_regression_history,
        claude_equivalent=claude_equivalent,
        claude_judge_cell=judge_cell,
        generator_family=generator_family,
        judge_family=judge_family,
    )


def _parse_suites(raw: Any) -> SuiteVerdicts:
    """Parse a ``suites:`` block into a :class:`SuiteVerdicts` (US-104).

    A mapping of ``suite -> off|comment|fail`` over the three quality suites
    (``retrieval_metrics`` / ``ragas`` / ``escalation``). Hard errors, no silent
    skip: an unknown suite key (a typo would leave that suite silently at its
    ``comment`` default), an unknown verdict value. A ``security`` output can never
    appear here — security invariants are pinned ``fail`` and are in no suite, so a
    security-named key is simply an "unknown suite" rejection (US-102 intact). The
    YAML-1.1 ``off`` gotcha (unquoted ``off`` parses to ``False``) is normalized
    back to the string ``"off"`` so ``ragas: off`` works unquoted.
    """
    if not isinstance(raw, Mapping):
        raise ValueError(
            "gate-declaration `suites` must be a mapping of "
            f"suite -> ({'|'.join(sorted(_LOUDNESS_VALUES))}); got {type(raw).__name__}"
        )
    knobs: "dict[str, str]" = {}
    for suite, knob in raw.items():
        suite = str(suite)
        if suite not in _SUITE_KEYS:
            raise ValueError(
                f"gate-declaration `suites` names unknown suite {suite!r}; the "
                f"quality suites are {sorted(_SUITE_KEYS)} (security invariants are "
                "pinned `fail` and are in no suite — no silent skip)."
            )
        # YAML 1.1 boolean gotcha (mirrors the `verdicts` section): an unquoted
        # `off` parses to the boolean False, never the string "off". `off` IS a
        # valid knob, so normalize False -> "off" up front.
        if knob is False:
            knob = "off"
        if not isinstance(knob, str) or knob not in _LOUDNESS_VALUES:
            hint = ""
            if isinstance(knob, bool):
                hint = (
                    " (an unquoted YAML `on`/`yes`/`true` parses to a boolean; "
                    "only `off|comment|fail` are knobs)"
                )
            raise ValueError(
                f"gate-declaration `suites.{suite}` has invalid loudness knob "
                f"{knob!r}; expected one of {sorted(_LOUDNESS_VALUES)}{hint}"
            )
        knobs[suite] = knob
    return SuiteVerdicts(knobs=knobs)


def _parse_per_pr(raw: Any) -> "frozenset[str]":
    """Parse a ``per_pr:`` block into the validated per-PR merge-blocking set (US-105).

    A mapping of ``suite_or_output -> fail`` naming the *deterministic* quality
    gates the buyer wants to block a merge per-PR. Every entry is validated through
    :func:`~evals.gate.placement.validate_per_pr_target`, which enforces the
    US-105 rule structurally (hard errors, no silent skip):

    * a **non-deterministic** target (a RAGAS/escalation suite or an LLM-judged
      output like ``faithfulness``) → :class:`~evals.gate.placement.PlacementError`
      (AC5: a judge wobble must never block an innocent merge);
    * a **security** output → :class:`SecurityGateError` (pinned ``fail``; it blocks
      per-PR structurally and carries no tunable knob - the placement counterpart to
      the ``verdicts:`` / ``suites:`` security pin);
    * an **unknown** target (neither a known suite nor a registered output) →
      ``ValueError``.

    The only accepted value is ``fail`` - ``per_pr:`` is exclusively the
    merge-blocking verdict; a ``comment`` / ``off`` here is a category error
    (that is loudness - set it under ``suites:`` / ``verdicts:``). A YAML-1.1
    unquoted ``off`` (→ ``False``) is caught and pointed at the loudness sections
    rather than silently normalized, since ``off`` is never a valid placement.
    """
    if not isinstance(raw, Mapping):
        raise ValueError(
            "gate-declaration `per_pr` must be a mapping of "
            f"suite-or-output -> `{_PER_PR_VALUE}`; got {type(raw).__name__}"
        )
    targets: "set[str]" = set()
    for name, value in raw.items():
        name = str(name)
        # Enforce the placement rule FIRST so the actionable structural message
        # (PlacementError / SecurityGateError) is what a buyer sees, rather than a
        # generic value complaint on a mistyped verdict. An unknown target raises
        # KeyError inside; re-cast it to the loader's ValueError convention with a
        # message naming the known targets (mirrors the `verdicts:` unknown-output
        # handling - no silent skip).
        try:
            validate_per_pr_target(name)  # PlacementError / SecurityGateError / KeyError
        except KeyError:
            raise ValueError(
                f"gate declaration `per_pr` names unknown target {name!r}; a "
                "`per_pr:` key must be a quality suite "
                f"{sorted(QUALITY_SUITES)} or a registered quality output "
                "(security invariants block per-PR structurally and carry no knob; "
                "see US-101/US-102/US-105)."
            ) from None
        if value != _PER_PR_VALUE:
            hint = (
                " - `per_pr:` is only the merge-blocking verdict; set loudness "
                "(`comment`/`off`) under `suites:` or `verdicts:` instead"
            )
            raise ValueError(
                f"gate-declaration `per_pr.{name}` must be `{_PER_PR_VALUE}` "
                f"(the per-PR merge-blocking verdict), got {value!r}{hint}"
            )
        targets.add(name)
    return frozenset(targets)


def load_gate_declaration(
    source: "Union[str, Path, Mapping[str, Any]]",
) -> GateDeclaration:
    """Load and validate a gate declaration; return a :class:`GateDeclaration`.

    Enforces the US-102 invariants (each a hard error, non-zero exit for a CLI
    caller — no silent skip / silent downgrade):

    * an unknown top-level section is rejected (a typo is loud);
    * ``verdicts`` must be a mapping of ``output -> verdict``;
    * every verdict key must be a **registered** eval output (unknown → error);
    * a **security**-class key is rejected with the pinned-``fail`` message —
      the load-time enforcement of "silence only by deletion" (AC1/AC5);
    * every verdict value must be one of ``off | comment | fail`` — an unquoted
      YAML ``off`` (which parses to the boolean ``False``) is normalized back to
      ``"off"`` so a buyer's natural ``recall_at_5: off`` works unquoted;
    * the optional ``bindings:`` section (US-103) is parsed into a
      :class:`~evals.retrieval.ragas_gates.GateBindings`; an unknown cell / metric
      / sub-key is a hard error. When absent, the bindings default to today's
      constants, so a verdicts-only declaration is unchanged.
    * the optional ``suites:`` section (US-104) is parsed into a
      :class:`~evals.gate.verdict.SuiteVerdicts` — one ``off|comment|fail`` knob
      per quality suite (``retrieval_metrics`` / ``ragas`` / ``escalation``); an
      unknown suite / knob is a hard error. When absent every suite defaults to
      ``comment`` (the shipped PR posture).
    * the optional ``per_pr:`` section (US-105) is the placement axis - a mapping
      of ``suite-or-output -> fail`` naming the *deterministic* quality gates that
      block a merge per-PR. A **non-deterministic** target is a structural load
      error (:class:`~evals.gate.placement.PlacementError`): a judge-driven gate is
      never offered a per-PR ``fail`` (AC5). A security output is rejected (pinned
      ``fail``, blocks per-PR structurally), and an unknown target / non-``fail``
      value is a hard error. When absent, no quality gate blocks the merge via
      config.
    """
    data = _coerce_source(source)

    unknown_sections = set(data) - _KNOWN_SECTIONS
    if unknown_sections:
        raise ValueError(
            f"unknown gate-declaration section(s) {sorted(unknown_sections)}; "
            f"the loader knows {sorted(_KNOWN_SECTIONS)} (later stories add more). "
            "A stray/misspelled section is a hard error, never a silent skip."
        )

    raw_verdicts = data.get("verdicts")
    if raw_verdicts is None:
        raw_verdicts = {}
    if not isinstance(raw_verdicts, Mapping):
        raise ValueError(
            "gate-declaration `verdicts` must be a mapping of "
            f"output -> ({'|'.join(sorted(_LOUDNESS_VALUES))}); got "
            f"{type(raw_verdicts).__name__}"
        )

    verdicts: dict[str, str] = {}
    for name, verdict in raw_verdicts.items():
        name = str(name)

        # (0) YAML 1.1 boolean gotcha: an UNQUOTED `off` verdict parses to the
        #     boolean False (as `on`/`yes`/`true` parse to True), never the
        #     string "off". `off` IS a valid verdict, so normalize False -> "off"
        #     up front — a buyer's natural `recall_at_5: off` then works whether
        #     or not they quote it, and the security-rejection message below reads
        #     `'off'` rather than `False`. A truthy YAML boolean (`on`/`yes`/
        #     `true`) is not a verdict and still falls through to the invalid-value
        #     error at (3).
        if verdict is False:
            verdict = "off"

        # (1) The load-time security pin — the heart of US-102. A security output
        #     has no tunable verdict; setting one is a build error, not a downgrade.
        if is_registered(name) and gate_class(name).is_security:
            raise SecurityGateError(
                f"gate declaration tries to set {name!r} to {verdict!r}: "
                f"{_PINNED_MESSAGE} (US-102). Security invariants "
                "(E4/E6/AU4/E7-P1b) are pinned `fail` and are not present in the "
                "tunable verdict map."
            )

        # (2) Unknown output — hard error, no silent skip (mirrors US-101's
        #     `gate_class` and US-103's unknown-cell rule).
        if not is_registered(name):
            raise ValueError(
                f"gate declaration sets a verdict on unknown eval output {name!r}. "
                "Every verdict key must be a registered quality output "
                "(security invariants carry no knob; see US-101/US-102)."
            )

        # (3) Invalid verdict value. A leftover YAML boolean here is a truthy
        #     `on`/`yes`/`true` (False was normalized to "off" above), so hint at
        #     the gotcha rather than emit a bare `True`.
        if not isinstance(verdict, str) or verdict not in _LOUDNESS_VALUES:
            hint = ""
            if isinstance(verdict, bool):
                hint = (
                    " (an unquoted YAML `on`/`yes`/`true` parses to a boolean; "
                    "only `off|comment|fail` are verdicts)"
                )
            raise ValueError(
                f"gate declaration sets {name!r} to invalid verdict {verdict!r}; "
                f"expected one of {sorted(_LOUDNESS_VALUES)}{hint}"
            )

        verdicts[name] = verdict

    raw_bindings = data.get("bindings")
    bindings = default_bindings() if raw_bindings is None else _parse_bindings(raw_bindings)

    raw_suites = data.get("suites")
    suites = SuiteVerdicts() if raw_suites is None else _parse_suites(raw_suites)

    raw_per_pr = data.get("per_pr")
    per_pr = frozenset() if raw_per_pr is None else _parse_per_pr(raw_per_pr)

    return GateDeclaration(
        verdicts=verdicts, bindings=bindings, suites=suites, per_pr=per_pr
    )
