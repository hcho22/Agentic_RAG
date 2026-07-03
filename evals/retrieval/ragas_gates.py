"""US-005 / US-006 / US-007: gates over the RAGAS aggregates.

Three gate families run over the RAGAS results the runner produces, and all
three feed `ragas.gate_findings` in the results JSON:

  * **Operational gates** (US-005) — fixed-threshold checks for pipeline / API
    degradation. A violation is `red`: it fails the weekly workflow (runner
    exits non-zero) and files a GitHub issue.
  * **Diagnostic gates** (US-006) — rolling-window drift checks. A violation is
    `yellow`: it never fails the workflow, it just surfaces in a `Diagnostics`
    section of `summary.md` so slow rot is visible before it would otherwise
    go unnoticed.
  * **Score-regression gates** (US-007) — rolling-median checks on the RAGAS
    scores, with cross-family corroboration. A RAGAS drop the independent
    Claude judge corroborates in the same cell is `red`; an uncorroborated
    single-judge drop is `yellow`. Context Precision / Recall have no Claude
    equivalent, so a drop there fires `single-judge-red` (red, but tagged so a
    reader knows it rests on one judge).

Fixed vs rolling — on purpose
-----------------------------
Operational gates use **fixed** thresholds; diagnostic and score-regression
gates use a **rolling** multi-week window. The split is deliberate (FR-8 /
FR-9 / FR-10): a fixed floor means a degraded pipeline can never quietly
redefine "normal", while a rolling window is the right tool both for spotting
gradual drift and for letting a genuine score *improvement* rebaseline so it is
not later mistaken for a regression.

Per-cell vs per-(metric × cell)
-------------------------------
`coverage` is genuinely per-(metric × cell) — different metrics score different
fractions of the question set — so coverage checks run per (metric × cell).
`api_errors` is a cell-level total (`ragas._aggregate_by_cell` stores the same
cell-wide count in every metric block), so API-error checks run **once per
cell**: one finding, not one per metric.

Bindings vs algorithm (US-103)
------------------------------
The three detection functions below are project-*agnostic* algorithms (fixed
floors, rolling-median windows, cross-family corroboration, single-judge-red,
severity, `auto_close_weeks`). Their project-*specific* bindings — which cells to
gate (`RAGAS_CELL_IDS`), which metric the cross-family judge corroborates
(`CLAUDE_EQUIVALENT` / `CLAUDE_JUDGE_CELL`), and the threshold constants — are
lifted out of module scope into a :class:`GateBindings` config object that every
detection function takes as a parameter. The module constants remain the SINGLE
source of the default values (`default_bindings()` reads them), and the shipped
`evals/gate/gate.yaml` reproduces them, so a buyer describes *their* cells / judge
map / thresholds in that declaration without forking this detection logic — and
the default declaration is byte-identical to the legacy hardcoded path
(`evals/gate/declaration.py` parses the YAML; the regression guard lives in
`evals/gate/test_gate_bindings.py`).
"""

from __future__ import annotations

import json
import logging
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from .ragas import RAGAS_CELL_IDS, RAGAS_METRICS, RAGAS_MODE

log = logging.getLogger("agentic_rag.evals.retrieval.ragas_gates")

ROOT = Path(__file__).resolve().parents[2]

# US-008 publishes one weekly RAGAS snapshot per file here, named
# `<YYYY-MM-DD>.json`. The directory does not exist until the first weekly run;
# `load_ragas_history` treats an absent directory as "no history yet".
RAGAS_WEEKLY_DIR = ROOT / "docs" / "ragas-weekly"

# The nightly retrieval eval publishes here. When the nightly run includes the
# Claude judge (`--include-generation`), these snapshots are the cross-family
# custom-judge history US-007 corroborates against.
NIGHTLY_DIR = ROOT / "docs" / "nightly"

# FR-8: fixed operational thresholds. Deliberately not rolling — see the module
# docstring. A cell must carry a non-NaN score for at least 96% of its
# questions; a cell with more than 2 API errors is an operational failure.
COVERAGE_FLOOR = 0.96
API_ERROR_CEILING = 2

