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
* Still to come: US-104 adds the ``(severity, knob) -> action`` verdict function
  that consumes ``verdicts``; US-105 adds the per-PR-vs-scheduled determinism
  check. Each extends ``_KNOWN_SECTIONS`` / this dataclass; none may relax the
  security pin.
* A verdict key is a registered **eval-output name** (matching US-102's own
  validation test, which authors ``E4_zero_leak: off``). US-104 layers the
  per-suite grouping on top of this per-output map; a security-output key stays
  rejected regardless of granularity, because a security output is in no suite.

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
from evals.retrieval.ragas import RAGAS_METRICS
from evals.retrieval.ragas_gates import GateBindings, default_bindings

# The top-level sections the loader knows. US-102 shipped `verdicts`; US-103 adds
# `bindings` (the detection layer's cells / judge map / thresholds). Kept a frozen
# set so an unknown / misspelled top-level key is a loud error, never a silent skip.
_KNOWN_SECTIONS = frozenset({"verdicts", "bindings"})

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
    """

    verdicts: Mapping[str, str]
    bindings: GateBindings = field(default_factory=default_bindings)

    def verdict_for(self, output: str) -> str:
        """The resolved loudness verdict for ``output``.

        Returns the buyer's explicit setting when present, else the registry
        default (``comment``). Raises :class:`SecurityGateError` for a
        ``security``-class output — there is no knob to read, it is pinned
        ``fail`` (mirrors ``GateClass.loudness``). Raises ``KeyError`` for an
        unregistered output.
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

    return GateDeclaration(verdicts=verdicts, bindings=bindings)
