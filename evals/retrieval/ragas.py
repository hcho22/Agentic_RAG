"""US-001: RAGAS scoring module — a parallel eval signal alongside the custom Claude judge.

RAGAS (Retrieval Augmented Generation Assessment) computes the four canonical
RAG metrics — Faithfulness, Answer Relevancy, Context Precision, Context
Recall — over the retrieval eval's 50-question golden set. Those standardized
metric names appear in nearly every reference RAG paper and competitor doc, so
shipping them lets a reader recognize the methodology without reading runner
source.

Same-family bias trade-off
--------------------------
The RAGAS judge LLM is ``gpt-4o-mini`` — the *same model family* as the answer
generator in ``runner.py`` (US-036's ``generate_answer``). A judge that shares
a family with the generator can be systematically lenient: it tends to favour
outputs that "reason like it does". We accept this deliberately:

  * Cost. ``gpt-4o-mini`` is cheap enough to run all four metrics weekly.
  * Independence is preserved elsewhere. The existing custom Claude judge
    (``runner.py::judge_answer``) is a genuine *cross-family* observation —
    different vendor, different model, different prompting technique — and it
    remains the load-bearing headline signal. RAGAS ships *alongside* it for
    standardized-vocabulary parity, not as a replacement.

So RAGAS trades judge independence for recognizable vocabulary; the Claude
judge keeps the independence. The two judges measure overlapping ground from
independent angles.

Lazy import
-----------
``ragas`` / ``langchain_openai`` pull in langchain-core, datasets and pandas —
heavy deps the PR-CI install (``requirements-ci.txt``) deliberately omits. All
RAGAS imports therefore happen *inside* ``score_with_ragas``, mirroring the
``_get_anthropic()`` lazy-import pattern in ``runner.py``. Importing this
module — or ``runner.py`` — never costs the RAGAS install; only an actual
``--include-ragas`` run does.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

log = logging.getLogger("agentic_rag.evals.retrieval.ragas")

# The four canonical RAGAS metrics, in display order.
RAGAS_METRICS: tuple[str, ...] = (
    "faithfulness",
    "answer_relevancy",
    "context_precision",
    "context_recall",
)

# FR-7: a NaN score carries a reason from this fixed enum — never free text —
# so downstream gate logic (US-005+) can branch on it programmatically. A
# score that succeeded stores `None`. An out-of-enum string is coerced to
# `unknown` by `_normalize_nan_reason` rather than stored verbatim.
NAN_REASONS: frozenset[str] = frozenset(
    {
        "judge_refused",
        "parse_error",
        "empty_contexts",
        "metric_error",
        "timeout",
        "unknown",
    }
)

# FR-4: the RAGAS judge LLM is gpt-4o-mini, hardcoded in v1 — deliberately not
# exposed on the CLI so an eval run can't be accidentally misconfigured onto a
# different judge. It shares a model family with the answer generator; see the
# same-family bias note in the module docstring.
RAGAS_JUDGE_MODEL = "gpt-4o-mini"

# FR-3: RAGAS scores hybrid retrieval only. Cross-mode comparison already lives
# in the recall@k tables, so running RAGAS on vector / keyword adds no new
# comparative signal — just cost.
RAGAS_MODE = "hybrid"

# FR-3: of the six (viewer × filter) cells the runner sweeps, RAGAS scores only
# these two. full_access×post_filter is degenerate (full_access sees
# everything, so post-filtering drops nothing); the no_access cells are covered
# by the security table; partial_access×post_filter is covered by the recall
# trade-off table. Only the two pre_filter cells carry new RAGAS signal.
RAGAS_CELLS: frozenset[tuple[str, str]] = frozenset(
    {("full_access", "pre_filter"), ("partial_access", "pre_filter")}
)

# The same two cells as ordered `viewer:filter` id strings — the keys used in
# RagasRow.cell and aggregates.by_cell, and the row order of the summary.md
# RAGAS comparison table. Ordered (frozensets are not) so output is stable.
RAGAS_CELL_IDS: tuple[str, ...] = (
    "full_access:pre_filter",
    "partial_access:pre_filter",
)


def ragas_cell_enabled(mode: str, viewer: str, filter_strategy: str) -> bool:
    """True when RAGAS should score this (mode × viewer × filter) cell.

    The gate is the conjunction of FR-3's two conditions: hybrid mode AND one
    of the two pre_filter cells in ``RAGAS_CELLS``. Every other combination is
    skipped so the weekly cost stays bounded.
    """
    return mode == RAGAS_MODE and (viewer, filter_strategy) in RAGAS_CELLS


@dataclass
class RagasRow:
    """One question's RAGAS scores for a single (cell × mode).

    ``scores`` and ``nan_reasons`` are keyed by the names in ``RAGAS_METRICS``.
    A ``None`` score carries a matching ``nan_reasons`` entry drawn from the
    fixed failure-reason enum (``judge_refused``, ``parse_error``,
    ``empty_contexts``, ``metric_error``, ``timeout``, ``unknown``); a
    successful score stores ``None`` as its reason.
    """

    question_id: str
    cell: str
    mode: str
    scores: dict[str, float | None] = field(default_factory=dict)
    nan_reasons: dict[str, str | None] = field(default_factory=dict)
    api_errors: int = 0
    judge_calls: int = 0


async def score_with_ragas(
    rows: list[dict[str, Any]], judge_model: str
) -> list[RagasRow]:
    """Score ``rows`` with the four canonical RAGAS metrics.

    ``rows`` is the per-question generation detail produced by the runner
    (question, retrieved contexts, generated answer, reference). ``judge_model``
    is the RAGAS judge LLM — ``gpt-4o-mini`` in v1 (see FR-4).

    US-001 scaffold: the real RAGAS pipeline lands in a later story. This body
    only honours the lazy-import contract — it imports ``ragas`` /
    ``langchain_openai`` and, when they are absent, raises an actionable
    ``RuntimeError`` (never a bare ``ImportError``) telling the operator how to
    install them — then returns an empty result.
    """
    try:
        import ragas  # noqa: F401
        from langchain_openai import ChatOpenAI  # noqa: F401
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "--include-ragas requires the `ragas` package. "
            "Run `pip install -r evals/retrieval/requirements.txt`."
        ) from e

    # The RAGAS evaluation pipeline (build EvaluationDataset from `rows`, run
    # the four metrics with ChatOpenAI(model=judge_model) as judge, map results
    # into RagasRow instances) lands in a later story. US-001 ships scaffold only.
    return []


def _normalize_nan_reason(reason: str | None) -> str | None:
    """Coerce a NaN reason onto the fixed `NAN_REASONS` enum (FR-7).

    `None` (the score succeeded) passes through unchanged. An out-of-enum
    string is recorded as `unknown` with a warning rather than stored verbatim
    — an arbitrary reason string would defeat programmatic gate evaluation.
    """
    if reason is None or reason in NAN_REASONS:
        return reason
    log.warning(
        "RAGAS nan_reason %r is not in the fixed enum; recording as 'unknown'",
        reason,
    )
    return "unknown"


def _aggregate_by_cell(rows: list[RagasRow]) -> dict[str, dict[str, Any]]:
    """Per (cell × metric): mean_strict, mean_available, coverage, api_errors.

    `mean_strict` averages over every question in the cell with NaN counted as
    0 — the headline number, so a degraded run can't hide behind a shrinking
    denominator (FR-6: never `nanmean` for a headline). `mean_available`
    averages over non-NaN scores only, and is `None` when a metric scored NaN
    everywhere. `coverage` is the non-NaN fraction. `api_errors` is the cell
    total — RagasRow counts errors per question, not per metric, so the same
    cell total appears in each of the four metric blocks.
    """
    cells: dict[str, list[RagasRow]] = defaultdict(list)
    for row in rows:
        cells[row.cell].append(row)

    by_cell: dict[str, dict[str, Any]] = {}
    for cell, cell_rows in cells.items():
        total = len(cell_rows)
        api_errors = sum(row.api_errors for row in cell_rows)
        metrics: dict[str, Any] = {}
        for metric in RAGAS_METRICS:
            available: list[float] = []
            for row in cell_rows:
                value = row.scores.get(metric)
                if value is not None:
                    available.append(value)
            n_available = len(available)
            metrics[metric] = {
                "mean_strict": round(sum(available) / total, 4),
                "mean_available": (
                    round(sum(available) / n_available, 4)
                    if n_available
                    else None
                ),
                "coverage": round(n_available / total, 4),
                "api_errors": api_errors,
            }
        by_cell[cell] = metrics
    return by_cell


def build_ragas_section(rows: list[RagasRow], judge_model: str) -> dict[str, Any]:
    """Assemble the `ragas` top-level results-JSON section (US-003).

    Shape: `judge_model`, `per_question` (one normalized RagasRow dict per
    scored question), and `aggregates.by_cell`. RAGAS lives under its own
    top-level key so existing consumers of the results JSON — which never look
    for `ragas` — stay byte-stable (FR-5).
    """
    per_question: list[dict[str, Any]] = []
    for row in rows:
        record = asdict(row)
        record["nan_reasons"] = {
            metric: _normalize_nan_reason(reason)
            for metric, reason in row.nan_reasons.items()
        }
        per_question.append(record)

    return {
        "judge_model": judge_model,
        "per_question": per_question,
        "aggregates": {"by_cell": _aggregate_by_cell(rows)},
    }