# FR-9: diagnostic drift parameters. `COVERAGE_DRIFT_PP` is how far below the
# rolling-median coverage a cell must fall to count as drift (5 percentage
# points on the 0–1 coverage scale). `MIN_DRIFT_HISTORY` is the smallest number
# of prior weekly snapshots the rolling window needs before drift is evaluated
# at all — below it the check is skipped rather than firing on noise. (The
# score-regression gates need a fuller 4-snapshot window; the diagnostic gate
# is lower-stakes, so it activates one week sooner.)
COVERAGE_DRIFT_PP = 0.05
MIN_DRIFT_HISTORY = 3

# FR-10 / FR-12 / FR-13: score-regression parameters. A regression is a strict
# drop below the rolling median. `RAGAS_DROP` is the trigger on the 0–1 RAGAS
# scale; `CLAUDE_FAITHFULNESS_DROP` / `CLAUDE_HELPFULNESS_DROP` are the
# cross-family Claude thresholds on the 1–5 Likert scale (helpfulness looser —
# it corroborates Answer Relevancy "softly"). `MIN_REGRESSION_HISTORY` is the
# full rolling window the score gate needs before it evaluates at all.
RAGAS_DROP = 0.05
CLAUDE_FAITHFULNESS_DROP = 0.3
CLAUDE_HELPFULNESS_DROP = 0.2
MIN_REGRESSION_HISTORY = 4

# The Claude judge (US-036) scores only the full_access × pre_filter cell, so
# that is the one cell where a RAGAS drop can be cross-family corroborated.
CLAUDE_JUDGE_CELL = "full_access:pre_filter"

# RAGAS metric → (Claude judge metric, Claude drop threshold). The two metrics
# absent here — context_precision, context_recall — have no Claude equivalent
# and fire single-judge-red on a RAGAS drop.
CLAUDE_EQUIVALENT: dict[str, tuple[str, float]] = {
    "faithfulness": ("faithfulness", CLAUDE_FAITHFULNESS_DROP),
    "answer_relevancy": ("helpfulness", CLAUDE_HELPFULNESS_DROP),
}

# Finding tags. Operational (red): `coverage-pipeline-failure` — RAGAS produced
# too few scores; `coverage-operational-failure` — the judge API was erroring.
# Diagnostic (yellow): `coverage-drift` / `api-error-drift` — a rolling-window
# slide. Score-regression: `score-regression` (faithfulness / answer_relevancy,
# red or yellow) and `single-judge-red` (context_precision / context_recall, no
# Claude equivalent). The weekly workflow opens / dedups one issue per tag.
TAG_COVERAGE_PIPELINE = "coverage-pipeline-failure"
TAG_COVERAGE_OPERATIONAL = "coverage-operational-failure"
TAG_COVERAGE_DRIFT = "coverage-drift"
TAG_API_ERROR_DRIFT = "api-error-drift"
TAG_SCORE_REGRESSION = "score-regression"
TAG_SINGLE_JUDGE_RED = "single-judge-red"

# US-103 corroboration families. The RAGAS scores are judged by gpt-4o-mini (the
# OpenAI family — the SAME family as the answer generator, see the same-family
# note in `ragas.py`), and the cross-family corroboration is the independent
# custom Claude judge (Anthropic). The two families DIFFER, so corroboration is
# enabled by default and reproduces today's behavior. When a buyer's judge family
# equals their generator family — or they omit the corroboration binding entirely
# — corroboration degrades to `single-judge-red`: a same-family judge is a weaker,
# non-independent signal, so a RAGAS drop stands on one judge (US-109).
DEFAULT_GENERATOR_FAMILY = "openai"
DEFAULT_JUDGE_FAMILY = "anthropic"


@dataclass(frozen=True)
class GateBindings:
    """The project-specific bindings the detection layer reads (US-103).

    Everything a buyer would change to point the *unchanged* detection algorithms
    at *their* eval shape:

    ``cell_ids``            — the cells to gate (today ``RAGAS_CELL_IDS``).
    ``coverage_floor``      — fixed operational coverage floor (``COVERAGE_FLOOR``).
    ``api_error_ceiling``   — fixed per-cell API-error ceiling (``API_ERROR_CEILING``).
    ``coverage_drift_pp``   — diagnostic rolling-window drift band (``COVERAGE_DRIFT_PP``).
    ``ragas_drop``          — score-regression trigger on the 0–1 RAGAS scale (``RAGAS_DROP``).
    ``min_drift_history``   — snapshots the diagnostic gate needs (``MIN_DRIFT_HISTORY``).
    ``min_regression_history`` — snapshots the score gate needs (``MIN_REGRESSION_HISTORY``).
    ``claude_equivalent``   — RAGAS metric → (cross-family judge metric, drop
                              threshold) map (``CLAUDE_EQUIVALENT``).
    ``claude_judge_cell``   — the one cell the cross-family judge covers (``CLAUDE_JUDGE_CELL``).
    ``generator_family`` / ``judge_family`` — the corroboration families; when
                              equal (or either omitted) corroboration is disabled
                              and every RAGAS drop falls through to single-judge-red.

    Frozen: a bindings object is a fixed declaration for one run. It is compared by
    value (``default_bindings() == <parsed gate.yaml>`` is the US-103 regression
    guard), never hashed (``claude_equivalent`` is a live mapping).
    """

    cell_ids: tuple[str, ...]
    coverage_floor: float
    api_error_ceiling: int
    coverage_drift_pp: float
    ragas_drop: float
    min_drift_history: int
    min_regression_history: int
    claude_equivalent: Mapping[str, tuple[str, float]]
    claude_judge_cell: Optional[str] = None
    generator_family: Optional[str] = None
    judge_family: Optional[str] = None

    @property
    def corroboration_enabled(self) -> bool:
        """True when cross-family corroboration is actually in force.

        Requires a non-empty judge map, a judge cell, and two DIFFERENT families
        (an independent second judge). Omitting the corroboration binding, or
        setting ``judge_family == generator_family``, disables it — a same-family
        or absent judge is not the cross-family corroboration the red escalation
        rests on (US-103 AC4).
        """
        return (
            bool(self.claude_equivalent)
            and self.claude_judge_cell is not None
            and self.generator_family is not None
            and self.judge_family is not None
            and self.generator_family != self.judge_family
        )

    def effective_claude_equivalent(self) -> "dict[str, tuple[str, float]]":
        """The corroboration map ACTUALLY in force this run.

        When corroboration is disabled (same-family judge, or the binding omitted)
        this is empty, so ``check_score_regressions`` finds no Claude equivalent
        for ANY metric and every RAGAS drop falls through to the existing
        ``single-judge-red`` path — the AC4 degrade with *no new code* (it is the
        exact path ``context_precision`` / ``context_recall`` already take).
        """
        if not self.corroboration_enabled:
            return {}
        return dict(self.claude_equivalent)


def default_bindings() -> GateBindings:
    """The kit's default bindings — today's ``ragas_gates`` constants verbatim.

    Built directly from the module constants above, so it is the executable
    definition of "today's hardcoded gate". The shipped ``evals/gate/gate.yaml``
    reproduces these values, making the declaration-driven path byte-identical to
    the legacy constant-driven path (US-103 AC3 regression guard).
    """
    return GateBindings(
        cell_ids=tuple(RAGAS_CELL_IDS),
        coverage_floor=COVERAGE_FLOOR,
        api_error_ceiling=API_ERROR_CEILING,
        coverage_drift_pp=COVERAGE_DRIFT_PP,
        ragas_drop=RAGAS_DROP,
        min_drift_history=MIN_DRIFT_HISTORY,
        min_regression_history=MIN_REGRESSION_HISTORY,
        claude_equivalent=dict(CLAUDE_EQUIVALENT),
        claude_judge_cell=CLAUDE_JUDGE_CELL,
        generator_family=DEFAULT_GENERATOR_FAMILY,
        judge_family=DEFAULT_JUDGE_FAMILY,
    )


@dataclass
class GateFinding:
    """One gate violation.

    ``severity`` is ``red`` (fails the workflow) or ``yellow`` (diagnostic
    only). ``tag`` groups findings so the workflow can open / dedup one GitHub
    issue per tag. ``cell`` locates the violation; ``metric`` names the RAGAS
    metric for per-metric findings and is ``""`` for cell-level findings (the
    API-error checks, since ``api_errors`` is a cell-level total).
    ``cross_family_corroborated`` is True only for a red score regression the
    Claude judge confirmed; ``auto_close_weeks`` is how long the workflow keeps
    an issue open with no recurrence (2 for ``single-judge-red``, 1 otherwise).
    ``message`` is the human-readable one-liner.
    """

    severity: str
    tag: str
    metric: str
    cell: str
    message: str
    cross_family_corroborated: bool = False
    auto_close_weeks: int = 1


def _cell_api_errors(cell: dict[str, Any]) -> int | None:
    """The cell-level API-error total.

    ``ragas._aggregate_by_cell`` stores the same cell-wide total in every
    metric block, so the value is read from whichever block is present.
    Returns ``None`` for a cell with no metric blocks.
    """
    for block in cell.values():
        err = block.get("api_errors")
        if err is not None:
            return err
    return None


def _collect_metric_history(
    history: list[dict[str, Any]], cell_id: str, metric: str, key: str
) -> list[float]:
    """Collect a per-(cell × metric) numeric field across prior snapshots.

    Each ``history`` entry is a prior run's ``ragas.aggregates``. Snapshots
    missing the cell, the metric, or the value are skipped — the result holds
    only the values actually present, the rolling window the gates median over.
    """
    values: list[float] = []
    for h in history:
        block = h.get("by_cell", {}).get(cell_id, {}).get(metric)
        if block is None:
            continue
        value = block.get(key)
        if value is not None:
            values.append(value)
    return values


def check_operational_gates(
    ragas_aggregates: dict[str, Any],
    bindings: GateBindings | None = None,
) -> list[GateFinding]:
    """Return red operational findings over the RAGAS by-cell aggregates.

    ``ragas_aggregates`` is the ``aggregates`` dict of the ``ragas`` results
    section (US-003) — ``{"by_cell": {cell_id: {metric: {coverage,
    api_errors, ...}}}}``. ``bindings`` supplies the cells and thresholds
    (US-103); when omitted it defaults to :func:`default_bindings` (today's
    constants), so an unbound call is byte-identical to the legacy behavior. Two
    fixed-threshold checks run:

      * ``coverage < coverage_floor`` per (metric × cell) → red ``coverage-pipeline-failure``
      * ``api_errors > api_error_ceiling`` per cell → red ``coverage-operational-failure``

    See the module docstring for why coverage is per-metric but ``api_errors``
    is checked once per cell.
    """
    b = bindings if bindings is not None else default_bindings()
    findings: list[GateFinding] = []
    by_cell = ragas_aggregates.get("by_cell", {})
    for cell_id in b.cell_ids:
        cell = by_cell.get(cell_id)
        if not cell:
            continue
        for metric in RAGAS_METRICS:
            block = cell.get(metric)
            if block is None:
                continue
            coverage = block.get("coverage")
            if coverage is not None and coverage < b.coverage_floor:
                findings.append(
                    GateFinding(
                        severity="red",
                        tag=TAG_COVERAGE_PIPELINE,
                        metric=metric,
                        cell=cell_id,
                        message=(
                            f"coverage {coverage:.4f} for {metric} × {cell_id} "
                            f"is below the fixed floor of {b.coverage_floor}"
                        ),
                    )
                )
        api_errors = _cell_api_errors(cell)
        if api_errors is not None and api_errors > b.api_error_ceiling:
            findings.append(
                GateFinding(
                    severity="red",
                    tag=TAG_COVERAGE_OPERATIONAL,
                    metric="",
                    cell=cell_id,
                    message=(
                        f"{api_errors} API errors for {cell_id} exceeds the "
                        f"fixed ceiling of {b.api_error_ceiling}"
                    ),
                )
            )
    return findings


def _load_snapshots(directory: Path, weeks: int) -> list[dict[str, Any]]:
    """Read the most recent ``weeks`` ``*.json`` snapshots from ``directory``.

    Oldest first. Filenames are ISO dates, so a lexical sort is chronological.
    An absent directory yields ``[]`` rather than an error.
    """
    if not directory.is_dir():
        return []
    snapshots: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json"))[-weeks:]:
        with path.open(encoding="utf-8") as f:
            snapshots.append(json.load(f))
    return snapshots


def load_ragas_history(
    weeks: int = 4, weekly_dir: Path | None = None
) -> list[dict[str, Any]]:
    """Return the most recent ``weeks`` weekly RAGAS snapshots, oldest first.

    Snapshots are the ``docs/ragas-weekly/<YYYY-MM-DD>.json`` files US-008's
    weekly workflow commits — each a full results JSON. An absent directory
    (pre-US-008, or the first run after rollout) yields ``[]``. ``weekly_dir``
    overrides the location, for tests.
    """
    directory = weekly_dir if weekly_dir is not None else RAGAS_WEEKLY_DIR
    return _load_snapshots(directory, weeks)


def load_custom_judge_history(
    weeks: int = 4, nightly_dir: Path | None = None
) -> list[dict[str, Any]]:
    """Return the most recent ``weeks`` nightly snapshots, oldest first.

    Snapshots are the ``docs/nightly/<YYYY-MM-DD>.json`` files the nightly
    retrieval eval commits — the source of cross-family Claude-judge history
    for US-007's corroboration. A nightly run only carries Claude scores when
    it was invoked with ``--include-generation``; when it was not, the
    ``faithfulness`` / ``helpfulness`` keys are simply absent and the
    corroboration step degrades gracefully (a RAGAS drop stays yellow).
    """
    directory = nightly_dir if nightly_dir is not None else NIGHTLY_DIR
    return _load_snapshots(directory, weeks)


def check_diagnostic_gates(
    current_aggregates: dict[str, Any],
    history: list[dict[str, Any]],
    bindings: GateBindings | None = None,
) -> list[GateFinding]:
    """Return yellow drift findings over the RAGAS by-cell aggregates.

    ``current_aggregates`` is this run's ``ragas.aggregates``; ``history`` is
    the list of prior runs' ``ragas.aggregates`` (oldest first), extracted from
    the snapshots ``load_ragas_history`` returns. ``bindings`` supplies the cells,
    drift band, and minimum history (US-103; defaults to today's constants when
    omitted). Two rolling-window checks run:

      * ``coverage`` below the rolling **median** by more than
        ``coverage_drift_pp``, per (metric × cell) → yellow ``coverage-drift``
      * ``api_errors`` above the rolling **mean** and non-zero, per cell →
        yellow ``api-error-drift``

    When fewer than ``min_drift_history`` prior snapshots are available the
    check is skipped with a single log line and returns ``[]`` — early-rollout
    runs do not have enough history to tell drift from noise. Yellow findings
    never fail the workflow.
    """
    b = bindings if bindings is not None else default_bindings()
    if len(history) < b.min_drift_history:
        log.info(
            "drift check skipped: insufficient history (%d runs)", len(history)
        )
        return []

    findings: list[GateFinding] = []
    by_cell = current_aggregates.get("by_cell", {})
    for cell_id in b.cell_ids:
        cell = by_cell.get(cell_id)
        if not cell:
            continue

        # Coverage drift — per (metric × cell); coverage is genuinely per-metric.
        for metric in RAGAS_METRICS:
            block = cell.get(metric)
            if block is None:
                continue
            current_cov = block.get("coverage")
            if current_cov is None:
                continue
            past_cov = _collect_metric_history(
                history, cell_id, metric, "coverage"
            )
            if not past_cov:
                continue
            median_cov = statistics.median(past_cov)
            if current_cov < median_cov - b.coverage_drift_pp:
                findings.append(
                    GateFinding(
                        severity="yellow",
                        tag=TAG_COVERAGE_DRIFT,
                        metric=metric,
                        cell=cell_id,
                        message=(
                            f"coverage {current_cov:.4f} for {metric} × {cell_id} "
                            f"is below the {len(past_cov)}-run rolling median "
                            f"{median_cov:.4f} by more than {b.coverage_drift_pp}"
                        ),
                    )
                )

        # API-error drift — per cell; api_errors is a cell-level total.
        current_err = _cell_api_errors(cell)
        if current_err is None:
            continue
        past_err: list[int] = []
        for h in history:
            h_cell = h.get("by_cell", {}).get(cell_id)
            if h_cell:
                err = _cell_api_errors(h_cell)
                if err is not None:
                    past_err.append(err)
        if not past_err:
            continue
        mean_err = statistics.mean(past_err)
        if current_err > 0 and current_err > mean_err:
            findings.append(
                GateFinding(
                    severity="yellow",
                    tag=TAG_API_ERROR_DRIFT,
                    metric="",
                    cell=cell_id,
                    message=(
                        f"{current_err} API errors for {cell_id} exceeds the "
                        f"{len(past_err)}-run rolling mean {mean_err:.2f}"
                    ),
                )
            )
    return findings


def _claude_metric_dropped(
    cell_id: str,
    claude_key: str,
    threshold: float,
    current_main: dict[str, Any],
    custom_judge_history: list[dict[str, Any]],
    judge_cell: str | None,
    min_history: int,
) -> bool:
    """True when the cross-family Claude judge's ``claude_key`` score for this
    cell has dropped below its rolling median by more than ``threshold``.

    ``judge_cell`` (the cell the cross-family judge covers) and ``min_history``
    (the rolling window the score gate needs) come from the caller's
    :class:`GateBindings` (US-103). Returns False (no corroboration) when the
    cell is not the judged one, when the current Claude score is absent, or when
    there is not enough Claude history to form a median — a RAGAS drop then
    stands as a single-judge (yellow) finding rather than escalating to red.
    """
    if cell_id != judge_cell:
        return False
    current_claude = (
        current_main.get("by_mode", {}).get(RAGAS_MODE, {}).get(claude_key)
    )
    if current_claude is None:
        return False
    past: list[float] = []
    for snapshot in custom_judge_history:
        value = snapshot.get("by_mode", {}).get(RAGAS_MODE, {}).get(claude_key)
        if value is not None:
            past.append(value)
    if len(past) < min_history:
        return False
    return current_claude < statistics.median(past) - threshold


def check_score_regressions(
    current: dict[str, Any],
    history: list[dict[str, Any]],
    custom_judge_history: list[dict[str, Any]],
    bindings: GateBindings | None = None,
) -> list[GateFinding]:
    """Return score-regression findings with cross-family corroboration.

    ``current`` is a results-shaped dict ``{"ragas": <ragas section>,
    "aggregates": <main aggregates>}``; ``history`` is the prior runs'
    ``ragas.aggregates`` (oldest first, from ``load_ragas_history``);
    ``custom_judge_history`` is the prior runs' main ``aggregates`` carrying
    the Claude judge scores (from ``load_custom_judge_history``). ``bindings``
    supplies the cells, thresholds, and corroboration map (US-103; defaults to
    today's constants when omitted).

    For each (RAGAS metric × cell), a regression is the current ``mean_strict``
    falling below the rolling median by more than ``ragas_drop``. The severity
    follows the cross-family corroboration matrix (FR-10 / FR-11):

      * ``faithfulness`` / ``answer_relevancy`` — corroborated by the Claude
        ``faithfulness`` / ``helpfulness`` judge. Both drop → red; only one
        judge drops → yellow. Tag ``score-regression``.
      * ``context_precision`` / ``context_recall`` — no Claude equivalent. A
        RAGAS drop fires ``single-judge-red`` directly (red, ``auto_close_weeks
        = 2`` since there is no second judge to clear it sooner).

    Corroboration is OPTIONAL (US-103 AC4): when the bindings disable it (a
    same-family judge, or the binding omitted) ``effective_claude_equivalent()``
    is empty, so EVERY metric — including ``faithfulness`` / ``answer_relevancy``
    — falls through to the ``single-judge-red`` branch below, degrading a
    would-be cross-family red to a tagged single-judge red with no special-casing.

    Coverage-guard (FR-12): a (metric × cell) whose current ``coverage`` is
    below ``coverage_floor`` is skipped with a log line — a degraded-sample
    mean is not comparable to a full-sample rolling median. When fewer than
    ``min_regression_history`` prior snapshots exist (FR-13) the whole check is
    skipped with a log line and returns ``[]``.
    """
    b = bindings if bindings is not None else default_bindings()
    if len(history) < b.min_regression_history:
        log.info(
            "score-regression check skipped: insufficient history (%d runs)",
            len(history),
        )
        return []

    # The corroboration map ACTUALLY in force: empty when corroboration is
    # disabled, which routes every RAGAS drop to single-judge-red (AC4).
    claude_equivalent = b.effective_claude_equivalent()

    findings: list[GateFinding] = []
    current_by_cell = (
        current.get("ragas", {}).get("aggregates", {}).get("by_cell", {})
    )
    current_main = current.get("aggregates", {})

    for cell_id in b.cell_ids:
        cell = current_by_cell.get(cell_id)
        if not cell:
            continue
        for metric in RAGAS_METRICS:
            block = cell.get(metric)
            if block is None:
                continue

            # Coverage-guard: a degraded-sample mean is not comparable to a
            # full-sample rolling median, so skip rather than (mis)flag.
            coverage = block.get("coverage")
            if coverage is not None and coverage < b.coverage_floor:
                log.info(
                    "score-regression check skipped for (%s × %s): "
                    "insufficient coverage (%.2f < %s)",
                    metric,
                    cell_id,
                    coverage,
                    b.coverage_floor,
                )
                continue

            current_strict = block.get("mean_strict")
            if current_strict is None:
                continue
            past_strict = _collect_metric_history(
                history, cell_id, metric, "mean_strict"
            )
            if len(past_strict) < b.min_regression_history:
                continue
            ragas_median = statistics.median(past_strict)
            ragas_dropped = current_strict < ragas_median - b.ragas_drop

            claude = claude_equivalent.get(metric)
            if claude is None:
                # context_precision / context_recall — no Claude judge exists
                # to corroborate, so a RAGAS drop fires single-judge-red.
                if ragas_dropped:
                    findings.append(
                        GateFinding(
                            severity="red",
                            tag=TAG_SINGLE_JUDGE_RED,
                            metric=metric,
                            cell=cell_id,
                            message=(
                                f"RAGAS {metric} for {cell_id} fell to "
                                f"{current_strict:.4f}, below the "
                                f"{len(past_strict)}-run rolling median "
                                f"{ragas_median:.4f} by more than {b.ragas_drop}; "
                                "no cross-family Claude equivalent exists to "
                                "corroborate, so this fires single-judge-red"
                            ),
                            cross_family_corroborated=False,
                            auto_close_weeks=2,
                        )
                    )
                continue

            # faithfulness / answer_relevancy — cross-family corroboration.
            claude_key, claude_threshold = claude
            claude_dropped = _claude_metric_dropped(
                cell_id, claude_key, claude_threshold, current_main,
                custom_judge_history,
                judge_cell=b.claude_judge_cell,
                min_history=b.min_regression_history,
            )
            if ragas_dropped and claude_dropped:
                findings.append(
                    GateFinding(
                        severity="red",
                        tag=TAG_SCORE_REGRESSION,
                        metric=metric,
                        cell=cell_id,
                        message=(
                            f"RAGAS {metric} for {cell_id} fell to "
                            f"{current_strict:.4f}, below the {len(past_strict)}-run "
                            f"rolling median {ragas_median:.4f} by more than "
                            f"{b.ragas_drop} — corroborated by a same-cell drop in "
                            f"the cross-family Claude {claude_key} judge"
                        ),
                        cross_family_corroborated=True,
                        auto_close_weeks=1,
                    )
                )
            elif ragas_dropped:
                findings.append(
                    GateFinding(
                        severity="yellow",
                        tag=TAG_SCORE_REGRESSION,
                        metric=metric,
                        cell=cell_id,
                        message=(
                            f"RAGAS {metric} for {cell_id} fell to "
                            f"{current_strict:.4f}, below the rolling median "
                            f"{ragas_median:.4f} by more than {b.ragas_drop}, but "
                            f"the cross-family Claude {claude_key} judge did not "
                            "drop — single-judge, not escalated to red"
                        ),
                        cross_family_corroborated=False,
                    )
                )
            elif claude_dropped:
                findings.append(
                    GateFinding(
                        severity="yellow",
                        tag=TAG_SCORE_REGRESSION,
                        metric=metric,
                        cell=cell_id,
                        message=(
                            f"the cross-family Claude {claude_key} judge dropped "
                            f"for {cell_id} but RAGAS {metric} held at "
                            f"{current_strict:.4f} (rolling median "
                            f"{ragas_median:.4f}) — single-judge, not escalated "
                            "to red"
                        ),
                        cross_family_corroborated=False,
                    )
                )
    return findings
