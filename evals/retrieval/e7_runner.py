"""US-052/053/054: E7 runner — P1a + P2 + P3 deflection-pipeline scoring.

E7 scores the **deflection pipeline** (ADR-0003), not raw retrieval recall, over
the escalation golden set loaded by `e7.load_escalation_questions` (US-051). This
module owns the runner's three hand-authored legs:

* the **P1a population** (US-052) — questions whose answer is genuinely absent
  from the corpus, which must escalate at the cheap, deterministic *retrieval
  gate* before any draft or judge call is made;
* the **P2 population** (US-053) — questions a faithful grounded answer exists
  for, which must clear the retrieval gate *and* produce a faithful draft, so the
  pipeline should **auto-resolve** them. A P2 that escalates is a
  **false-escalate** (an annoyance cost, not a safety failure); and
* the **P3 population** (US-054) — the moat case: questions with *strong*
  retrieval but **no faithful grounded answer** (the topic is on-corpus, the
  specific fact is not, or the doc defers to a human). A P3 must clear the
  retrieval gate, draft, and then be caught by the *faithfulness* leg → escalate.
  A P3 that **auto-resolves is a false-resolve** — the Risk #3 safety failure the
  false-resolve ceiling governs (US-055/059). A P3 that escalates at the
  *retrieval* gate is **mislabeled** (its retrieval was not actually strong, so it
  never exercised the faithfulness gate the row exists to prove).

The legs differ sharply on determinism and CI placement (US-059):

* **P1a is deterministic** — pure arithmetic on cosine scores, no LLM — so it is
  the per-PR tripwire and may hard-block a merge.
* **P2 and P3 are LLM-judged** — they draft an answer and score that draft's
  faithfulness with the **OFFLINE cross-family Claude judge** (`runner.judge_answer`,
  the same judge the E4 generation table uses), NOT the runtime one-call
  faithfulness gate (`escalation.faithfulness_gate`). The offline judge is a
  cross-family observation (Claude grading gpt-4o-mini drafts), so it does not
  share the same-model bias the runtime gate would, and it is free to be
  slower/multi-pass off the latency path. Because they are LLM-judged, the P2/P3
  legs are **scheduled (weekly)** artifacts; their deflection / false-escalate /
  false-resolve numbers feed the US-055 consolidated rates and the E8 gate
  (US-059). No P2/P3 leg *individually* hard-blocks a merge — the per-PR exit
  code is decided by the deterministic gate invariants (P1a/P1b retrieval-gate +
  the US-058 non-disclosure assertion). The one number the weekly legs can fail
  on is the **consolidated false-resolve rate vs the buyer's ceiling**
  (`assert_false_resolve_ceiling`, US-059) — a pinned safety invariant, never a
  per-PR block (the per-PR run never scores the P3 faithfulness leg, so it carries
  an accepted up-to-a-week detection latency).

P2 and P3 run the **identical** pipeline traversal — `retrieve → retrieval_gate →
[if strong] draft → offline faithfulness judge → auto-resolve-or-escalate`
(`_run_judged_leg`) — and differ ONLY in how they *label* the outcome: a P2
auto-resolve is correct (deflection) while a P3 auto-resolve is a false-resolve;
a P2 escalation is a false-escalate while a P3 faithfulness-leg escalation is the
correct, moat-proving outcome.

Why P1a is special
------------------
The P1a outcome is decided by **pure arithmetic on cosine scores** — there is no
LLM in the loop — so it is deterministic and can hard-block per-PR (US-059's
per-PR tripwire), unlike the P2/P3 faithfulness legs (US-053/054) which are
LLM-judged and therefore weekly. Accordingly this module:

* reuses the **real backend gate** `escalation.retrieval_gate` (US-047) — a future
  PR that breaks the gate breaks E7, exactly as `runner.py` reuses the real
  `search_documents` / `hybrid_search`; and
* makes **zero** draft and **zero** judge calls on the P1a path — this leg has no
  answerer or judge client at all, so "0 draft / 0 judge" is structural, not a
  policy that could regress. The orchestrator records `draft_calls=0` /
  `judge_calls=0` on every decision so the invariant is explicit and auditable in
  the JSON.

A P1a row that *clears* the retrieval gate would proceed to a draft — for a
genuinely-no-context question that is a **false-resolve risk** (Risk #3), the
safety failure the false-resolve ceiling governs (US-055/059). Such a row is
flagged `correct=False` and fails the run.

Scope so far (US-052/053/054/055/056/057): the P1a retrieval-gate leg, the P2
auto-resolve leg, the P3 should-escalate leg, the **consolidated** deflection /
false-resolve / false-escalate metrics (US-055, `compute_e7_metrics`) that roll
the per-leg outcomes into the operating-objective numbers — each carrying its
numerator/denominator + per-population breakdown so the false-resolve number is
verifiable against the buyer's ceiling (US-059) and never folded into an opaque
accuracy score — the **knob sweep** (US-056, `run_e7_sweep`) that grids over
τ_sim / N_min / the offline faithfulness floor, emits the
deflection-vs-false-resolve curve, and selects the deflection-maximizing **knee**
subject to the false-resolve ceiling, and the **P1b** no-access replay (US-057,
`run_e7_p1b`) that re-runs the P2 questions under a no-access viewer (gold
ACL-revoked via the E4 machinery) and asserts they escalate at the retrieval gate
exactly as P1a does — with no privileged second pass — and the **P1b
non-disclosure byte-equality assertion** (US-058, `assert_p1b_non_disclosure`)
that pins the customer-facing escalation bytes byte-for-byte identical to the P1a
generic deferral, so a no-access escalation never discloses that restricted
content exists. Still to land: the CI placement (US-059).
This is kept a **standalone** runner (its own `python -m evals.retrieval.e7_runner`
entry, distinct from the E4 `runner.py`) because the deterministic P1a gate leg is
the per-PR tripwire (US-059) and must not pay for the full E4 sweep + RAGAS
machinery to run; the LLM-judged P2/P3 legs are opt-in via `--include-p2` /
`--include-p3`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal

import httpx
from openai import AsyncOpenAI

if TYPE_CHECKING:
    # Type-only import of the runner's viewer literal so the P1b no-access ACL
    # dict type-checks, without eagerly importing `runner` (its RAGAS/asyncpg
    # deps stay off the core import path — `runner` is lazy-imported in `amain`).
    from .runner import ViewerKind

# Backend on the path so E7 reuses the REAL gate (`escalation.retrieval_gate`)
# and the REAL similarity-threshold resolver — same "real backend functions"
# discipline as `runner.py` (which inserts `backend` then imports `retrieval`).
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(_ROOT / "backend"))

from escalation import (  # noqa: E402
    EscalationConfig,
    RetrievalGateDecision,
    _escalated,
    draft_support_answer,
    get_false_resolve_ceiling,
    retrieval_gate,
)
from retrieval import (  # noqa: E402
    DEFAULT_TOP_K,
    SearchDocumentsResult,
    get_similarity_threshold,
)

from .e7 import (  # noqa: E402
    ESCALATION_GOLD,
    POPULATION_BY_LABEL,
    load_escalation_questions,
    resolve_escalation_gold,
)

log = logging.getLogger("agentic_rag.evals.retrieval.e7")

# P1a is the `no_context` population, P2 the `answerable_faithful` one, P3 the
# `should_escalate` one (US-051 `ESCALATION_LABELS`).
P1A_LABEL = "no_context"
P2_LABEL = "answerable_faithful"
P3_LABEL = "should_escalate"

# The offline Claude judge scores faithfulness on a 1-5 integer scale
# (`runner.JUDGE_PROMPT_TEMPLATE`: 5 = every claim grounded, 4 = mostly grounded
# with minor unsupported phrasing, ...). The P2 leg calls a draft "faithful" — and
# so auto-resolves it — when that score is at least this floor. Default 4 ("mostly
# grounded") balances deflection against grounding; the US-056 knob sweep tunes it.
# Note this is a DIFFERENT scale from the runtime gate's `faithfulness_cutoff`
# ([0,1] in `EscalationConfig`): E7 deliberately scores with the offline 1-5 judge,
# not the runtime gate, so the two thresholds are not interchangeable.
DEFAULT_FAITHFULNESS_JUDGE_MIN = 4

# Issue #26: the P3 mislabel-ratio guard ceiling. The false-resolve safety ceiling
# (US-055/059) is only proven over P3 rows that actually EXERCISE the faithfulness
# gate (cleared retrieval, drafted, and got a verdict). A row that escalates earlier
# is `mislabeled` — a gold-authoring / config defect (e.g. τ_sim drift or a degraded
# answerer making rows escalate before the gate), not a pipeline result — and it
# shrinks the effective sample. The existing positive control (`E7P3Result.passed`)
# only fails the run when EVERY row is mislabeled (zero exercised); this ceiling
# governs the PARTIAL case: when the mislabeled FRACTION over the full presented P3
# population exceeds it, the leg is failed/flagged low-confidence so heavy gold drift
# trips the gate instead of quietly diluting the false-resolve rate (latent today at
# the 3-row gold, a real masking path as the P3 gold grows). Default 0.5 (a majority-
# mislabeled P3 leg is a gold defect); override via E7_P3_MISLABEL_RATIO_MAX or
# --p3-mislabel-ratio-max. The denominator is the full presented population
# (`n_questions`), matching the false-resolve rate's — NOT `len(exercised)`.
DEFAULT_P3_MISLABEL_RATIO_MAX = 0.5


def get_p3_mislabel_ratio_max() -> float:
    """Resolve the issue-#26 P3 mislabel-ratio ceiling from E7_P3_MISLABEL_RATIO_MAX
    (a fraction in [0,1]); falls back to DEFAULT_P3_MISLABEL_RATIO_MAX. An unparseable
    or out-of-range value fails CLOSED (ValueError) rather than silently disabling the
    guard — a misconfigured safety ceiling must not read as "no ceiling"."""
    raw = os.environ.get("E7_P3_MISLABEL_RATIO_MAX")
    if raw is None or not raw.strip():
        return DEFAULT_P3_MISLABEL_RATIO_MAX
    try:
        val = float(raw)
    except ValueError as e:
        raise ValueError(
            f"E7_P3_MISLABEL_RATIO_MAX must be a float in [0,1], got {raw!r}"
        ) from e
    if not 0.0 <= val <= 1.0:
        raise ValueError(f"E7_P3_MISLABEL_RATIO_MAX must be in [0,1], got {val}")
    return val


# A retrieval callable: question -> ranked results (each carrying the pre-fusion
# `cosine_similarity`, US-046). Injected so the orchestrator is unit-testable with
# a call-counting fake AND so the production path wires it to the real
# `hybrid_search` — the deflection pipeline's retrieval mode (US-049).
Retrieve = Callable[[str], Awaitable[list[SearchDocumentsResult]]]

# P2-leg collaborators, injected for the same reason: the production path wires
# them to the REAL backend drafter + the REAL offline Claude judge, while the
# test injects deterministic fakes (no network, no LLM).
#   Draft: (question, chunks) -> drafted answer text.
#   Judge: (question, reference, chunks, draft) -> {"faithfulness", "helpfulness"}
#          integer 1-5 scores from the OFFLINE cross-family Claude judge.
Draft = Callable[[str, list[SearchDocumentsResult]], Awaitable[str]]
Judge = Callable[
    [str, str, list[SearchDocumentsResult], str], Awaitable[dict[str, int]]
]

# The P1b (US-057) no-access retrieval callable: it takes the FULL question dict
# (not just the text) because replaying a P2 question as the no-access viewer must
# first REVOKE that question's gold from the viewer (the E4 `reset_viewer_acls`
# machinery) before retrieving under the viewer's own JWT. The production path
# wires it to that revoke-then-retrieve closure; the test injects a fake mapping
# question id -> the (gold-filtered) rows the viewer would see.
RetrieveNoAccess = Callable[[dict[str, Any]], Awaitable[list[SearchDocumentsResult]]]


@dataclass
class P1aDecision:
    """One P1a row's scored outcome — pure data for the result JSON.

    `decision` is ``"escalate"`` when the retrieval gate called retrieval weak
    (the row is caught at the gate with no draft/judge call — the only correct
    P1a outcome), or ``"draft"`` when the gate called retrieval strong, meaning
    the pipeline WOULD have drafted (a false-resolve risk for a no-context
    question). `top1_cosine` / `n_cleared` come straight off the real gate
    decision so a near-miss (e.g. `top1_cosine` just below `tau_sim`) is visible
    in the JSON. `draft_calls` / `judge_calls` are pinned 0 — the P1a leg never
    drafts or judges.
    """

    question_id: str
    decision: Literal["escalate", "draft"]
    expected: Literal["escalate"]
    correct: bool
    gate_strong: bool
    top1_cosine: float | None
    n_cleared: int
    gate_reason: str
    n_results: int
    draft_calls: int
    judge_calls: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class E7P1aResult:
    """Outcome of the E7 P1a leg. `to_dict()` is what lands in the result JSON."""

    population: str  # "P1a"
    label: str  # "no_context"
    tau_sim: float
    n_min: int
    match_threshold: float
    n_questions: int
    decisions: list[P1aDecision] = field(default_factory=list)

    @property
    def cleared_gate(self) -> list[P1aDecision]:
        """P1a rows that WRONGLY cleared the retrieval gate (would draft → a
        retrieval-leg false-resolve). Empty == clean."""
        return [d for d in self.decisions if not d.correct]

    @property
    def total_draft_calls(self) -> int:
        return sum(d.draft_calls for d in self.decisions)

    @property
    def total_judge_calls(self) -> int:
        return sum(d.judge_calls for d in self.decisions)

    @property
    def passed(self) -> bool:
        """Every P1a row must escalate at the retrieval gate having made ZERO
        draft and ZERO judge calls. A run with no P1a rows is NOT a pass — that
        is a structurally blind eval, not a clean one (mirrors E6's positive
        control: a zero must be a real zero)."""
        return (
            self.n_questions > 0
            and not self.cleared_gate
            and self.total_draft_calls == 0
            and self.total_judge_calls == 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "population": self.population,
            "label": self.label,
            "tau_sim": self.tau_sim,
            "n_min": self.n_min,
            "match_threshold": self.match_threshold,
            "n_questions": self.n_questions,
            "total_draft_calls": self.total_draft_calls,
            "total_judge_calls": self.total_judge_calls,
            "cleared_gate": [d.question_id for d in self.cleared_gate],
            "passed": self.passed,
            "decisions": [d.to_dict() for d in self.decisions],
        }


async def run_e7_p1a(
    *,
    questions: list[dict[str, Any]],
    retrieve: Retrieve,
    config: EscalationConfig,
    match_threshold: float,
) -> E7P1aResult:
    """Score the P1a (`no_context`) rows against the REAL retrieval gate (US-052).

    For each P1a row: retrieve once (the caller injects the production hybrid
    retrieval), then call `escalation.retrieval_gate` — pure arithmetic on the
    pre-fusion cosine (US-046/047). A weak gate escalates the row at the retrieval
    leg with **zero** draft and **zero** judge calls (this leg has no answerer or
    judge client); a row that clears the gate WOULD have drafted, which for a
    genuinely-no-context question is a false-resolve risk and a defect
    (`correct=False`).

    Deterministic over the retrieved cosines — no LLM judge, no draft, no DB
    writes — so it can hard-block per-PR (US-059). Non-P1a rows in `questions`
    are ignored (the P2/P3 legs are US-053/054).
    """
    p1a = [q for q in questions if q.get("escalation") == P1A_LABEL]
    result = E7P1aResult(
        population=POPULATION_BY_LABEL[P1A_LABEL],
        label=P1A_LABEL,
        tau_sim=config.tau_sim,
        n_min=config.n_min,
        match_threshold=match_threshold,
        n_questions=len(p1a),
    )
    for q in p1a:
        rows = await retrieve(q["question"])
        gate = retrieval_gate(rows, config.tau_sim, config.n_min, match_threshold)
        decision: Literal["escalate", "draft"] = (
            "escalate" if not gate.strong else "draft"
        )
        result.decisions.append(
            P1aDecision(
                question_id=q["id"],
                decision=decision,
                expected="escalate",
                correct=(decision == "escalate"),
                gate_strong=gate.strong,
                top1_cosine=gate.top1_cosine,
                n_cleared=gate.n_cleared,
                gate_reason=gate.reason,
                n_results=len(rows),
                draft_calls=0,
                judge_calls=0,
            )
        )
    return result


def render_e7_p1a_section(result: E7P1aResult) -> list[str]:
    """Markdown lines for the E7 P1a block of a summary (deterministic gate leg)."""
    if result.passed:
        verdict = (
            f"PASS — all {result.n_questions} P1a rows escalated at the retrieval "
            "gate (0 draft / 0 judge calls)."
        )
    elif result.cleared_gate:
        verdict = (
            f"FAIL — {len(result.cleared_gate)} P1a row(s) CLEARED the retrieval "
            "gate (would draft → false-resolve risk): "
            + ", ".join(f"`{d.question_id}`" for d in result.cleared_gate)
            + "."
        )
    else:
        verdict = (
            "FAIL — no P1a rows scored; the eval is structurally blind, so a "
            "zero false-resolve is a false pass."
        )

    lines = [
        "",
        "### E7 P1a (US-052) — genuinely-no-context escalation (retrieval gate)",
        "",
        f"τ_sim={result.tau_sim} · N_min={result.n_min} · "
        f"match_threshold={result.match_threshold}. Every P1a question is "
        "genuinely unanswerable, so it must escalate at the deterministic "
        "retrieval gate with **no draft / judge call**.",
        "",
        "| Question | Decision | top1_cosine | n_cleared | gate reason |",
        "|---|---|---|---|---|",
    ]
    for d in result.decisions:
        cosine = "—" if d.top1_cosine is None else f"{d.top1_cosine:.4f}"
        flag = "" if d.correct else " ⚠️"
        lines.append(
            f"| `{d.question_id}` | {d.decision}{flag} | {cosine} | "
            f"{d.n_cleared} | {d.gate_reason} |"
        )
    lines += ["", f"**Verdict:** {verdict}"]
    return lines


# ---------------------------------------------------------------------------
# Shared LLM-judged traversal (US-053/054).
#
# Unlike the deterministic P1a leg, the P2 and P3 legs run the FULL pipeline
# shape: retrieve → retrieval gate → [if strong] draft → faithfulness scoring →
# auto-resolve-or-escalate. They reuse the real backend `retrieval_gate` and
# `draft_support_answer` (a future PR that breaks either breaks E7), but score the
# draft's faithfulness with the OFFLINE cross-family Claude judge (injected
# `Judge`, wired to `runner.judge_answer`), NOT the runtime one-call
# `faithfulness_gate`.
#
# P2 and P3 traverse this pipeline IDENTICALLY and differ ONLY in how they label
# the outcome (a P2 auto-resolve is correct deflection; a P3 auto-resolve is a
# false-resolve). `_run_judged_leg` is that single shared traversal, returning the
# raw `_JudgedLeg` outcome that each leg then maps onto its own decision type — so
# the two legs can never drift in the pipeline they exercise.
# ---------------------------------------------------------------------------


def _render_judge_context(chunks: list[SearchDocumentsResult]) -> str:
    """Render retrieved chunks into the context block the offline judge sees.

    Mirrors what `escalation.draft_support_answer` showed the drafter (`[i]
    content` blocks), so the judge scores the draft's grounding against exactly
    the chunks the drafter had to work from."""
    return "\n\n".join(f"[{i + 1}] {c.content}" for i, c in enumerate(chunks))


@dataclass
class _JudgedLeg:
    """The raw outcome of one judged-leg pipeline traversal (shared by P2 + P3).

    P2 (US-053) and P3 (US-054) run the same deflection pipeline and differ only
    in labeling, so this is the pre-labeling result both consume. `decision` is
    ``"auto_resolve"`` (cleared the gate AND drafted a faithful answer) or
    ``"escalate"``; `escalate_leg` records where an escalation happened —
    ``"retrieval"`` (weak gate, 0 draft/0 judge), ``"draft"`` (empty draft, 1
    draft/0 judge), or ``"faithfulness"`` (judge below the floor, 1 draft/1 judge)
    — and is ``None`` on an auto-resolve. The score / call-count fields carry the
    OFFLINE judge's 1-5 integers and the actual LLM calls made.
    """

    decision: Literal["auto_resolve", "escalate"]
    escalate_leg: Literal["retrieval", "draft", "faithfulness"] | None
    gate: RetrievalGateDecision
    faithfulness_score: int | None
    helpfulness_score: int | None
    faithful: bool | None
    draft: str | None
    n_results: int
    draft_calls: int
    judge_calls: int


async def _run_judged_leg(
    q: dict[str, Any],
    *,
    retrieve: Retrieve,
    draft: Draft,
    judge: Judge,
    config: EscalationConfig,
    match_threshold: float,
    faithfulness_judge_min: int,
) -> _JudgedLeg:
    """Run one question through the deflection pipeline + offline faithfulness
    judge, returning the raw (pre-labeling) `_JudgedLeg` shared by the P2/P3 legs:

        retrieve once → retrieval_gate
            → weak   ⇒ escalate (escalate_leg="retrieval"), ZERO draft/judge calls
            → strong ⇒ draft → [empty?] escalate (escalate_leg="draft", 0 judge)
                            → else offline judge faithfulness
                                → >= floor ⇒ AUTO-RESOLVE
                                → else     ⇒ escalate (escalate_leg="faithfulness")

    Reuses the REAL backend `retrieval_gate` + `draft_support_answer` (injected as
    `draft`) so a regression in either shows up in BOTH legs; the `judge` callable
    is the OFFLINE Claude judge (`runner.judge_answer`), which receives the row's
    `reference` gold answer (validated by `e7.load_escalation_questions`) — the
    runtime one-call gate never sees a reference. Call counts are pinned per branch
    so the LLM cost stays auditable.
    """
    rows = await retrieve(q["question"])
    gate = retrieval_gate(rows, config.tau_sim, config.n_min, match_threshold)

    if not gate.strong:
        # Short-circuit at the retrieval gate: no draft, no judge call.
        return _JudgedLeg(
            decision="escalate", escalate_leg="retrieval", gate=gate,
            faithfulness_score=None, helpfulness_score=None, faithful=None,
            draft=None, n_results=len(rows), draft_calls=0, judge_calls=0,
        )

    draft_text = await draft(q["question"], rows)
    if not draft_text.strip():
        # The drafter produced nothing — escalate with no judge call (no answer
        # to score).
        return _JudgedLeg(
            decision="escalate", escalate_leg="draft", gate=gate,
            faithfulness_score=None, helpfulness_score=None, faithful=None,
            draft=draft_text, n_results=len(rows), draft_calls=1, judge_calls=0,
        )

    scores = await judge(q["question"], q["reference"], rows, draft_text)
    faithfulness_score = int(scores["faithfulness"])
    helpfulness_score = int(scores["helpfulness"])
    faithful = faithfulness_score >= faithfulness_judge_min
    return _JudgedLeg(
        decision="auto_resolve" if faithful else "escalate",
        escalate_leg=None if faithful else "faithfulness",
        gate=gate,
        faithfulness_score=faithfulness_score,
        helpfulness_score=helpfulness_score,
        faithful=faithful,
        draft=draft_text,
        n_results=len(rows),
        draft_calls=1,
        judge_calls=1,
    )


# ---------------------------------------------------------------------------
# US-053: P2 (answerable + faithful) — end-to-end auto-resolve scoring.
#
# A P2 row should AUTO-RESOLVE (strong retrieval + a faithful draft); any
# escalation is a false-escalate (a tunable quality metric, US-055/059 — never a
# pinned safety invariant like a P1a false-resolve or a P3 false-resolve).
# ---------------------------------------------------------------------------


@dataclass
class P2Decision:
    """One P2 row's end-to-end outcome — pure data for the result JSON.

    `decision` is ``"auto_resolve"`` when the row cleared the retrieval gate AND
    its draft scored faithful (`faithfulness_score >= faithfulness_judge_min`) —
    the only correct P2 outcome, counted toward deflection — or ``"escalate"``
    otherwise (a **false-escalate**). `escalate_leg` records where it escalated:
    ``"retrieval"`` (weak gate — no draft/judge call), ``"draft"`` (the drafter
    produced nothing — no judge call), or ``"faithfulness"`` (the offline judge
    scored the draft below the floor). It is ``None`` on an auto-resolve.

    `faithfulness_score` / `helpfulness_score` are the OFFLINE Claude judge's 1-5
    integers (`None` when the row escalated before a judge call). `draft_calls` /
    `judge_calls` are the actual calls this row made (0/0 at the retrieval gate,
    1/0 on an empty draft, 1/1 once judged) so the LLM cost is auditable.
    """

    question_id: str
    decision: Literal["auto_resolve", "escalate"]
    expected: Literal["auto_resolve"]
    correct: bool
    false_escalate: bool
    escalate_leg: Literal["retrieval", "draft", "faithfulness"] | None
    gate_strong: bool
    top1_cosine: float | None
    n_cleared: int
    gate_reason: str
    faithfulness_score: int | None
    helpfulness_score: int | None
    faithfulness_judge_min: int
    faithful: bool | None
    n_results: int
    draft: str | None
    draft_calls: int
    judge_calls: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class E7P2Result:
    """Outcome of the E7 P2 leg. `to_dict()` is what lands in the result JSON.

    `judge_model` records the OFFLINE Claude judge that scored faithfulness (so
    the JSON is self-describing about which judge produced the numbers). The
    deflection / false-escalate rates are computed over this P2 population alone;
    US-055 consolidates them with P3 into the canonical three-rate framework.
    """

    population: str  # "P2"
    label: str  # "answerable_faithful"
    tau_sim: float
    n_min: int
    match_threshold: float
    faithfulness_judge_min: int
    judge_model: str
    n_questions: int
    decisions: list[P2Decision] = field(default_factory=list)

    @property
    def auto_resolved(self) -> list[P2Decision]:
        return [d for d in self.decisions if d.decision == "auto_resolve"]

    @property
    def false_escalates(self) -> list[P2Decision]:
        """P2 rows that escalated — every one is a false-escalate (annoyance)."""
        return [d for d in self.decisions if d.false_escalate]

    @property
    def deflection_rate(self) -> float | None:
        """Correctly auto-resolved / answerable. The whole P2 population is
        answerable, so this is the P2-local deflection rate (US-055 consolidates
        P2+P3 into the canonical rate). `None` over an empty population."""
        if not self.decisions:
            return None
        return len(self.auto_resolved) / len(self.decisions)

    @property
    def false_escalate_rate(self) -> float | None:
        """Wrongly escalated / answerable over the P2 population. `None` when
        empty. This is the complement of `deflection_rate`."""
        if not self.decisions:
            return None
        return len(self.false_escalates) / len(self.decisions)

    @property
    def total_draft_calls(self) -> int:
        return sum(d.draft_calls for d in self.decisions)

    @property
    def total_judge_calls(self) -> int:
        return sum(d.judge_calls for d in self.decisions)

    @property
    def passed(self) -> bool:
        """Structural validity only: a non-empty P2 population was scored. An
        empty population is NOT a pass — a deflection rate over zero answerable
        questions is structurally blind (mirrors P1a's positive-control guard).

        Deliberately does NOT fail on false-escalates: deflection / false-escalate
        are *tunable quality metrics*, not pinned safety invariants (US-059 — the
        E8 gate governs comment-vs-fail on them). The per-PR hard block is the P1a
        leg; this LLM-judged leg only reports."""
        return self.n_questions > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "population": self.population,
            "label": self.label,
            "tau_sim": self.tau_sim,
            "n_min": self.n_min,
            "match_threshold": self.match_threshold,
            "faithfulness_judge_min": self.faithfulness_judge_min,
            "judge_model": self.judge_model,
            "n_questions": self.n_questions,
            "n_auto_resolved": len(self.auto_resolved),
            "n_false_escalate": len(self.false_escalates),
            "deflection_rate": self.deflection_rate,
            "false_escalate_rate": self.false_escalate_rate,
            "total_draft_calls": self.total_draft_calls,
            "total_judge_calls": self.total_judge_calls,
            "false_escalates": [d.question_id for d in self.false_escalates],
            "passed": self.passed,
            "decisions": [d.to_dict() for d in self.decisions],
        }


async def run_e7_p2(
    *,
    questions: list[dict[str, Any]],
    retrieve: Retrieve,
    draft: Draft,
    judge: Judge,
    config: EscalationConfig,
    match_threshold: float,
    judge_model: str,
    faithfulness_judge_min: int = DEFAULT_FAITHFULNESS_JUDGE_MIN,
) -> E7P2Result:
    """Score the P2 (`answerable_faithful`) rows end-to-end (US-053).

    For each P2 row the leg runs the shared deflection-pipeline traversal
    (`_run_judged_leg`) — retrieve → retrieval gate → [if strong] draft → OFFLINE
    faithfulness judge → auto-resolve-or-escalate — then labels the outcome with
    P2 semantics: an auto-resolve is correct (deflection), any escalation is a
    false-escalate (`escalate_leg` records whether it was the retrieval gate, an
    empty draft, or the faithfulness judge).

    Reuses the real backend `retrieval_gate` + `draft_support_answer` (injected as
    `draft`) so a regression in either shows up here; the `judge` callable is the
    offline Claude judge (`runner.judge_answer`). Each P2 row carries a `reference`
    gold answer (validated by `e7.load_escalation_questions`) the judge scores
    against. This leg makes live LLM calls, so it is NOT deterministic and is a
    scheduled/weekly artifact (US-059). Non-P2 rows in `questions` are ignored.
    """
    p2 = [q for q in questions if q.get("escalation") == P2_LABEL]
    result = E7P2Result(
        population=POPULATION_BY_LABEL[P2_LABEL],
        label=P2_LABEL,
        tau_sim=config.tau_sim,
        n_min=config.n_min,
        match_threshold=match_threshold,
        faithfulness_judge_min=faithfulness_judge_min,
        judge_model=judge_model,
        n_questions=len(p2),
    )
    for q in p2:
        leg = await _run_judged_leg(
            q,
            retrieve=retrieve,
            draft=draft,
            judge=judge,
            config=config,
            match_threshold=match_threshold,
            faithfulness_judge_min=faithfulness_judge_min,
        )
        result.decisions.append(
            P2Decision(
                question_id=q["id"],
                decision=leg.decision,
                expected="auto_resolve",
                correct=(leg.decision == "auto_resolve"),
                false_escalate=(leg.decision == "escalate"),
                escalate_leg=leg.escalate_leg,
                gate_strong=leg.gate.strong,
                top1_cosine=leg.gate.top1_cosine,
                n_cleared=leg.gate.n_cleared,
                gate_reason=leg.gate.reason,
                faithfulness_score=leg.faithfulness_score,
                helpfulness_score=leg.helpfulness_score,
                faithfulness_judge_min=faithfulness_judge_min,
                faithful=leg.faithful,
                n_results=leg.n_results,
                draft=leg.draft,
                draft_calls=leg.draft_calls,
                judge_calls=leg.judge_calls,
            )
        )
    return result


def render_e7_p2_section(result: E7P2Result) -> list[str]:
    """Markdown lines for the E7 P2 block of a summary (LLM-judged quality leg)."""
    if result.n_questions == 0:
        verdict = (
            "BLIND — no P2 rows scored; the deflection rate is over zero "
            "answerable questions, so it is structurally meaningless."
        )
    else:
        deflection = result.deflection_rate or 0.0
        n_fe = len(result.false_escalates)
        verdict = (
            f"deflection {deflection:.0%} "
            f"({len(result.auto_resolved)}/{result.n_questions} auto-resolved); "
            f"{n_fe} false-escalate(s)"
        )
        if result.false_escalates:
            verdict += " — " + ", ".join(
                f"`{d.question_id}` ({d.escalate_leg})"
                for d in result.false_escalates
            )
        verdict += "."

    lines = [
        "",
        "### E7 P2 (US-053) — answerable + faithful auto-resolve (offline judge)",
        "",
        f"τ_sim={result.tau_sim} · N_min={result.n_min} · "
        f"match_threshold={result.match_threshold} · "
        f"faithfulness≥{result.faithfulness_judge_min}/5 "
        f"(offline judge `{result.judge_model}`). Every P2 question has a faithful "
        "grounded answer, so the pipeline should **auto-resolve** it; an escalation "
        "is a false-escalate. Quality metric — not a per-PR hard block (US-059).",
        "",
        "| Question | Decision | top1_cosine | faithfulness | leg |",
        "|---|---|---|---|---|",
    ]
    for d in result.decisions:
        cosine = "—" if d.top1_cosine is None else f"{d.top1_cosine:.4f}"
        faith = "—" if d.faithfulness_score is None else f"{d.faithfulness_score}/5"
        leg = "—" if d.escalate_leg is None else d.escalate_leg
        flag = "" if d.correct else " ⚠️"
        lines.append(
            f"| `{d.question_id}` | {d.decision}{flag} | {cosine} | "
            f"{faith} | {leg} |"
        )
    lines += ["", f"**Verdict:** {verdict}"]
    return lines


# ---------------------------------------------------------------------------
# US-054: P3 (should-escalate) — the moat case.
#
# A P3 row has STRONG retrieval but NO faithful grounded answer (the topic is
# on-corpus, the specific fact is not, or the doc defers to a human). It runs the
# SAME shared pipeline traversal as P2 (`_run_judged_leg`) but with the OPPOSITE
# expectation: the only correct P3 outcome is to clear the retrieval gate, draft,
# and then be caught by the FAITHFULNESS leg → escalate. The labeling therefore
# flips relative to P2:
#
#   * escalate at the faithfulness leg ⇒ CORRECT (the moat working);
#   * auto-resolve                     ⇒ FALSE-RESOLVE — the Risk #3 safety
#     failure the false-resolve ceiling governs (US-055/059), NOT a false-escalate;
#   * escalate at the retrieval/draft leg ⇒ MISLABELED — the row escalated (the
#     safe direction) but never exercised the faithfulness gate it exists to
#     prove, so its retrieval was not actually strong (a gold-authoring defect),
#     not a pipeline result.
#
# Like P2 this leg is LLM-judged (offline cross-family Claude judge), so it is a
# scheduled/weekly artifact and only REPORTS here — the false-resolve RATE vs the
# buyer's ceiling is consolidated in US-055 and enforced in CI by US-059. This
# runner's exit code stays decided by the deterministic P1a invariant alone.
# ---------------------------------------------------------------------------


@dataclass
class P3Decision:
    """One P3 row's end-to-end outcome — pure data for the result JSON.

    `decision` is ``"escalate"`` (the expected outcome) or ``"auto_resolve"``.
    The three booleans are mutually exclusive and capture the P3 taxonomy:

    * `correct` — escalated at the FAITHFULNESS leg (the moat working): strong
      retrieval, a draft, and the offline judge scoring it below the floor.
    * `false_resolve` — auto-resolved: the draft scored faithful and the pipeline
      WOULD have auto-sent an answer to an unanswerable question. This is the
      Risk #3 safety failure (US-055/059), tallied toward the false-resolve rate.
    * `mislabeled` — escalated at the retrieval or draft leg, so it never reached
      the faithfulness judge. It escalated (the safe direction, so NOT a
      false-resolve) but did not exercise the gate the row exists to prove —
      surfaced so the gold row can be re-authored.

    `escalate_leg` records where an escalation happened (``None`` on an
    auto-resolve); `faithfulness_score` / `helpfulness_score` are the OFFLINE
    judge's 1-5 integers (``None`` when the row escalated before a judge call);
    `draft_calls` / `judge_calls` are the actual calls made so the LLM cost is
    auditable.
    """

    question_id: str
    decision: Literal["escalate", "auto_resolve"]
    expected: Literal["escalate"]
    correct: bool
    false_resolve: bool
    mislabeled: bool
    escalate_leg: Literal["retrieval", "draft", "faithfulness"] | None
    gate_strong: bool
    top1_cosine: float | None
    n_cleared: int
    gate_reason: str
    faithfulness_score: int | None
    helpfulness_score: int | None
    faithfulness_judge_min: int
    faithful: bool | None
    n_results: int
    draft: str | None
    draft_calls: int
    judge_calls: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class E7P3Result:
    """Outcome of the E7 P3 leg. `to_dict()` is what lands in the result JSON.

    `judge_model` records the OFFLINE Claude judge that scored faithfulness. The
    `false_resolve_rate` computed here is the P3-local rate; US-055 carries it as
    the gated faithfulness-leg false-resolve number the buyer's ceiling governs (the
    retrieval-leg P1a/P1b false-resolves are pinned separately, not folded in).
    """

    population: str  # "P3"
    label: str  # "should_escalate"
    tau_sim: float
    n_min: int
    match_threshold: float
    faithfulness_judge_min: int
    judge_model: str
    n_questions: int
    decisions: list[P3Decision] = field(default_factory=list)

    @property
    def escalated_at_faithfulness(self) -> list[P3Decision]:
        """P3 rows that escalated at the faithfulness leg — the moat working."""
        return [d for d in self.decisions if d.correct]

    @property
    def false_resolves(self) -> list[P3Decision]:
        """P3 rows that auto-resolved — every one is a false-resolve (the safety
        failure). The list of ids is surfaced in the JSON so a regression is
        attributable."""
        return [d for d in self.decisions if d.false_resolve]

    @property
    def mislabeled(self) -> list[P3Decision]:
        """P3 rows that escalated WITHOUT exercising the faithfulness gate
        (retrieval/draft leg) — a gold-authoring defect, not a pipeline result."""
        return [d for d in self.decisions if d.mislabeled]

    @property
    def exercised(self) -> list[P3Decision]:
        """P3 rows that actually REACHED and were judged by the faithfulness gate:
        cleared retrieval, produced a non-empty draft, and got a faithfulness
        verdict (whether the judge scored it below the floor → `correct`, or at/above
        it → `false_resolve`). The complement of `mislabeled` (rows that escalated at
        the retrieval/draft leg, never reaching the gate). The false-resolve ceiling
        is only meaningful over a population with ≥1 exercised row — a population that
        is empty OR entirely mislabeled never exercises the gate, so its measured rate
        is vacuous (`None` or a 0/n that proves nothing)."""
        return [d for d in self.decisions if not d.mislabeled]

    @property
    def false_resolve_rate(self) -> float | None:
        """Wrongly auto-resolved / unanswerable over the P3 population — the
        SAFETY number the false-resolve ceiling governs (US-055/059).

        Denominator is the FULL presented P3 population (`len(self.decisions)` ==
        `n_questions`), INCLUDING `mislabeled` rows; numerator is the false-resolves
        only (a mislabeled row escalated, so it is never a false-resolve and is
        excluded from the numerator but kept in the denominator). This is the
        operating-metric meaning — "of all unanswerable questions presented, what
        fraction did we wrongly auto-resolve" — consistent with how P1a/P1b feed the
        same summed consolidated rate. It is deliberately NOT `len(exercised)`: an
        exercised-only denominator would contradict this docstring and mix per-
        population denominators in the sum-based consolidated rate (the rejected
        "denominator misread", issue #26). The dilution risk a high mislabeled
        fraction creates is guarded separately by the mislabel-ratio gate
        (`mislabel_ratio`, issue #26), not by changing this denominator.

        `None` over an empty population (a rate over zero questions is structurally
        blind)."""
        if not self.decisions:
            return None
        return len(self.false_resolves) / len(self.decisions)

    @property
    def mislabel_ratio(self) -> float | None:
        """Fraction of the presented P3 population that was `mislabeled` (escalated
        before exercising the faithfulness gate). Denominator is the full presented
        population (`len(self.decisions)` == `n_questions`), matching
        `false_resolve_rate`. A high ratio shrinks the effective sample the
        false-resolve ceiling is measured over and is guarded by the issue-#26
        mislabel-ratio gate in `e7_pinned_invariants_failed`. `None` over an empty
        population (a ratio over zero rows is structurally blind, like the rate)."""
        if not self.decisions:
            return None
        return len(self.mislabeled) / len(self.decisions)

    @property
    def total_draft_calls(self) -> int:
        return sum(d.draft_calls for d in self.decisions)

    @property
    def total_judge_calls(self) -> int:
        return sum(d.judge_calls for d in self.decisions)

    @property
    def passed(self) -> bool:
        """Positive control for the false-resolve ceiling, which is fed SOLELY by
        this leg (US-059). True iff ≥1 P3 row actually EXERCISED the faithfulness
        gate (see `exercised`). A population that is empty OR entirely `mislabeled`
        (every row escalated at the retrieval/draft leg) never reaches the gate, so
        its measured false-resolve rate is vacuous — `None` over zero rows, or a
        0/n that proves nothing — and the ceiling is structurally blind. Neither is
        a clean pass (mirrors P1a/P2's positive control: a zero must be a real zero).

        Deliberately does NOT fail on false-resolves themselves: the false-resolve
        RATE vs the buyer's ceiling is consolidated and enforced in US-055/059, not
        per-leg here. This LLM-judged leg only reports; the per-PR hard block is
        the deterministic P1a leg."""
        return len(self.exercised) > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "population": self.population,
            "label": self.label,
            "tau_sim": self.tau_sim,
            "n_min": self.n_min,
            "match_threshold": self.match_threshold,
            "faithfulness_judge_min": self.faithfulness_judge_min,
            "judge_model": self.judge_model,
            "n_questions": self.n_questions,
            "n_escalated_at_faithfulness": len(self.escalated_at_faithfulness),
            "n_false_resolve": len(self.false_resolves),
            "n_mislabeled": len(self.mislabeled),
            "mislabel_ratio": self.mislabel_ratio,
            "false_resolve_rate": self.false_resolve_rate,
            "total_draft_calls": self.total_draft_calls,
            "total_judge_calls": self.total_judge_calls,
            "false_resolves": [d.question_id for d in self.false_resolves],
            "mislabeled": [d.question_id for d in self.mislabeled],
            "passed": self.passed,
            "decisions": [d.to_dict() for d in self.decisions],
        }


async def run_e7_p3(
    *,
    questions: list[dict[str, Any]],
    retrieve: Retrieve,
    draft: Draft,
    judge: Judge,
    config: EscalationConfig,
    match_threshold: float,
    judge_model: str,
    faithfulness_judge_min: int = DEFAULT_FAITHFULNESS_JUDGE_MIN,
) -> E7P3Result:
    """Score the P3 (`should_escalate`) rows end-to-end (US-054) — the moat case.

    For each P3 row the leg runs the SAME shared deflection-pipeline traversal as
    P2 (`_run_judged_leg`) but labels the outcome with P3 semantics: the only
    correct outcome is to clear the retrieval gate, draft, and be caught by the
    FAITHFULNESS leg → escalate. An auto-resolve is a **false-resolve** (the Risk
    #3 safety failure); an escalation at the retrieval/draft leg is **mislabeled**
    (the row never exercised the faithfulness gate, so its gold retrieval was not
    actually strong).

    Reuses the real backend `retrieval_gate` + `draft_support_answer`; faithfulness
    is scored by the OFFLINE Claude judge (`runner.judge_answer`) against the row's
    `reference` should-escalate gold (validated by `e7.load_escalation_questions`,
    US-054). LLM-judged, so NOT deterministic and a scheduled/weekly artifact
    (US-059); the false-resolve rate it records feeds US-055/059, never blocking a
    merge here. Non-P3 rows in `questions` are ignored.
    """
    p3 = [q for q in questions if q.get("escalation") == P3_LABEL]
    result = E7P3Result(
        population=POPULATION_BY_LABEL[P3_LABEL],
        label=P3_LABEL,
        tau_sim=config.tau_sim,
        n_min=config.n_min,
        match_threshold=match_threshold,
        faithfulness_judge_min=faithfulness_judge_min,
        judge_model=judge_model,
        n_questions=len(p3),
    )
    for q in p3:
        leg = await _run_judged_leg(
            q,
            retrieve=retrieve,
            draft=draft,
            judge=judge,
            config=config,
            match_threshold=match_threshold,
            faithfulness_judge_min=faithfulness_judge_min,
        )
        # P3 labeling: correct iff escalated at the faithfulness leg (the moat);
        # auto-resolve is the safety failure; any pre-faithfulness escalation is a
        # mislabeled (under-strong) gold row.
        escalated_faithfulness = (
            leg.decision == "escalate" and leg.escalate_leg == "faithfulness"
        )
        false_resolve = leg.decision == "auto_resolve"
        mislabeled = (
            leg.decision == "escalate" and leg.escalate_leg != "faithfulness"
        )
        result.decisions.append(
            P3Decision(
                question_id=q["id"],
                decision=leg.decision,
                expected="escalate",
                correct=escalated_faithfulness,
                false_resolve=false_resolve,
                mislabeled=mislabeled,
                escalate_leg=leg.escalate_leg,
                gate_strong=leg.gate.strong,
                top1_cosine=leg.gate.top1_cosine,
                n_cleared=leg.gate.n_cleared,
                gate_reason=leg.gate.reason,
                faithfulness_score=leg.faithfulness_score,
                helpfulness_score=leg.helpfulness_score,
                faithfulness_judge_min=faithfulness_judge_min,
                faithful=leg.faithful,
                n_results=leg.n_results,
                draft=leg.draft,
                draft_calls=leg.draft_calls,
                judge_calls=leg.judge_calls,
            )
        )
    return result


def render_e7_p3_section(result: E7P3Result) -> list[str]:
    """Markdown lines for the E7 P3 block of a summary (the moat / safety leg)."""
    if result.n_questions == 0:
        verdict = (
            "BLIND — no P3 rows scored; the false-resolve rate is over zero "
            "unanswerable questions, so it is structurally meaningless."
        )
    else:
        n_fr = len(result.false_resolves)
        n_ok = len(result.escalated_at_faithfulness)
        fr_rate = result.false_resolve_rate or 0.0
        verdict = (
            f"false-resolve {fr_rate:.0%} ({n_fr}/{result.n_questions}); "
            f"{n_ok} correctly escalated at the faithfulness gate"
        )
        if result.false_resolves:
            verdict += " — FALSE-RESOLVE (safety): " + ", ".join(
                f"`{d.question_id}`" for d in result.false_resolves
            )
        if result.mislabeled:
            verdict += " — mislabeled (escalated before the faithfulness gate): " + ", ".join(
                f"`{d.question_id}` ({d.escalate_leg})" for d in result.mislabeled
            )
        verdict += "."

    lines = [
        "",
        "### E7 P3 (US-054) — should-escalate / the moat (offline judge)",
        "",
        f"τ_sim={result.tau_sim} · N_min={result.n_min} · "
        f"match_threshold={result.match_threshold} · "
        f"faithfulness≥{result.faithfulness_judge_min}/5 "
        f"(offline judge `{result.judge_model}`). Every P3 question has strong "
        "retrieval but **no faithful grounded answer**, so the pipeline must clear "
        "the retrieval gate, draft, and then **escalate at the faithfulness gate**. "
        "An auto-resolve is a **false-resolve** (the Risk #3 safety failure the "
        "ceiling governs, US-055/059) — not a per-PR block here (LLM-judged).",
        "",
        "| Question | Decision | top1_cosine | faithfulness | leg | flag |",
        "|---|---|---|---|---|---|",
    ]
    for d in result.decisions:
        cosine = "—" if d.top1_cosine is None else f"{d.top1_cosine:.4f}"
        faith = "—" if d.faithfulness_score is None else f"{d.faithfulness_score}/5"
        leg = "—" if d.escalate_leg is None else d.escalate_leg
        if d.false_resolve:
            flag = "❌ false-resolve"
        elif d.mislabeled:
            flag = "⚠️ mislabeled"
        else:
            flag = ""
        lines.append(
            f"| `{d.question_id}` | {d.decision} | {cosine} | "
            f"{faith} | {leg} | {flag} |"
        )
    lines += ["", f"**Verdict:** {verdict}"]
    return lines


# ---------------------------------------------------------------------------
# US-057: P1b — viewer-parameterized no-access population (reuse E4).
#
# P1b is NOT a hand-authored gold population — US-051 adds no `p1b` label. It is
# the SAME P2 (`answerable_faithful`) questions REPLAYED under a NO-ACCESS viewer:
# the E4 `reset_viewer_acls` / `compute_visible_stable_ids(... "no_access" ...)`
# machinery revokes each question's gold from the viewer, so from the viewer's own
# retrieval the gold is invisible and retrieval is weak. The only correct P1b
# outcome is therefore IDENTICAL to P1a — escalate at the deterministic retrieval
# gate — and, like P1a, the leg makes ZERO draft and ZERO judge calls.
#
# The point of P1b is the access-filtered case: the same question a full-access
# viewer can answer (P2) must escalate for a no-access viewer, and must do so
# WITHOUT the customer learning that restricted content exists. That
# non-disclosure is structurally guaranteed here by what is ABSENT: there is NO
# privileged second pass. The leg is given exactly one retrieval callable — the
# no-access viewer's own (gold-filtered) retrieval — and never an unfiltered /
# owner retrieval, so a "this question is actually answerable for someone" signal
# can never reach the decision. P1b is decided ONLY from the no-access viewer's
# filtered retrieval, exactly as P1a is, so its customer-facing output is
# byte-identical to P1a's generic deferral (the invariant US-058 pins).
#
# A P1b row that CLEARS the gate is a defect (`correct=False`) — worse than a P1a
# near-miss: it means the access filter LEAKED the gold to a no-access viewer (a
# real isolation failure, E6/AU4 territory) or the question is answerable from
# non-gold chunks (a gold-authoring defect). Either way it would draft for a
# no-access viewer — a false-resolve AND a disclosure risk — so it fails the run,
# the same pinned shape as a P1a cleared gate. Deterministic (gate-only, no LLM),
# so P1b is part of the per-PR tripwire (US-059), not the weekly LLM-judged sweep.
# ---------------------------------------------------------------------------


@dataclass
class P1bDecision:
    """One P1b row's scored outcome — pure data for the result JSON.

    The `question_id` is the SOURCE P2 question replayed under the no-access viewer.

    The correctness signal is **identity-based**, NOT gate-based: a P1b row LEAKS
    iff a gold `stable_id` actually appears in the no-access viewer's retrieved set
    (`leaked_gold_ids` non-empty). `correct` is therefore "no gold reached the
    no-access viewer" — the real isolation invariant — and is decoupled from the
    answerability gate. The gate fields (`decision` / `gate_strong` / `top1_cosine`
    / `n_cleared` / `gate_reason`) are kept for REPORTING only: a no-access viewer
    legitimately holds every non-gold corpus chunk (the E4 no-access construction
    grants all-but-the-gold), so its retrieval can read strong off topic-adjacent
    NON-gold chunks (e.g. the gold document's sibling chunk) without any gold
    leaking — `decision == "draft"` with `leaked_gold_ids == []` is correct, not a
    leak. Conversely a gold chunk returned at a cosine below `tau_sim` (gate weak)
    is STILL a leak: `leaked_gold_ids` catches it regardless of cosine.

    `returned_stable_ids` is the no-access viewer's retrieved stable ids (mapped
    from the chunk ids via the corpus `stable_id` map); `gold_stable_ids` is the
    replayed question's gold; `leaked_gold_ids = gold ∩ returned`.
    """

    question_id: str
    decision: Literal["escalate", "draft"]
    expected: Literal["escalate"]
    correct: bool
    gate_strong: bool
    top1_cosine: float | None
    n_cleared: int
    gate_reason: str
    n_results: int
    returned_stable_ids: list[str]
    gold_stable_ids: list[str]
    leaked_gold_ids: list[str]
    draft_calls: int
    judge_calls: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class E7P1bResult:
    """Outcome of the E7 P1b leg (US-057). `to_dict()` lands in the result JSON.

    `source_label` records that P1b is the `answerable_faithful` (P2) population
    replayed under a no-access viewer — P1b carries no gold of its own. The
    pass/fail shape is the real ISOLATION invariant: no row may return a gold chunk
    to the no-access viewer (`leaked_gold_ids` empty for every row), with ZERO draft
    and ZERO judge calls, and an empty population is NOT a pass (a structurally-blind
    eval, not a clean one). The answerability gate (strong/weak) is NOT the pass
    criterion — a no-access viewer can legitimately retrieve non-gold corpus chunks
    it is granted — so a strong gate with no gold leaked is clean.
    """

    population: str  # "P1b"
    source_label: str  # "answerable_faithful" (the replayed P2 population)
    tau_sim: float
    n_min: int
    match_threshold: float
    n_questions: int
    decisions: list[P1bDecision] = field(default_factory=list)

    @property
    def cleared_gate(self) -> list[P1bDecision]:
        """P1b rows that LEAKED — a gold `stable_id` actually appeared in the
        no-access viewer's retrieved set (`leaked_gold_ids` non-empty), an isolation
        / disclosure failure. Empty == clean. (Named `cleared_gate` for caller
        continuity; the leak signal is now measured gold disclosure, not a strong
        gate — a strong gate off legitimately-granted non-gold chunks is NOT a
        leak, and a gold chunk returned below `tau_sim` IS.)"""
        return [d for d in self.decisions if not d.correct]

    @property
    def total_draft_calls(self) -> int:
        return sum(d.draft_calls for d in self.decisions)

    @property
    def total_judge_calls(self) -> int:
        return sum(d.judge_calls for d in self.decisions)

    @property
    def passed(self) -> bool:
        """No P1b row may leak gold to the no-access viewer (`cleared_gate` empty),
        with ZERO draft/judge calls. A run with no P1b rows is NOT a pass — there
        were no P2 questions to replay, so the access-filtered case is structurally
        blind (mirrors P1a)."""
        return (
            self.n_questions > 0
            and not self.cleared_gate
            and self.total_draft_calls == 0
            and self.total_judge_calls == 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "population": self.population,
            "source_label": self.source_label,
            "tau_sim": self.tau_sim,
            "n_min": self.n_min,
            "match_threshold": self.match_threshold,
            "n_questions": self.n_questions,
            "total_draft_calls": self.total_draft_calls,
            "total_judge_calls": self.total_judge_calls,
            "cleared_gate": [d.question_id for d in self.cleared_gate],
            "passed": self.passed,
            "decisions": [d.to_dict() for d in self.decisions],
        }


async def run_e7_p1b(
    *,
    questions: list[dict[str, Any]],
    retrieve_no_access: RetrieveNoAccess,
    config: EscalationConfig,
    match_threshold: float,
    stable_id_by_chunk_id: dict[str, str],
) -> E7P1bResult:
    """Score the P1b population — P2 questions replayed under a no-access viewer
    (US-057).

    For each `answerable_faithful` (P2) row, retrieve via `retrieve_no_access` —
    the production path revokes that question's gold from the no-access viewer
    (the E4 `reset_viewer_acls` machinery) and then retrieves under the viewer's
    own JWT, so the gold is filtered out by RLS + the membership clause.

    The leak verdict is **identity-based**: each returned chunk id is mapped to its
    corpus `stable_id` (via `stable_id_by_chunk_id`) and a row LEAKS iff a gold
    stable id actually appears in the no-access viewer's retrieved set
    (`leaked_gold_ids = gold ∩ returned`). That — not the answerability gate — is
    the real isolation invariant: a no-access viewer legitimately holds every
    non-gold corpus chunk (the E4 no-access construction grants all-but-the-gold),
    so its retrieval can read STRONG off topic-adjacent non-gold chunks (e.g. the
    gold document's sibling chunk) with no gold leaked — that is correct, not a
    leak. Conversely a gold chunk returned below `tau_sim` (gate weak) is STILL a
    leak. A row whose `leaked_gold_ids` is non-empty is `correct=False` and fails
    the run. The gate decision is still recorded (for reporting + the US-058
    escalation-path non-disclosure check), but it no longer decides pass/fail. This
    leg makes ZERO draft and ZERO judge calls (it has no answerer/judge client).

    There is NO privileged second pass: the leg is given only the no-access
    retrieval callable, so P1b is decided purely from the no-access viewer's own
    retrieval (US-057/058). Non-P2 rows are ignored.
    """
    p2 = [q for q in questions if q.get("escalation") == P2_LABEL]
    result = E7P1bResult(
        population="P1b",
        source_label=P2_LABEL,
        tau_sim=config.tau_sim,
        n_min=config.n_min,
        match_threshold=match_threshold,
        n_questions=len(p2),
    )
    for q in p2:
        rows = await retrieve_no_access(q)
        gate = retrieval_gate(rows, config.tau_sim, config.n_min, match_threshold)
        decision: Literal["escalate", "draft"] = (
            "escalate" if not gate.strong else "draft"
        )
        # Map the no-access viewer's retrieved chunk ids to corpus stable ids, then
        # measure the actual leak: gold stable ids present in the retrieved set.
        returned_stable_ids = [
            stable_id_by_chunk_id[r.id]
            for r in rows
            if r.id in stable_id_by_chunk_id
        ]
        gold_stable_ids = list(q.get("gold_stable_ids") or [])
        leaked_gold_ids = sorted(set(gold_stable_ids) & set(returned_stable_ids))
        result.decisions.append(
            P1bDecision(
                question_id=q["id"],
                decision=decision,
                expected="escalate",
                correct=(not leaked_gold_ids),
                gate_strong=gate.strong,
                top1_cosine=gate.top1_cosine,
                n_cleared=gate.n_cleared,
                gate_reason=gate.reason,
                n_results=len(rows),
                returned_stable_ids=returned_stable_ids,
                gold_stable_ids=gold_stable_ids,
                leaked_gold_ids=leaked_gold_ids,
                draft_calls=0,
                judge_calls=0,
            )
        )
    return result


def render_e7_p1b_section(result: E7P1bResult) -> list[str]:
    """Markdown lines for the E7 P1b block (no-access replay, deterministic gate)."""
    if result.passed:
        verdict = (
            f"PASS — none of the {result.n_questions} P1b rows (P2 questions under a "
            "no-access viewer) returned a gold chunk; no gold leaked (0 draft / 0 "
            "judge)."
        )
    elif result.cleared_gate:
        verdict = (
            f"FAIL — {len(result.cleared_gate)} P1b row(s) RETURNED the gold to a "
            "no-access viewer (isolation/disclosure failure): "
            + ", ".join(
                f"`{d.question_id}` (leaked {', '.join(d.leaked_gold_ids)})"
                for d in result.cleared_gate
            )
            + "."
        )
    else:
        verdict = (
            "BLIND — no P2 rows to replay, so the access-filtered case scored "
            "nothing; a zero here is a false pass."
        )

    lines = [
        "",
        "### E7 P1b (US-057) — no-access replay of P2 (gold-exclusion check)",
        "",
        f"τ_sim={result.tau_sim} · N_min={result.n_min} · "
        f"match_threshold={result.match_threshold}. Each P1b row is a P2 "
        "(`answerable_faithful`) question replayed under a **no-access viewer** "
        "(its gold ACL-revoked). The invariant is **isolation**: no gold "
        "`stable_id` may appear in the no-access viewer's retrieved set. The "
        "answerability gate is reported but does NOT decide pass/fail — a viewer "
        "legitimately retrieving non-gold corpus chunks it is granted is not a "
        "leak; only a returned gold chunk is. No privileged second pass (US-057/058).",
        "",
        "| Question (P2 source) | Decision | top1_cosine | n_cleared | leaked gold |",
        "|---|---|---|---|---|",
    ]
    for d in result.decisions:
        cosine = "—" if d.top1_cosine is None else f"{d.top1_cosine:.4f}"
        flag = "" if d.correct else " ⚠️ leak"
        leaked = ", ".join(d.leaked_gold_ids) if d.leaked_gold_ids else "—"
        lines.append(
            f"| `{d.question_id}` | {d.decision}{flag} | {cosine} | "
            f"{d.n_cleared} | {leaked} |"
        )
    lines += ["", f"**Verdict:** {verdict}"]
    return lines


# ---------------------------------------------------------------------------
# US-058: P1b non-disclosure assertion (pinned security invariant).
#
# US-057 proves the ISOLATION invariant — no gold chunk reaches the no-access
# viewer (`E7P1bResult.cleared_gate`, identity-based). US-058 pins the orthogonal,
# customer-visible invariant on the ESCALATION PATH: when a no-access row DOES
# escalate (its retrieval is weak), the BYTES the customer sees are byte-for-byte
# identical to the P1a generic-deferral bytes, so escalating never discloses that
# restricted content exists — no `reason`, no `restricted-to`, no existence bit
# echoed to the customer. US-058 is DECOUPLED from the US-057 leak check: it scores
# only escalating rows, not draft rows (a no-access viewer drafting from
# legitimately-granted NON-gold chunks is not a restricted-content disclosure — the
# gold-leak case is owned entirely by US-057's identity check).
#
# It is a binary leak invariant (`assert leak == 0`) in the E8 pinned-`fail`
# security/correctness class (US-059) — NOT buyer-downgradable to comment/off;
# silencing it requires deleting the eval, not configuring it down — and is
# DETERMINISTIC (string/byte equality, no LLM judge), so it joins the per-PR
# tripwire and may hard-block a merge. Access-aware reason/routing surfacing is
# explicitly out of scope (a future S4 authorized-agent surface, gated on its own
# existence-non-disclosure eval).
#
# The customer output is derived through the REAL production escalate constructor
# (`escalation._escalated` → `escalation.GENERIC_DEFERRAL`), fed each row's OWN
# (per-row-DIFFERENT) gate decision + internal reason. So the assertion proves the
# customer bytes are invariant to the differing internal reason / cosine / access
# state — it would catch a future change that let the customer message reflect
# them — rather than comparing two hardcoded copies of the deferral string.
# ---------------------------------------------------------------------------


def _row_customer_output(d: P1aDecision | P1bDecision) -> bytes | None:
    """The exact bytes a customer sees for one P1a/P1b row, via the REAL path.

    `escalate` ⇒ run the production escalate constructor (`escalation._escalated`,
    the sole emitter of an escalated `DeflectionResult`) with this row's OWN gate
    decision and internal reason, and return its `customer_message` bytes — always
    `GENERIC_DEFERRAL`, regardless of the (per-row-varying) gate reason. `draft` ⇒
    `None`: the row cleared the gate and WOULD have sent a drafted answer (content
    disclosure), which is by definition not the generic deferral. The drafted bytes
    are not computed (the P1a/P1b legs make zero draft calls); `None` marks
    "discloses content, ≠ deferral" so the non-disclosure assertion flags it.
    """
    if d.decision == "draft":
        return None
    gate = RetrievalGateDecision(
        strong=d.gate_strong,
        top1_cosine=d.top1_cosine,
        n_cleared=d.n_cleared,
        reason=d.gate_reason,
    )
    result = _escalated(gate, faithfulness=None, reason=f"retrieval_{gate.reason}")
    return result.customer_message.encode("utf-8")


def _canonical_deferral_output() -> bytes:
    """The production generic-deferral bytes, via the REAL escalate constructor.

    Used as the US-058 reference only when no P1a row escalated (the P1a leg has
    already failed); otherwise a real escalated P1a row supplies the reference so
    the comparison is literally "what a P1b customer sees vs what a P1a customer
    sees".
    """
    canonical = RetrievalGateDecision(
        strong=False, top1_cosine=None, n_cleared=0, reason="weak: empty_results"
    )
    return _escalated(
        canonical, faithfulness=None, reason=f"retrieval_{canonical.reason}"
    ).customer_message.encode("utf-8")


@dataclass
class P1bLeak:
    """One P1b row whose customer-facing bytes differ from the P1a deferral — a
    non-disclosure leak (US-058). `detail` distinguishes a drafted-answer
    disclosure (the row cleared the gate) from differing escalation bytes."""

    question_id: str
    detail: Literal["drafted_answer_disclosed", "customer_bytes_differ"]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class E7P1bNonDisclosure:
    """US-058: the pinned, deterministic P1b non-disclosure assertion outcome.

    Compares every P1b row's customer-facing escalation bytes against the P1a
    generic-deferral reference and records any row that differs. A binary leak
    invariant: `passed` iff at least one P1b row was scored AND no row leaked.
    Pinned `fail` (US-059) — not buyer-downgradable. `to_dict()` lands under
    `e7_p1b_non_disclosure` in the result JSON.
    """

    p1a_reference: str  # the reference customer-output text (the generic deferral)
    p1a_reference_bytes: int  # its UTF-8 byte length
    n_p1b: int
    leaks: list[P1bLeak] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Every P1b row's customer bytes must equal the P1a deferral, and at least
        one P1b row must have been scored — a zero-leak over zero rows is
        structurally blind, not a clean pass (mirrors P1a/P1b's positive control)."""
        return self.n_p1b > 0 and not self.leaks

    def to_dict(self) -> dict[str, Any]:
        return {
            "assertion": "p1b_customer_output == p1a_customer_output (byte-for-byte)",
            "p1a_reference": self.p1a_reference,
            "p1a_reference_bytes": self.p1a_reference_bytes,
            "n_p1b": self.n_p1b,
            "n_leaks": len(self.leaks),
            "leaks": [leak.to_dict() for leak in self.leaks],
            "passed": self.passed,
        }


def assert_p1b_non_disclosure(
    p1a: E7P1aResult,
    p1b: E7P1bResult,
    *,
    p1b_output_fn: Callable[[P1bDecision], bytes | None] = _row_customer_output,
) -> E7P1bNonDisclosure:
    """Assert every ESCALATING P1b row's customer bytes equal the P1a generic
    deferral (US-058).

    The reference is the customer output of a real escalated P1a row (or the
    canonical production deferral if none escalated — the P1a leg has already
    failed), taken through the REAL escalate constructor. Each P1b row that
    ESCALATES is taken through the same path with its OWN gate decision, so the
    comparison proves the customer bytes are invariant to the per-row internal
    reason / cosine / access state. Deterministic (byte equality, no LLM) → pinned
    `fail`, per-PR (US-059).

    Scoped to the escalation path and DECOUPLED from the US-057 leak check: a P1b
    row that DRAFTS (its no-access retrieval read strong) is skipped here — a
    no-access viewer drafting from legitimately-granted NON-gold chunks is not a
    restricted-content disclosure, and the gold-leak case is owned entirely by
    US-057's identity check (`E7P1bResult.cleared_gate`). The positive control still
    requires `n_p1b > 0` (some P2 row was replayed); the conditional non-disclosure
    invariant holds vacuously when no row escalates.

    `p1b_output_fn` is the per-row customer-output deriver, injected so a test can
    simulate a regression that leaks an access-aware reason into the P1b output (the
    PRD's "inject a reason=access-denied and re-run" failure case); production uses
    the real `_row_customer_output`.
    """
    reference: bytes | None = None
    for pa in p1a.decisions:
        if pa.decision == "escalate":
            reference = _row_customer_output(pa)
            break
    if reference is None:
        reference = _canonical_deferral_output()

    leaks: list[P1bLeak] = []
    for pb in p1b.decisions:
        # US-058 guards the escalation path only; a drafting row's disclosure risk
        # (if any) is a gold leak, owned by the US-057 identity check.
        if pb.decision != "escalate":
            continue
        output = p1b_output_fn(pb)
        if output is None:
            leaks.append(P1bLeak(pb.question_id, "drafted_answer_disclosed"))
        elif output != reference:
            leaks.append(P1bLeak(pb.question_id, "customer_bytes_differ"))

    return E7P1bNonDisclosure(
        p1a_reference=reference.decode("utf-8", errors="replace"),
        p1a_reference_bytes=len(reference),
        n_p1b=len(p1b.decisions),
        leaks=leaks,
    )


def render_e7_p1b_non_disclosure_section(nd: E7P1bNonDisclosure) -> list[str]:
    """Markdown lines for the US-058 P1b non-disclosure assertion (pinned, no LLM)."""
    if nd.passed:
        verdict = (
            f"PASS — all {nd.n_p1b} P1b row(s) show the customer byte-for-byte the "
            "SAME generic deferral as P1a (no reason / restricted-to / existence "
            "bit disclosed)."
        )
    elif nd.n_p1b == 0:
        verdict = (
            "BLIND — no P1b rows scored, so the non-disclosure invariant is "
            "structurally blind; a zero-leak here is a false pass."
        )
    else:
        verdict = (
            f"FAIL — {len(nd.leaks)} P1b row(s) DISCLOSED: their customer-facing "
            "output differs from the P1a generic deferral: "
            + ", ".join(f"`{leak.question_id}` ({leak.detail})" for leak in nd.leaks)
            + "."
        )

    return [
        "",
        "### E7 P1b non-disclosure (US-058) — customer-output byte equality",
        "",
        "Pinned security invariant: the bytes a **no-access** customer sees on a P1b "
        "escalation must be byte-for-byte identical to the P1a generic deferral "
        "(`escalation.GENERIC_DEFERRAL`), so escalating never discloses that "
        "restricted content exists. Deterministic (no LLM) → per-PR hard block, "
        "pinned `fail` / un-downgradable (US-059).",
        "",
        f"Reference (P1a deferral, {nd.p1a_reference_bytes} bytes): "
        f"“{nd.p1a_reference}”",
        "",
        f"**Verdict:** {verdict}",
    ]


# ---------------------------------------------------------------------------
# US-055: consolidated operating-objective metrics.
#
# The per-leg results above each compute a population-LOCAL rate (P2's
# deflection, P3's false-resolve). US-055 consolidates them into the three
# canonical rates the operating objective is stated in — "maximize deflection
# subject to false-resolve ≤ ceiling":
#
#   * deflection     = correctly auto-resolved / ANSWERABLE      (maximize)
#   * false-resolve  = wrongly auto-resolved   / UNANSWERABLE    (the SAFETY number)
#   * false-escalate = wrongly escalated       / ANSWERABLE      (annoyance)
#
# Populations (from the E7 labels, US-051):
#   * answerable   = P2 (a faithful grounded answer exists).
#   * the ceiling-gated false-resolve number is the FAITHFULNESS-LEG rate: P3
#     (strong retrieval, no faithful answer) rows that auto-resolved, over the P3
#     population — the cases where a false-resolve can actually occur once a draft
#     clears the retrieval gate. The RETRIEVAL-leg false-resolves (P1a no-context /
#     P1b no-access rows that clear the gate) are a SEPARATE zero-tolerance
#     invariant: the P1a/P1b gate checks hard-fail unconditionally on any nonzero
#     count (a no-context draft / a gold leak is critical regardless of rate), so
#     they are surfaced in the breakdown for monitoring but EXCLUDED from the gated
#     rate — folding always-escalating true-negatives into the denominator would
#     only dilute the safety signal (worse as the gold set grows: each P2 adds a
#     P1b row).
#
# Each rate is emitted with its explicit numerator/denominator AND a
# per-population breakdown so a regression is attributable to a single leg and the
# false-resolve number is verifiable against the buyer's ceiling (US-059) — never
# collapsed into an opaque "accuracy" score. `compute_e7_metrics` only REPORTS the
# rates; `assert_false_resolve_ceiling` (below, US-059) is where the false-resolve
# ≤ ceiling check is ENFORCED as a pinned safety invariant in the runner's exit
# code, alongside the deterministic P1a/P1b gate + non-disclosure invariants.
# ---------------------------------------------------------------------------


@dataclass
class PopulationContribution:
    """One population's (numerator, denominator) contribution to a consolidated
    rate, so a change in the rate is attributable to P1a/P1b/P2/P3 (US-055).

    `counts_toward_rate` is `False` for a contribution that is SURFACED for
    monitoring but excluded from the headline rate — the retrieval-leg P1a/P1b
    false-resolves, which are a zero-tolerance invariant the P1a/P1b gate checks
    hard-fail unconditionally (any nonzero count is critical regardless of rate),
    not a rate that may dilute the ceiling-gated faithfulness-leg number (US-059)."""

    population: str  # "P1a" / "P1b" / "P2" / "P3"
    numerator: int
    denominator: int
    counts_toward_rate: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "population": self.population,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "counts_toward_rate": self.counts_toward_rate,
        }


@dataclass
class ConsolidatedRate:
    """A consolidated E7 operating-objective rate (US-055).

    The headline `numerator` / `denominator` are the sum of the RATE-BEARING
    per-population contributions (`counts_toward_rate`), and are exposed explicitly
    — never a bare float — so US-059's ceiling gate can verify
    `numerator / denominator <= ceiling` and a regression is attributable to a
    single leg. A `counts_toward_rate=False` contribution (the retrieval-leg P1a/P1b
    false-resolves) is carried in `by_population` for monitoring but EXCLUDED from
    the headline rate: it is a zero-tolerance invariant gated unconditionally
    elsewhere, never a rate that may dilute the ceiling-governed faithfulness-leg
    number. `safety` flags the false-resolve rate, the one pinned-invariant number;
    deflection and false-escalate are tunable quality metrics.
    """

    name: Literal["deflection", "false_resolve", "false_escalate"]
    safety: bool
    by_population: list[PopulationContribution] = field(default_factory=list)

    @property
    def numerator(self) -> int:
        return sum(c.numerator for c in self.by_population if c.counts_toward_rate)

    @property
    def denominator(self) -> int:
        return sum(c.denominator for c in self.by_population if c.counts_toward_rate)

    @property
    def rate(self) -> float | None:
        """`None` over an empty rate-bearing population — a rate over zero questions
        is structurally blind, not 0.0 (mirrors the per-leg `*_rate` guards). A
        consumer must treat `None` as "not measured", never as a passing 0."""
        if self.denominator == 0:
            return None
        return self.numerator / self.denominator

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "rate": self.rate,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "safety": self.safety,
            "by_population": [c.to_dict() for c in self.by_population],
        }


@dataclass
class E7Metrics:
    """The three consolidated E7 operating-objective rates (US-055).

    `to_dict()` is what lands in the result JSON under `e7_metrics`. The operating
    objective — *maximize deflection subject to false-resolve ≤ ceiling* — is
    measurable from these three rates; the false-resolve rate is the pinned safety
    number US-059 gates against the buyer's ceiling.
    """

    deflection: ConsolidatedRate
    false_resolve: ConsolidatedRate
    false_escalate: ConsolidatedRate

    def to_dict(self) -> dict[str, Any]:
        return {
            "deflection": self.deflection.to_dict(),
            "false_resolve": self.false_resolve.to_dict(),
            "false_escalate": self.false_escalate.to_dict(),
        }


def compute_e7_metrics(
    p1a: E7P1aResult,
    p2: E7P2Result | None,
    p3: E7P3Result | None,
    p1b: E7P1bResult | None = None,
) -> E7Metrics:
    """Consolidate the per-leg outcomes into the three operating-objective rates
    (US-055): deflection, false-resolve, false-escalate.

    Numerators / denominators:

      * **deflection** = correctly auto-resolved P2 / answerable. Answerable = the
        P2 population (a faithful grounded answer exists); the numerator is the P2
        rows that auto-resolved.
      * **false-resolve** = the SAFETY number, the FAITHFULNESS-LEG false-resolve
        rate: P3 rows (should-escalate, strong retrieval) that *auto-resolved*, over
        the P3 population — the cases where a false-resolve can actually occur once a
        draft clears the retrieval gate. This is the rate US-059 gates against the
        buyer's ceiling. The RETRIEVAL-leg false-resolves — P1a (no context) / P1b
        (no-access replay of P2, US-057) rows that *cleared the gate* — are carried
        in the breakdown for monitoring (`counts_toward_rate=False`) but EXCLUDED
        from this rate: each is a zero-tolerance invariant the P1a/P1b gate checks
        hard-fail unconditionally (any nonzero count is a no-context draft / a gold
        leak, critical regardless of rate), so folding always-escalating
        true-negatives into the denominator would only dilute the ceiling-gated
        number — worse as the gold set grows (each P2 adds a P1b row).
      * **false-escalate** = wrongly escalated P2 / answerable — the annoyance
        number (the complement of deflection over the P2 population).

    P2 / P3 / P1b are opt-in legs (`--include-p2` / `--include-p3` /
    `--include-p1b`); when a leg was not run its population contributes nothing (and
    a rate over an all-empty rate-bearing set is `None` — surfaced as blind rather
    than a false 0.0). Because the gated false-resolve rate is the P3 leg, a per-PR
    run (P1a/P1b only, no P3) reports it as `None`/not-measured — the ceiling gate
    is inert there and the retrieval-leg invariants own per-PR safety. A P3 row that
    escalated before the faithfulness gate (`mislabeled`) is a *safe* outcome — it
    stays in the P3 (faithfulness-leg) denominator but is NOT counted toward the
    false-resolve numerator. The P3 false-resolve denominator is therefore the FULL
    presented P3 population (`p3.n_questions`, mislabeled rows included), NOT the
    exercised-only subset: that full-population denominator is the operating-metric
    meaning and keeps this sum-based consolidated rate from mixing per-population
    denominators (the rejected "exercised-only" misread, issue #26). The dilution a
    heavily-mislabeled P3 leg would cause is caught by the separate mislabel-ratio
    guard (issue #26), never by shrinking this denominator. This function only
    computes the rates; US-059 enforces the ceiling.
    """
    deflection = ConsolidatedRate(name="deflection", safety=False)
    false_escalate = ConsolidatedRate(name="false_escalate", safety=False)
    if p2 is not None:
        deflection.by_population.append(
            PopulationContribution("P2", len(p2.auto_resolved), p2.n_questions)
        )
        false_escalate.by_population.append(
            PopulationContribution("P2", len(p2.false_escalates), p2.n_questions)
        )

    # false-resolve is the safety metric (the pinned invariant US-059 gates). The
    # GATED rate is the faithfulness leg (P3 rows that auto-resolved, over the P3
    # population) — the population where a false-resolve can occur once a draft
    # clears the retrieval gate. The retrieval-leg P1a/P1b false-resolves are carried
    # for monitoring (counts_toward_rate=False) but EXCLUDED from the rate: each is a
    # zero-tolerance invariant the P1a/P1b gate checks hard-fail unconditionally, so
    # folding always-escalating true-negatives into the denominator would only dilute
    # the ceiling-gated number.
    false_resolve = ConsolidatedRate(name="false_resolve", safety=True)
    false_resolve.by_population.append(
        PopulationContribution(
            "P1a", len(p1a.cleared_gate), p1a.n_questions, counts_toward_rate=False
        )
    )
    if p1b is not None:
        false_resolve.by_population.append(
            PopulationContribution(
                "P1b", len(p1b.cleared_gate), p1b.n_questions, counts_toward_rate=False
            )
        )
    if p3 is not None:
        false_resolve.by_population.append(
            PopulationContribution(
                "P3", len(p3.false_resolves), p3.n_questions, counts_toward_rate=True
            )
        )

    return E7Metrics(
        deflection=deflection,
        false_resolve=false_resolve,
        false_escalate=false_escalate,
    )


def render_e7_metrics_section(metrics: E7Metrics) -> list[str]:
    """Markdown lines for the consolidated E7 operating-objective metrics (US-055).

    One row per rate, each showing the rate, its explicit numerator/denominator,
    its class (the safety false-resolve number vs the tunable quality numbers), and
    the per-population breakdown — so the false-resolve number is auditable against
    the buyer's ceiling (US-059) and never collapsed into a single accuracy score.
    """
    lines = [
        "",
        "### E7 consolidated metrics (US-055) — deflection / false-resolve / "
        "false-escalate",
        "",
        "Operating objective: **maximize deflection subject to false-resolve ≤ "
        "ceiling**. Answerable = P2; the gated false-resolve rate is the "
        "faithfulness leg (P3 rows that auto-resolved, over P3). The retrieval-leg "
        "P1a/P1b false-resolves are shown `[monitor-only]` — a zero-tolerance "
        "invariant the P1a/P1b gates hard-fail unconditionally, NOT diluting the "
        "ceiling-gated rate. Each rate carries its numerator/denominator + "
        "per-population breakdown so the false-resolve number is verifiable against "
        "the buyer's ceiling (US-059), not folded into an opaque accuracy score. "
        "Reported here; the ceiling is enforced in US-059.",
        "",
        "| Metric | Rate | n/d | Class | By population |",
        "|---|---|---|---|---|",
    ]
    for rate in (metrics.deflection, metrics.false_resolve, metrics.false_escalate):
        rate_str = "— (blind)" if rate.rate is None else f"{rate.rate:.0%}"
        cls = "🔒 safety (pinned)" if rate.safety else "tunable quality"
        breakdown = (
            ", ".join(
                f"{c.population} {c.numerator}/{c.denominator}"
                + ("" if c.counts_toward_rate else " [monitor-only]")
                for c in rate.by_population
            )
            or "—"
        )
        lines.append(
            f"| {rate.name} | {rate_str} | {rate.numerator}/{rate.denominator} | "
            f"{cls} | {breakdown} |"
        )
    return lines


# ---------------------------------------------------------------------------
# US-059: the false-resolve ceiling gate — the pinned SAFETY invariant.
#
# `compute_e7_metrics` (US-055) REPORTS the consolidated false-resolve rate with
# its explicit numerator/denominator; this is where US-059 ENFORCES it. The
# buyer's `get_false_resolve_ceiling()` (US-050) is the one risk number; a
# measured false-resolve rate above it is a safety breach that fails the run —
# the same pinned-`fail`, non-downgradable shape as the P1a/P1b gate invariants
# and the US-058 non-disclosure assertion. It is never softened to a comment,
# unlike the tunable deflection / false-escalate quality metrics the E8 gate
# governs (US-059 AC3).
#
# The gated rate is the faithfulness leg (P3), so the per-PR tripwire (the
# deterministic P1a/P1b legs only, no P3) reports it as `None`/not-measured — this
# ceiling gate is INERT there, and the retrieval-leg P1a/P1b false-resolves are
# independently pinned by the P1a/P1b gate checks (a zero-tolerance invariant). The
# weekly sweep adds the LLM-judged P3 faithfulness leg, so the faithfulness-leg
# false-resolve is what this ceiling actually catches — at an accepted up-to-a-week
# detection latency (docs/evals.md "E7 CI placement"; F3/P5).
#
# A `None` rate (no unanswerable rows scored) is "not measured", NOT a breach, so
# this gate fires only on a MEASURED rate that exceeds the ceiling. The
# structurally-blind failure is owned elsewhere: a per-PR run legitimately has no P3
# leg, while a requested P3 leg that never EXERCISES the faithfulness gate is
# hard-failed by the P3 positive-control guard in `e7_pinned_invariants_failed`
# (mirroring the P1a/P1b blindness guards). That guard covers both disarm paths — an
# empty P3 leg (rate `None`) AND an entirely-mislabeled one (every row escalates
# before the gate, so the rate reads a vacuous measured 0%) — so neither can
# silently disarm the ceiling.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FalseResolveCeilingVerdict:
    """US-059: the consolidated false-resolve rate gated against the buyer's
    ceiling — a pinned safety invariant (`rate <= ceiling`).

    `breached` is the verdict: `True` iff the rate is MEASURED (denominator > 0)
    and STRICTLY exceeds `ceiling` — equality is feasible, matching the knee's
    `false_resolve <= ceiling` feasibility in `_select_knee`. `passed` is its
    negation, the shape the runner's exit-code block folds in. The explicit
    numerator/denominator are echoed off `E7Metrics.false_resolve` so the verdict
    is `numerator / denominator <= ceiling`, never a bare-float comparison.
    """

    ceiling: float
    numerator: int
    denominator: int
    rate: float | None
    breached: bool

    @property
    def passed(self) -> bool:
        return not self.breached

    def to_dict(self) -> dict[str, Any]:
        return {
            "ceiling": self.ceiling,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "rate": self.rate,
            "breached": self.breached,
            "passed": self.passed,
        }


def assert_false_resolve_ceiling(
    metrics: E7Metrics, ceiling: float
) -> FalseResolveCeilingVerdict:
    """US-059: enforce the buyer's false-resolve ceiling on the consolidated
    metric (US-055) as a pinned safety invariant.

    Reads the explicit numerator/denominator off `metrics.false_resolve` (never a
    bare float) so the verdict is `numerator / denominator <= ceiling` and a breach
    is attributable to the per-population breakdown the metric already carries. That
    rate is the FAITHFULNESS-LEG (P3) false-resolve — the retrieval-leg P1a/P1b
    false-resolves are a zero-tolerance invariant the P1a/P1b gate checks hard-fail
    unconditionally, not folded into this rate. A `None` rate (no P3 faithfulness-leg
    rows scored — e.g. a per-PR P1a/P1b-only run) is NOT a breach; this gate fires
    only on a measured rate that strictly exceeds the ceiling. The blind case is
    owned elsewhere: the P3 positive-control guard in `e7_pinned_invariants_failed`
    hard-fails a requested P3 leg that never exercises the faithfulness gate —
    whether empty (rate `None`) or entirely mislabeled (a vacuous measured 0%) — so
    neither an unmeasured nor a vacuous rate here can silently disarm the ceiling.
    """
    fr = metrics.false_resolve
    rate = fr.rate
    breached = rate is not None and rate > ceiling
    return FalseResolveCeilingVerdict(
        ceiling=ceiling,
        numerator=fr.numerator,
        denominator=fr.denominator,
        rate=rate,
        breached=breached,
    )


def render_e7_false_resolve_ceiling_section(
    verdict: FalseResolveCeilingVerdict,
) -> list[str]:
    """Markdown lines for the US-059 false-resolve ceiling gate (pinned safety)."""
    if verdict.rate is None:
        rate_str = "— (blind: no P3 faithfulness-leg rows scored)"
        status = "not measured"
    else:
        rate_str = f"{verdict.rate:.0%} ({verdict.numerator}/{verdict.denominator})"
        status = "❌ BREACH (fails the run)" if verdict.breached else "✅ within ceiling"
    return [
        "",
        "### E7 false-resolve ceiling gate (US-059) — pinned safety invariant",
        "",
        f"- Buyer ceiling: **{verdict.ceiling:.0%}** "
        "(`ESCALATION_FALSE_RESOLVE_CEILING`, US-050)",
        f"- Measured false-resolve (faithfulness leg, P3): **{rate_str}**",
        f"- Verdict: **{status}**",
        "",
        "A measured faithfulness-leg false-resolve rate above the ceiling fails the "
        "run (pinned `fail`, never downgraded to a comment — unlike the tunable "
        "deflection/false-escalate metrics). The LLM-judged P3 leg is scored only in "
        "the weekly sweep, so per-PR (P1a/P1b only) this rate is not measured and the "
        "gate is inert (the retrieval-leg P1a/P1b false-resolves are pinned "
        "separately); the faithfulness-leg false-resolve carries an accepted "
        "up-to-a-week detection latency.",
    ]


# ---------------------------------------------------------------------------
# US-056: knob sweep, deflection-vs-false-resolve curve, knee.
#
# The sweep grids over the three escalation knobs — τ_sim, N_min, and the offline
# faithfulness floor (the E7 1-5 judge min, NOT the runtime [0,1]
# faithfulness_cutoff) — records the consolidated (deflection, false-resolve,
# false-escalate) at each grid point, and selects the **knee**: the point
# MAXIMIZING deflection SUBJECT TO false-resolve ≤ the buyer's ceiling
# (`get_false_resolve_ceiling`, US-050). It is the ceiling-constrained objective,
# never "maximize accuracy" — the false-resolve safety number is a hard
# constraint, not a term to be averaged away.
#
# Cost: the draft text and the offline judge's 1-5 faithfulness score for a
# question are INDEPENDENT of the knobs (the knobs are pure post-hoc thresholds —
# τ_sim/N threshold the already-retrieved cosines, the faithfulness floor
# thresholds the already-scored draft). So the sweep retrieves / drafts / judges
# each question EXACTLY ONCE (memoized) and re-decides every grid point by
# RE-RUNNING the real US-052/053/054 legs + the US-055 consolidation over those
# cached raw materials — no logic is duplicated, and the whole grid costs the same
# LLM as a single operating point. Reusing one draft/judge sample per question
# (rather than re-sampling per point) also keeps the curve a function of the
# KNOBS, not of LLM sampling variance.
#
# LLM-judged, so structurally a scheduled (weekly) artifact, never a per-PR block
# (US-059) — the deterministic per-PR tripwire is the P1a leg alone.
# ---------------------------------------------------------------------------


def _memoize_retrieve(retrieve: Retrieve) -> Retrieve:
    """Wrap `retrieve` so each distinct question retrieves at most once."""
    cache: dict[str, list[SearchDocumentsResult]] = {}

    async def cached(question: str) -> list[SearchDocumentsResult]:
        if question not in cache:
            cache[question] = await retrieve(question)
        return cache[question]

    return cached


def _memoize_draft(draft: Draft) -> Draft:
    """Wrap `draft` so each distinct question drafts at most once.

    Chunks are fixed per question (retrieval is memoized), so keying on the
    message alone is sufficient — and reusing ONE draft sample per question keeps
    the swept curve a function of the knobs, not of LLM sampling variance.
    """
    cache: dict[str, str] = {}

    async def cached(message: str, chunks: list[SearchDocumentsResult]) -> str:
        if message not in cache:
            cache[message] = await draft(message, chunks)
        return cache[message]

    return cached


def _memoize_judge(judge: Judge) -> Judge:
    """Wrap `judge` so each distinct question is scored at most once.

    Reference / chunks / draft are all fixed per question (the draft is memoized),
    so the question text is a sufficient cache key; the cached 1-5 score is then
    re-thresholded against each grid point's faithfulness floor with no new call.
    """
    cache: dict[str, dict[str, int]] = {}

    async def cached(
        question: str,
        reference: str,
        chunks: list[SearchDocumentsResult],
        draft_text: str,
    ) -> dict[str, int]:
        if question not in cache:
            cache[question] = await judge(question, reference, chunks, draft_text)
        return cache[question]

    return cached


@dataclass
class SweepPoint:
    """One grid point's knobs + the consolidated rates it produces (US-056).

    `index` is the point's position in the deterministic grid enumeration (a
    stable tie-breaker for knee selection). `feasible` is `false_resolve ≤ ceiling`
    — the hard safety constraint the knee must satisfy. The `deflection` /
    `false_resolve` / `false_escalate` properties are read straight off the
    embedded `E7Metrics` (US-055), so a swept rate is computed by the exact same
    consolidation code a single-point run uses.
    """

    index: int
    tau_sim: float
    n_min: int
    faithfulness_judge_min: int
    metrics: E7Metrics
    feasible: bool

    @property
    def deflection(self) -> float | None:
        return self.metrics.deflection.rate

    @property
    def false_resolve(self) -> float | None:
        return self.metrics.false_resolve.rate

    @property
    def false_escalate(self) -> float | None:
        return self.metrics.false_escalate.rate

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "tau_sim": self.tau_sim,
            "n_min": self.n_min,
            "faithfulness_judge_min": self.faithfulness_judge_min,
            "deflection": self.deflection,
            "false_resolve": self.false_resolve,
            "false_escalate": self.false_escalate,
            "feasible": self.feasible,
            "metrics": self.metrics.to_dict(),
        }


def _select_knee(
    points: list[SweepPoint], ceiling: float
) -> tuple[int | None, Literal["ok", "no_point_under_ceiling", "deflection_blind"]]:
    """Pick the knee: max deflection SUBJECT TO false-resolve ≤ ceiling (US-056).

    Returns `(index, "ok")` for the chosen point. If NO grid point achieves
    false-resolve ≤ ceiling, returns `(None, "no_point_under_ceiling")` — reported
    explicitly, never silently downgraded to the least-bad point. If points
    satisfy the ceiling but none has a defined deflection (the P2 answerable
    population was empty), returns `(None, "deflection_blind")`. Ties on deflection
    break toward the LOWER false-resolve (more safety margin), then lower
    false-escalate, then the lower grid index (determinism).
    """
    under = [
        p for p in points
        if p.false_resolve is not None and p.false_resolve <= ceiling
    ]
    if not under:
        return None, "no_point_under_ceiling"
    eligible = [p for p in under if p.deflection is not None]
    if not eligible:
        return None, "deflection_blind"
    knee = min(
        eligible,
        key=lambda p: (
            -(p.deflection if p.deflection is not None else 0.0),
            p.false_resolve if p.false_resolve is not None else 1.0,
            p.false_escalate if p.false_escalate is not None else 1.0,
            p.index,
        ),
    )
    return knee.index, "ok"


@dataclass
class E7Sweep:
    """Outcome of the E7 knob sweep (US-056). `to_dict()` lands under `e7_sweep`.

    `knee_index` is `None` when no point satisfies the ceiling (`knee_reason`
    distinguishes `no_point_under_ceiling` from `deflection_blind`); otherwise it
    indexes the deflection-maximizing feasible point. `faithfulness_cutoff` is the
    runtime [0,1] cutoff carried in each grid `EscalationConfig` for completeness;
    the offline legs ignore it (they threshold the 1-5 judge score against the
    swept `faithfulness_judge_min`).
    """

    ceiling: float
    match_threshold: float
    judge_model: str
    faithfulness_cutoff: float
    n_questions: int
    points: list[SweepPoint]
    knee_index: int | None
    knee_reason: Literal["ok", "no_point_under_ceiling", "deflection_blind"]

    @property
    def knee(self) -> SweepPoint | None:
        return None if self.knee_index is None else self.points[self.knee_index]

    @property
    def feasible_points(self) -> list[SweepPoint]:
        """Grid points satisfying the safety constraint (false-resolve ≤ ceiling)."""
        return [p for p in self.points if p.feasible]

    @property
    def curve(self) -> list[dict[str, Any]]:
        """The deflection-vs-false-resolve curve as plottable points, sorted by
        false-resolve ascending then deflection descending — the operating curve
        the knee sits on. Points whose deflection or false-resolve is blind (None)
        are omitted (they are not on the curve)."""
        plottable = [
            p for p in self.points
            if p.false_resolve is not None and p.deflection is not None
        ]
        plottable.sort(
            key=lambda p: (
                p.false_resolve if p.false_resolve is not None else 1.0,
                -(p.deflection if p.deflection is not None else 0.0),
            )
        )
        return [
            {
                "false_resolve": p.false_resolve,
                "deflection": p.deflection,
                "false_escalate": p.false_escalate,
                "tau_sim": p.tau_sim,
                "n_min": p.n_min,
                "faithfulness_judge_min": p.faithfulness_judge_min,
                "feasible": p.feasible,
                "index": p.index,
            }
            for p in plottable
        ]

    def recommended_config(self) -> dict[str, Any] | None:
        """The knee's knobs as recommended US-050 defaults, or `None` if no knee.

        τ_sim and N_min promote DIRECTLY to the runtime gate knobs
        (`ESCALATION_TAU_SIM` / `ESCALATION_N_MIN`) — same machinery, same scale.
        The offline 1-5 faithfulness floor is reported as guidance only: the
        runtime `ESCALATION_FAITHFULNESS_CUTOFF` lives on a DIFFERENT [0,1] scale
        (US-048/050) and is tuned separately, so it is not promoted verbatim.
        """
        k = self.knee
        if k is None:
            return None
        return {
            "ESCALATION_TAU_SIM": k.tau_sim,
            "ESCALATION_N_MIN": k.n_min,
            "faithfulness_judge_min_offline_1_5": k.faithfulness_judge_min,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "ceiling": self.ceiling,
            "match_threshold": self.match_threshold,
            "judge_model": self.judge_model,
            "faithfulness_cutoff": self.faithfulness_cutoff,
            "n_questions": self.n_questions,
            "knee_reason": self.knee_reason,
            "knee": self.knee.to_dict() if self.knee is not None else None,
            "recommended_config": self.recommended_config(),
            "curve": self.curve,
            "points": [p.to_dict() for p in self.points],
        }


async def run_e7_sweep(
    *,
    questions: list[dict[str, Any]],
    retrieve: Retrieve,
    draft: Draft,
    judge: Judge,
    tau_sims: list[float],
    n_mins: list[int],
    faithfulness_mins: list[int],
    match_threshold: float,
    faithfulness_cutoff: float,
    ceiling: float,
    judge_model: str,
) -> E7Sweep:
    """Sweep τ_sim × N_min × faithfulness-floor and select the knee (US-056).

    For each point in the Cartesian product `tau_sims × n_mins × faithfulness_mins`
    (enumerated τ_sim-outer → N_min → faithfulness-floor, a stable order), this
    re-runs the REAL US-052/053/054 legs (`run_e7_p1a` / `run_e7_p2` /
    `run_e7_p3`) and the US-055 consolidation (`compute_e7_metrics`) — so a swept
    rate is computed by the exact same code a single-point run uses, never a
    re-implementation. `retrieve` / `draft` / `judge` are memoized per question, so
    the entire grid retrieves / drafts / judges each question at most once and
    costs the same LLM as one operating point.

    Selects the knee = the feasible (false-resolve ≤ `ceiling`) point that
    maximizes deflection; if none is feasible it reports that explicitly via
    `knee_reason` rather than picking the least-bad point. The swept false-resolve
    is the gated faithfulness-leg (P3) rate — the SAME corrected denominator the
    enforced ceiling gate (`assert_false_resolve_ceiling`) uses, so a chosen knee
    satisfies the same gate it will be measured against. LLM-judged → a
    scheduled/weekly artifact, never a per-PR block (US-059).
    """
    mret = _memoize_retrieve(retrieve)
    mdraft = _memoize_draft(draft)
    mjudge = _memoize_judge(judge)

    grid = [
        (tau, n_min, faith_min)
        for tau in tau_sims
        for n_min in n_mins
        for faith_min in faithfulness_mins
    ]

    points: list[SweepPoint] = []
    for index, (tau, n_min, faith_min) in enumerate(grid):
        config = EscalationConfig(
            tau_sim=tau, n_min=n_min, faithfulness_cutoff=faithfulness_cutoff
        )
        p1a = await run_e7_p1a(
            questions=questions,
            retrieve=mret,
            config=config,
            match_threshold=match_threshold,
        )
        p2 = await run_e7_p2(
            questions=questions,
            retrieve=mret,
            draft=mdraft,
            judge=mjudge,
            config=config,
            match_threshold=match_threshold,
            judge_model=judge_model,
            faithfulness_judge_min=faith_min,
        )
        p3 = await run_e7_p3(
            questions=questions,
            retrieve=mret,
            draft=mdraft,
            judge=mjudge,
            config=config,
            match_threshold=match_threshold,
            judge_model=judge_model,
            faithfulness_judge_min=faith_min,
        )
        metrics = compute_e7_metrics(p1a, p2, p3)
        fr = metrics.false_resolve.rate
        points.append(
            SweepPoint(
                index=index,
                tau_sim=tau,
                n_min=n_min,
                faithfulness_judge_min=faith_min,
                metrics=metrics,
                feasible=(fr is not None and fr <= ceiling),
            )
        )

    knee_index, knee_reason = _select_knee(points, ceiling)
    return E7Sweep(
        ceiling=ceiling,
        match_threshold=match_threshold,
        judge_model=judge_model,
        faithfulness_cutoff=faithfulness_cutoff,
        n_questions=len(questions),
        points=points,
        knee_index=knee_index,
        knee_reason=knee_reason,
    )


def render_e7_sweep_section(sweep: E7Sweep) -> list[str]:
    """Markdown lines for the E7 knob-sweep block: the curve table + the knee."""
    lines = [
        "",
        "### E7 knob sweep (US-056) — deflection-vs-false-resolve curve + knee",
        "",
        f"Grid of {len(sweep.points)} operating point(s) over τ_sim × N_min × "
        f"faithfulness-floor (offline 1-5 judge `{sweep.judge_model}`, "
        f"match_threshold={sweep.match_threshold}). Objective: **maximize "
        f"deflection subject to false-resolve ≤ {sweep.ceiling:.0%}** (the buyer's "
        "ceiling) — not maximize accuracy. LLM-judged → scheduled/weekly, never a "
        "per-PR block (US-059).",
        "",
        "| τ_sim | N_min | faith≥ | deflection | false-resolve | false-escalate "
        "| ≤ ceiling |",
        "|---|---|---|---|---|---|---|",
    ]
    knee = sweep.knee
    for p in sweep.points:
        defl = "—" if p.deflection is None else f"{p.deflection:.0%}"
        fr = "—" if p.false_resolve is None else f"{p.false_resolve:.0%}"
        fe = "—" if p.false_escalate is None else f"{p.false_escalate:.0%}"
        feas = "✅" if p.feasible else "✗"
        mark = " ⭐ knee" if knee is not None and p.index == knee.index else ""
        lines.append(
            f"| {p.tau_sim} | {p.n_min} | {p.faithfulness_judge_min}/5 | {defl} | "
            f"{fr} | {fe} | {feas}{mark} |"
        )

    lines.append("")
    if sweep.knee_reason == "ok" and knee is not None:
        defl = "—" if knee.deflection is None else f"{knee.deflection:.0%}"
        fr = "—" if knee.false_resolve is None else f"{knee.false_resolve:.0%}"
        lines.append(
            f"**Knee:** τ_sim={knee.tau_sim}, N_min={knee.n_min}, "
            f"faithfulness≥{knee.faithfulness_judge_min}/5 → deflection {defl}, "
            f"false-resolve {fr} (≤ ceiling {sweep.ceiling:.0%})."
        )
        lines.append(
            f"**Recommended US-050 defaults:** `ESCALATION_TAU_SIM={knee.tau_sim}`, "
            f"`ESCALATION_N_MIN={knee.n_min}`. The offline faithfulness floor "
            f"({knee.faithfulness_judge_min}/5) is guidance only — the runtime "
            "`ESCALATION_FAITHFULNESS_CUTOFF` is a different [0,1] scale (US-048/050), "
            "tuned separately."
        )
    elif sweep.knee_reason == "no_point_under_ceiling":
        lines.append(
            f"**No knee:** no operating point achieves false-resolve ≤ "
            f"{sweep.ceiling:.0%}. Reported explicitly (the ceiling is a hard "
            "constraint) rather than picking the least-bad point — tighten the gate "
            "(raise τ_sim / N_min) or improve retrieval grounding, then re-sweep."
        )
    else:  # deflection_blind
        lines.append(
            "**No knee:** point(s) satisfy the ceiling but the P2 answerable "
            "population is empty, so deflection is structurally blind — the sweep "
            "cannot recommend an operating point. Add P2 rows and re-sweep."
        )
    return lines


def e7_pinned_invariants_failed(
    *,
    p1a_result: E7P1aResult,
    p1b_result: E7P1bResult | None,
    non_disclosure: E7P1bNonDisclosure | None,
    p3_result: E7P3Result | None,
    ceiling_verdict: FalseResolveCeilingVerdict,
    p3_mislabel_ratio_max: float = DEFAULT_P3_MISLABEL_RATIO_MAX,
) -> bool:
    """US-059: the E7 runner's pinned exit-code decision — pure over the scored
    legs so the per-PR and weekly fail conditions are unit-testable (amain only
    wires the live legs into it). Returns True iff a pinned safety invariant fired;
    each fired invariant logs at ERROR first, so the caller can write the JSON
    snapshot before exiting and the cause is preserved.

    The pinned invariants (none ever softened to a comment, unlike the tunable
    deflection/false-escalate metrics the E8 gate governs):

      * a P1a no-context row that CLEARS the retrieval gate (a retrieval-leg
        false-resolve), or a P1b no-access-replay row that RETURNED a gold chunk to
        the no-access viewer (an isolation/disclosure failure, identity-measured —
        independent of the answerability gate); zero-tolerance, hard-fails
        unconditionally regardless of rate;
      * a P1b non-disclosure byte mismatch (a customer output that differs from the
        P1a generic deferral, disclosing restricted content exists);
      * a structurally-blind positive control on ANY requested leg — fail closed,
        because a blind eval cannot prove the invariant. For P1a/P1b/non-disclosure
        this means 0 rows scored. For the P3 faithfulness leg the bar is stricter:
        the false-resolve ceiling is fed SOLELY by P3, so it must verify the gate was
        actually EXERCISED, not merely that rows exist. `E7P3Result.passed` requires
        ≥1 row to clear retrieval, draft, and reach a faithfulness verdict, closing
        BOTH disarm paths — an empty P3 leg (rate `None`, "not measured") AND a
        non-empty but entirely-mislabeled P3 leg (every row escalates at the
        retrieval/draft leg, so the rate reads a vacuous measured 0% that never
        breaches). Either way the gold drifted out from under the ceiling;
      * a P3 mislabel-RATIO breach (issue #26): the positive control above closes
        only the TOTAL-dilution case (zero exercised); a leg that is HEAVILY but not
        entirely mislabeled still exercises ≥1 row, passes the positive control, yet
        measures the false-resolve ceiling over a shrunken sample that can mask a bad
        faithfulness gate as the P3 gold grows. This guard fails the run when the
        mislabeled fraction over the full presented population exceeds
        `p3_mislabel_ratio_max`, catching that partial dilution;
      * a MEASURED faithfulness-leg (P3) false-resolve rate above the buyer's ceiling.

    A per-PR run carries no P3 leg (p3_result is None), so the P3 guards never trip
    it there — only the deterministic P1a/P1b gate + non-disclosure invariants gate a
    merge. The ceiling gate is likewise inert per-PR (an unmeasured rate is not a
    breach); it has teeth once the weekly P3 leg feeds the rate.
    """
    failed = False

    # Pinned fail: the deterministic gate-only legs. A P1a row that drafts/
    # auto-resolves is a retrieval-leg false-resolve; a P1b row that clears the gate
    # leaked the gold to a no-access viewer (a false-resolve AND a disclosure risk).
    if not p1a_result.passed:
        if p1a_result.cleared_gate:
            log.error(
                "E7 P1a FALSE-RESOLVE RISK — %d no-context row(s) cleared the "
                "retrieval gate (would draft):",
                len(p1a_result.cleared_gate),
            )
            for d in p1a_result.cleared_gate:
                log.error(
                    "  %s top1_cosine=%s n_cleared=%d (%s)",
                    d.question_id,
                    "None" if d.top1_cosine is None else f"{d.top1_cosine:.4f}",
                    d.n_cleared,
                    d.gate_reason,
                )
        else:
            log.error(
                "E7 P1a scored 0 questions — the eval is structurally blind. "
                "Failing the run."
            )
        failed = True

    if p1b_result is not None and not p1b_result.passed:
        if p1b_result.cleared_gate:
            log.error(
                "E7 P1b LEAK — %d no-access-replay row(s) RETURNED a gold chunk to "
                "the no-access viewer (isolation/disclosure failure; identity-"
                "measured, independent of the answerability gate):",
                len(p1b_result.cleared_gate),
            )
            for leak in p1b_result.cleared_gate:
                log.error(
                    "  %s leaked_gold=%s top1_cosine=%s n_cleared=%d (%s)",
                    leak.question_id,
                    ",".join(leak.leaked_gold_ids) or "?",
                    "None" if leak.top1_cosine is None else f"{leak.top1_cosine:.4f}",
                    leak.n_cleared,
                    leak.gate_reason,
                )
        else:
            log.error(
                "E7 P1b scored 0 questions — no P2 rows to replay, so the "
                "access-filtered case is structurally blind. Failing the run."
            )
        failed = True

    # US-058: the P1b non-disclosure byte-equality assertion is a pinned leak
    # invariant — a P1b customer output that differs from the P1a generic deferral
    # discloses that restricted content exists.
    if non_disclosure is not None and not non_disclosure.passed:
        if non_disclosure.leaks:
            log.error(
                "E7 P1b NON-DISCLOSURE LEAK — %d P1b row(s) show the customer a "
                "DIFFERENT output than the P1a generic deferral (restricted-content "
                "existence disclosed):",
                len(non_disclosure.leaks),
            )
            for nd_leak in non_disclosure.leaks:
                log.error("  %s (%s)", nd_leak.question_id, nd_leak.detail)
        else:
            log.error(
                "E7 P1b non-disclosure assertion scored 0 rows — structurally "
                "blind. Failing the run."
            )
        failed = True

    # US-059: the P3 positive control for the false-resolve ceiling, which is now fed
    # SOLELY by P3. A P3 leg that never EXERCISES the faithfulness gate leaves the
    # ceiling unmeasured/vacuous → the gate is inert → the safety invariant goes
    # silently unmeasured. `E7P3Result.passed` requires ≥1 exercised row, so this one
    # guard closes BOTH disarm paths: an empty P3 leg (rate `None`) AND a non-empty
    # but entirely-mislabeled P3 leg (every row escalated at the retrieval/draft leg,
    # so the rate reads a vacuous measured 0% that never breaches). Fail closed,
    # mirroring the P1a/P1b/non-disclosure blindness guards. p3_result is None on a
    # per-PR run (no P3 leg requested), which never trips this.
    if p3_result is not None and not p3_result.passed:
        if p3_result.n_questions == 0:
            log.error(
                "E7 P3 scored 0 should-escalate questions — the false-resolve safety "
                "ceiling is fed solely by the P3 faithfulness leg, so a structurally "
                "blind P3 population leaves it unmeasured. Failing the run closed; "
                "check that --questions carries P3 rows."
            )
        else:
            log.error(
                "E7 P3 scored %d should-escalate row(s) but NONE exercised the "
                "faithfulness gate — every row escalated at the retrieval/draft leg "
                "(mislabeled), so the false-resolve ceiling reads a vacuous measured "
                "0%% (never a breach) and the safety invariant is unmeasured. Failing "
                "the run closed; re-author the P3 gold so retrieval reads strong: %s",
                p3_result.n_questions,
                ", ".join(
                    f"{d.question_id}({d.escalate_leg})" for d in p3_result.mislabeled
                ),
            )
        failed = True

    # Issue #26: the P3 mislabel-RATIO guard — the PARTIAL-dilution complement of the
    # positive control above. That control fails closed only when EVERY P3 row is
    # mislabeled (zero exercised); a leg that is heavily but not entirely mislabeled
    # still exercises ≥1 row (so `passed` is True) yet runs the false-resolve ceiling
    # over a shrunken sample — latent today at the 3-row gold, a real masking path as
    # the P3 gold grows. Gated on `passed` so the empty / all-mislabeled cases stay
    # owned by the positive control above (one clear failure reason each); fires only
    # when the mislabeled FRACTION over the full presented population STRICTLY exceeds
    # the ceiling. Inert per-PR (p3_result is None → no P3 leg).
    if (
        p3_result is not None
        and p3_result.passed
        and p3_result.mislabel_ratio is not None
        and p3_result.mislabel_ratio > p3_mislabel_ratio_max
    ):
        log.error(
            "E7 P3 MISLABEL-RATIO BREACH — %d/%d P3 row(s) (%.0f%%) escalated before "
            "the faithfulness gate (mislabeled), exceeding the max %.0f%% "
            "(E7_P3_MISLABEL_RATIO_MAX, issue #26). The false-resolve ceiling is then "
            "measured over only %d exercised row(s) — a diluted sample that can mask a "
            "bad faithfulness gate. Re-author the drifted P3 gold so retrieval reads "
            "strong (or check τ_sim / a degraded answerer): %s",
            len(p3_result.mislabeled),
            p3_result.n_questions,
            p3_result.mislabel_ratio * 100.0,
            p3_mislabel_ratio_max * 100.0,
            len(p3_result.exercised),
            ", ".join(
                f"{d.question_id}({d.escalate_leg})" for d in p3_result.mislabeled
            ),
        )
        failed = True

    # US-059: the false-resolve ceiling is a pinned SAFETY invariant — a MEASURED
    # faithfulness-leg (P3) false-resolve rate above the buyer's ceiling fails the
    # run. Inert per-PR (no P3 leg → not measured); the blind-P3 guard above owns the
    # "requested but unmeasurable" case so a None rate here means genuinely no P3 leg.
    if ceiling_verdict.breached:
        log.error(
            "E7 FALSE-RESOLVE CEILING BREACH — measured faithfulness-leg %.1f%% "
            "(%d/%d P3) exceeds the buyer's ceiling %.1f%% "
            "(ESCALATION_FALSE_RESOLVE_CEILING, US-050). The deflection pipeline "
            "auto-resolved too many unanswerable questions (the Risk #3 safety "
            "failure).",
            (ceiling_verdict.rate or 0.0) * 100.0,
            ceiling_verdict.numerator,
            ceiling_verdict.denominator,
            ceiling_verdict.ceiling * 100.0,
        )
        failed = True

    return failed


# ---------------------------------------------------------------------------
# Standalone CLI entry (`python -m evals.retrieval.e7_runner`)
# ---------------------------------------------------------------------------


async def amain() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "US-052/053/054/055/056 E7 escalation eval. Always runs the "
            "deterministic P1a leg (genuinely-no-context → escalate at the "
            "retrieval gate, no draft/judge) and emits the consolidated US-055 "
            "metrics (deflection / false-resolve / false-escalate). With "
            "--include-p2 / --include-p3, additionally runs the LLM-judged P2 leg "
            "(answerable+faithful → auto-resolve) and/or P3 leg (should-escalate → "
            "escalate at the faithfulness gate; the moat), scored by the offline "
            "cross-family Claude judge. With --sweep, grids the knobs and reports "
            "the deflection-vs-false-resolve curve + the knee under the ceiling "
            "(US-056)."
        )
    )
    parser.add_argument(
        "--questions", type=Path, default=ESCALATION_GOLD,
        help="Path to escalation_gold.yaml",
    )
    parser.add_argument(
        "--out", type=Path, default=None,
        help="JSON output path; default: results/e7-<ISO-timestamp>.json",
    )
    parser.add_argument(
        "--include-p2",
        action="store_true",
        help=(
            "US-053: additionally score the P2 (answerable+faithful) population "
            "end-to-end — retrieve → retrieval gate → draft (real "
            "draft_support_answer) → OFFLINE Claude faithfulness judge → "
            "auto-resolve-or-escalate. Requires ANTHROPIC_API_KEY and the "
            "`anthropic` package (the cross-family judge cannot use the OpenAI key). "
            "LLM-judged, so it is a scheduled/weekly artifact (US-059), never a "
            "per-PR block."
        ),
    )
    parser.add_argument(
        "--include-p3",
        action="store_true",
        help=(
            "US-054: additionally score the P3 (should-escalate) population — the "
            "moat. Same pipeline as P2 but the only correct outcome is to clear the "
            "retrieval gate, draft, and ESCALATE at the faithfulness gate; an "
            "auto-resolve is a false-resolve (the Risk #3 safety failure). Same "
            "ANTHROPIC_API_KEY + `anthropic` requirement as --include-p2; LLM-judged, "
            "so scheduled/weekly (US-059), never a per-PR block — the false-resolve "
            "rate it records feeds the US-055/059 ceiling gate."
        ),
    )
    parser.add_argument(
        "--faithfulness-judge-min",
        type=int,
        default=DEFAULT_FAITHFULNESS_JUDGE_MIN,
        help=(
            "Minimum offline-judge faithfulness score (1-5) for a P2/P3 draft to "
            f"count as faithful → auto-resolve. Default {DEFAULT_FAITHFULNESS_JUDGE_MIN}. "
            "Distinct from the runtime gate's [0,1] faithfulness_cutoff."
        ),
    )
    parser.add_argument(
        "--include-p1b",
        action="store_true",
        help=(
            "US-057: additionally score the P1b population — the SAME P2 questions "
            "replayed under a NO-ACCESS viewer (its gold ACL-revoked via the E4 "
            "reset_viewer_acls machinery), which must escalate at the retrieval "
            "gate exactly like P1a (no privileged second pass). Deterministic "
            "(gate-only, no LLM), so it joins the per-PR tripwire (US-059), but it "
            "needs DB write access for the per-question chunk_acl reset — requires "
            "CORPUS_SEED_DATABASE_URL or DATABASE_URL. A P1b row that clears the "
            "gate (the gold leaked to a no-access viewer) fails the run. Also runs "
            "the US-058 non-disclosure byte-equality assertion (every P1b customer "
            "output must equal the P1a generic deferral), pinned `fail`."
        ),
    )
    parser.add_argument(
        "--sweep",
        action="store_true",
        help=(
            "US-056: grid-sweep τ_sim × N_min × the offline faithfulness floor, "
            "emit the deflection-vs-false-resolve curve, and select the "
            "deflection-maximizing KNEE subject to false-resolve ≤ ceiling — the "
            "recommended US-050 defaults. Same ANTHROPIC_API_KEY + `anthropic` "
            "requirement as --include-p2/-p3 (it drafts + offline-judges); the grid "
            "memoizes per question, so it costs the same LLM as one operating point. "
            "Scored over the SAME P1a/P2/P3 gold; the `--faithfulness-judge-min` "
            "single-point flag is ignored under --sweep (the floor is swept)."
        ),
    )
    parser.add_argument(
        "--sweep-tau-sim",
        default="0.30,0.40,0.50",
        help="Comma-separated τ_sim grid values in [0,1] (--sweep). Default 0.30,0.40,0.50.",
    )
    parser.add_argument(
        "--sweep-n-min",
        default="1,2,3",
        help="Comma-separated N_min grid values (>=1) (--sweep). Default 1,2,3.",
    )
    parser.add_argument(
        "--sweep-faithfulness-min",
        default="4,5",
        help="Comma-separated offline faithfulness-floor grid values in [1,5] (--sweep). Default 4,5.",
    )
    parser.add_argument(
        "--false-resolve-ceiling",
        type=float,
        default=None,
        help=(
            "Buyer's false-resolve ceiling in [0,1] for knee selection (--sweep). "
            "Default: ESCALATION_FALSE_RESOLVE_CEILING via get_false_resolve_ceiling() "
            "(US-050)."
        ),
    )
    parser.add_argument(
        "--p3-mislabel-ratio-max",
        type=float,
        default=None,
        help=(
            "Issue #26: max fraction of P3 rows allowed to be `mislabeled` "
            "(escalated before the faithfulness gate) before the weekly run fails. "
            "Guards against a heavily-mislabeled P3 gold quietly diluting the "
            "false-resolve sample (complements the positive control, which only "
            "fails on a fully-mislabeled leg). Default: E7_P3_MISLABEL_RATIO_MAX via "
            "get_p3_mislabel_ratio_max() (0.5)."
        ),
    )
    args = parser.parse_args()
    if not 1 <= args.faithfulness_judge_min <= 5:
        parser.error("--faithfulness-judge-min must be in [1,5]")
    p3_mislabel_ratio_max = (
        args.p3_mislabel_ratio_max
        if args.p3_mislabel_ratio_max is not None
        else get_p3_mislabel_ratio_max()
    )
    if not 0.0 <= p3_mislabel_ratio_max <= 1.0:
        parser.error("--p3-mislabel-ratio-max must be in [0,1]")

    sweep_tau_sims: list[float] = []
    sweep_n_mins: list[int] = []
    sweep_faith_mins: list[int] = []
    sweep_ceiling = 0.0
    if args.sweep:
        try:
            sweep_tau_sims = [float(x) for x in args.sweep_tau_sim.split(",") if x.strip()]
            sweep_n_mins = [int(x) for x in args.sweep_n_min.split(",") if x.strip()]
            sweep_faith_mins = [int(x) for x in args.sweep_faithfulness_min.split(",") if x.strip()]
        except ValueError as e:
            parser.error(f"invalid --sweep-* grid value: {e}")
        if not (sweep_tau_sims and sweep_n_mins and sweep_faith_mins):
            parser.error("--sweep grids must each carry at least one value")
        if any(not 0.0 <= t <= 1.0 for t in sweep_tau_sims):
            parser.error("--sweep-tau-sim values must be in [0,1]")
        if any(n < 1 for n in sweep_n_mins):
            parser.error("--sweep-n-min values must be >= 1")
        if any(not 1 <= f <= 5 for f in sweep_faith_mins):
            parser.error("--sweep-faithfulness-min values must be in [1,5]")
        sweep_ceiling = (
            args.false_resolve_ceiling
            if args.false_resolve_ceiling is not None
            else get_false_resolve_ceiling()
        )
        if not 0.0 <= sweep_ceiling <= 1.0:
            parser.error("--false-resolve-ceiling must be in [0,1]")

    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )

    # Lazy import of the E4 runner's infra helpers. One-directional: e7_runner
    # imports runner, never the reverse — so there is no import cycle, and the
    # core API above stays importable (and unit-testable) without dragging in the
    # runner's RAGAS / Anthropic dependencies.
    from . import runner as r

    supabase_url = os.environ.get("SUPABASE_URL")
    if not supabase_url:
        raise RuntimeError("SUPABASE_URL is required")
    openai_api_key = os.environ.get("OPENAI_API_KEY")
    if not openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is required (hybrid retrieval embeds the query)")
    service_role_key = (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_SERVICE_ROLE")
    )
    jwt_secret = os.environ.get("SUPABASE_JWT_SECRET") or r.LOCAL_JWT_SECRET
    anon_key = os.environ.get("SUPABASE_ANON_KEY") or service_role_key
    if not anon_key:
        raise RuntimeError(
            "SUPABASE_ANON_KEY or SUPABASE_SERVICE_ROLE_KEY is required "
            "(PostgREST edge router needs an apikey header)"
        )

    questions = load_escalation_questions(args.questions)
    # The gate knobs + per-row floor come from the SAME config the production
    # support endpoint uses (US-050), so a buyer's knob change is reflected here.
    config = EscalationConfig.from_env()
    match_threshold = get_similarity_threshold()

    # The OFFLINE Claude judge for the P2/P3 legs + the knob sweep. Lazy-imported
    # so the deterministic P1a-only path keeps zero Anthropic dependency (mirrors
    # runner._get_anthropic). Built once and shared by the LLM-judged legs/sweep.
    include_judged = args.include_p2 or args.include_p3 or args.sweep
    anthropic_client: Any | None = None
    if include_judged:
        anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not anthropic_api_key:
            raise RuntimeError(
                "--include-p2/--include-p3/--sweep require ANTHROPIC_API_KEY (the "
                "offline cross-family Claude judge cannot use the OpenAI key)"
            )
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "--include-p2/--include-p3/--sweep require the `anthropic` package. "
                "Run `pip install -r evals/retrieval/requirements.txt`."
            ) from e
        anthropic_client = anthropic.AsyncAnthropic(api_key=anthropic_api_key)

    # We retrieve as the full-access corpus owner so "is the answer in the corpus
    # at all" is isolated from access filtering (the access-filtered case is P1b,
    # US-057). The owner is a Default-Workspace member, so retrieval runs the real
    # RLS + membership path. P1a expects this to be weak; P2/P3 expect it strong.
    owner_headers = r.user_headers(
        r.mint_user_jwt(r.CORPUS_USER_ID, r.CORPUS_USER_EMAIL, jwt_secret),
        anon_key,
    )
    openai_client = AsyncOpenAI(api_key=openai_api_key)
    started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    # US-057: P1b infrastructure (the DB-backed no-access replay). Resolved only
    # when --include-p1b is set. P1b is deterministic (gate-only, no LLM), so it
    # needs no Anthropic judge — but it DOES need a direct DB connection for the
    # per-question chunk_acl reset (the E4 reset_viewer_acls machinery) plus the
    # no-access viewer's PostgREST headers (retrieval runs under the viewer's JWT
    # so RLS + the membership clause filter the gold).
    p1b_database_url: str | None = None
    no_access_headers: dict[str, str] | None = None
    p1b_all_stable_ids: list[str] = []
    p1b_sid_to_chunk_id: dict[str, uuid.UUID] = {}
    if args.include_p1b:
        p1b_database_url = (
            os.environ.get("CORPUS_SEED_DATABASE_URL")
            or os.environ.get("DATABASE_URL")
        )
        if not p1b_database_url:
            raise RuntimeError(
                "--include-p1b requires CORPUS_SEED_DATABASE_URL or DATABASE_URL "
                "(the no-access replay rewrites chunk_acl directly via asyncpg)"
            )
        # Idempotently ensure the no-access viewer exists + is a Default-Workspace
        # member (else the US-003 membership clause hides the whole corpus).
        await r.ensure_viewer_users(p1b_database_url)
        stable_id_map = await r.fetch_stable_id_map(p1b_database_url)
        # US-108: resolve the P2/P3 content anchors to the CURRENT chunk stable_ids
        # against the live corpus text — the same fail-loud resolution
        # `runner.resolve_gold_anchors` runs for the base retrieval gold. This
        # injects each gold-bearing row's `gold_stable_ids` (P1a rows stay empty)
        # so the no-access replay below can revoke exactly this question's gold; a
        # non-resolving anchor raises ZeroResolveError and fails the run here,
        # before any retrieval.
        from .content_anchors import fetch_chunk_contents

        resolve_escalation_gold(
            questions, await fetch_chunk_contents(p1b_database_url)
        )
        p1b_all_stable_ids = sorted(stable_id_map.values())
        p1b_sid_to_chunk_id = {
            sid: uuid.UUID(cid) for cid, sid in stable_id_map.items()
        }
        no_access_headers = r.user_headers(
            r.mint_user_jwt(
                r.NO_ACCESS_VIEWER_ID, r.NO_ACCESS_VIEWER_EMAIL, jwt_secret
            ),
            anon_key,
        )

    p1b_result: E7P1bResult | None = None
    p2_result: E7P2Result | None = None
    p3_result: E7P3Result | None = None
    sweep_result: E7Sweep | None = None
    async with httpx.AsyncClient(timeout=30.0) as http:

        async def retrieve(question: str) -> list[SearchDocumentsResult]:
            # Hybrid at DEFAULT_TOP_K = the production deflection-pipeline
            # retrieval (US-049): run_deflection_pipeline retrieves at
            # DEFAULT_TOP_K (escalation.py), so the retrieval gate's n_cleared
            # and the swept N_min knee are calibrated at the depth production
            # applies them at runtime — not the E4/E6 recall-curve depth (10).
            return await r.run_query(
                "hybrid",
                openai_client,
                http,
                supabase_url,
                owner_headers,
                question,
                top_k=DEFAULT_TOP_K,
            )

        p1a_result = await run_e7_p1a(
            questions=questions,
            retrieve=retrieve,
            config=config,
            match_threshold=match_threshold,
        )

        if args.include_p1b and p1b_database_url is not None and no_access_headers is not None:
            # US-057: replay the P2 questions under the no-access viewer. A direct
            # asyncpg connection rewrites chunk_acl per question; retrieval itself
            # goes through PostgREST under the viewer's JWT (RLS-enforced). There is
            # exactly ONE retrieval callable here — the no-access one — and NO
            # owner/privileged retrieve, so the leg cannot do a privileged second
            # pass (US-057/058).
            import asyncpg

            db_conn = await asyncpg.connect(p1b_database_url)
            try:
                async def retrieve_no_access(
                    q: dict[str, Any],
                ) -> list[SearchDocumentsResult]:
                    # Revoke THIS question's gold from the no-access viewer (the E4
                    # `compute_visible_stable_ids("no_access", …)` set is everything
                    # EXCEPT the gold), then retrieve under the viewer's own JWT so
                    # the gold is filtered out by RLS + the membership clause.
                    visible = r.compute_visible_stable_ids(
                        "no_access", q, p1b_all_stable_ids, {}
                    )
                    visible_chunk_ids: dict[ViewerKind, set[uuid.UUID]] = {
                        "partial_access": set(),
                        "no_access": {
                            p1b_sid_to_chunk_id[sid]
                            for sid in visible
                            if sid in p1b_sid_to_chunk_id
                        },
                    }
                    await r.reset_viewer_acls(db_conn, visible_chunk_ids)
                    return await r.run_query(
                        "hybrid",
                        openai_client,
                        http,
                        supabase_url,
                        no_access_headers,
                        q["question"],
                        top_k=DEFAULT_TOP_K,
                    )

                p1b_result = await run_e7_p1b(
                    questions=questions,
                    retrieve_no_access=retrieve_no_access,
                    config=config,
                    match_threshold=match_threshold,
                    # chunk_id -> stable_id, so the leg can measure whether any GOLD
                    # stable id actually reached the no-access viewer (US-057).
                    stable_id_by_chunk_id=stable_id_map,
                )
            finally:
                await db_conn.close()

        if include_judged and anthropic_client is not None:

            async def draft(
                message: str, chunks: list[SearchDocumentsResult]
            ) -> str:
                # The REAL production drafter — a regression in it shows up here.
                return await draft_support_answer(openai_client, message, chunks)

            async def judge(
                question: str,
                reference: str,
                chunks: list[SearchDocumentsResult],
                draft_text: str,
            ) -> dict[str, int]:
                # The OFFLINE cross-family judge (Claude grading gpt-4o-mini
                # drafts) — NOT the runtime one-call faithfulness_gate.
                context = _render_judge_context(chunks)
                return await r.judge_answer(
                    anthropic_client, question, reference, context, draft_text
                )

            if args.include_p2:
                p2_result = await run_e7_p2(
                    questions=questions,
                    retrieve=retrieve,
                    draft=draft,
                    judge=judge,
                    config=config,
                    match_threshold=match_threshold,
                    judge_model=r.JUDGE_MODEL,
                    faithfulness_judge_min=args.faithfulness_judge_min,
                )

            if args.include_p3:
                p3_result = await run_e7_p3(
                    questions=questions,
                    retrieve=retrieve,
                    draft=draft,
                    judge=judge,
                    config=config,
                    match_threshold=match_threshold,
                    judge_model=r.JUDGE_MODEL,
                    faithfulness_judge_min=args.faithfulness_judge_min,
                )

            if args.sweep:
                # US-056: grid-sweep the knobs + pick the knee. Memoizes
                # retrieve/draft/judge per question, so the whole grid costs the
                # same LLM as one operating point. `config.faithfulness_cutoff` is
                # the runtime [0,1] cutoff carried in each grid config for
                # completeness; the offline legs threshold the swept 1-5 floor.
                sweep_result = await run_e7_sweep(
                    questions=questions,
                    retrieve=retrieve,
                    draft=draft,
                    judge=judge,
                    tau_sims=sweep_tau_sims,
                    n_mins=sweep_n_mins,
                    faithfulness_mins=sweep_faith_mins,
                    match_threshold=match_threshold,
                    faithfulness_cutoff=config.faithfulness_cutoff,
                    ceiling=sweep_ceiling,
                    judge_model=r.JUDGE_MODEL,
                )

    # US-055: roll the per-leg outcomes into the consolidated operating-objective
    # rates (deflection / false-resolve / false-escalate). The gated safety
    # false-resolve number is the faithfulness leg (P3), present only when that
    # opt-in leg ran; the retrieval-leg P1a/P1b false-resolves are surfaced for
    # monitoring but excluded from the gated rate (they are pinned unconditionally by
    # the P1a/P1b gate checks below).
    metrics = compute_e7_metrics(p1a_result, p2_result, p3_result, p1b_result)

    # US-058: the pinned, deterministic P1b non-disclosure assertion — the bytes a
    # no-access customer sees on a P1b escalation must be byte-for-byte identical to
    # the P1a generic deferral (no existence bit leaked). Computed only when the P1b
    # leg ran; like the P1a/P1b gate invariants it is pinned `fail` and decides the
    # exit code (US-059).
    non_disclosure: E7P1bNonDisclosure | None = None
    if p1b_result is not None:
        non_disclosure = assert_p1b_non_disclosure(p1a_result, p1b_result)

    # US-059: enforce the buyer's false-resolve ceiling on the consolidated metric
    # as a pinned safety invariant. The gated rate is the faithfulness leg (P3), so
    # per-PR (P1a/P1b only, no P3) it is not measured and the ceiling gate is inert —
    # the retrieval-leg P1a/P1b false-resolves are pinned by the P1a/P1b gate checks
    # below. The ceiling has teeth in the weekly run, where the LLM-judged P3
    # faithfulness leg feeds the rate.
    ceiling = get_false_resolve_ceiling()
    ceiling_verdict = assert_false_resolve_ceiling(metrics, ceiling)

    payload: dict[str, Any] = {
        "generated_at": started_at,
        "e7_p1a": p1a_result.to_dict(),
        "e7_metrics": metrics.to_dict(),
        "e7_false_resolve_ceiling": ceiling_verdict.to_dict(),
    }
    if p1b_result is not None:
        payload["e7_p1b"] = p1b_result.to_dict()
    if non_disclosure is not None:
        payload["e7_p1b_non_disclosure"] = non_disclosure.to_dict()
    if p2_result is not None:
        payload["e7_p2"] = p2_result.to_dict()
    if p3_result is not None:
        payload["e7_p3"] = p3_result.to_dict()
    if sweep_result is not None:
        payload["e7_sweep"] = sweep_result.to_dict()
    out_path = args.out
    if out_path is None:
        results_dir = Path(__file__).resolve().parent / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        ts = started_at.replace(":", "").replace("-", "")
        out_path = results_dir / f"e7-{ts}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    print("\n".join(render_e7_p1a_section(p1a_result)))
    if p1b_result is not None:
        print("\n".join(render_e7_p1b_section(p1b_result)))
    if non_disclosure is not None:
        print("\n".join(render_e7_p1b_non_disclosure_section(non_disclosure)))
    if p2_result is not None:
        print("\n".join(render_e7_p2_section(p2_result)))
    if p3_result is not None:
        print("\n".join(render_e7_p3_section(p3_result)))
    print("\n".join(render_e7_metrics_section(metrics)))
    print("\n".join(render_e7_false_resolve_ceiling_section(ceiling_verdict)))
    if sweep_result is not None:
        print("\n".join(render_e7_sweep_section(sweep_result)))
    print(f"\n→ {out_path}")

    # P2 is a tunable quality metric, not a per-PR hard block (US-059): a
    # false-escalate never fails the run. A structurally-blind P2 leg (you asked
    # for --include-p2 but scored 0 rows) IS surfaced loudly but still does not
    # fail the run on its own — only the pinned P1a invariant decides the exit
    # code below. The detail is in the JSON regardless.
    if p2_result is not None:
        if not p2_result.passed:
            log.error(
                "E7 P2 scored 0 answerable questions — the deflection rate is "
                "structurally blind. Check that --questions carries P2 rows."
            )
        elif p2_result.false_escalates:
            log.warning(
                "E7 P2 false-escalate(s) (annoyance, not a safety failure): %s",
                ", ".join(
                    f"{d.question_id}({d.escalate_leg})"
                    for d in p2_result.false_escalates
                ),
            )

    # P3 false-resolves are the SAFETY number (Risk #3), so they are surfaced at
    # ERROR level (louder than a P2 false-escalate warning) — but per US-059 the
    # false-resolve RATE vs the buyer's ceiling is consolidated in US-055 and
    # enforced by the ceiling gate in e7_pinned_invariants_failed below (which also
    # fails the run closed on a P3 leg that never exercises the faithfulness gate),
    # NOT per-leg here. So this block reports loudly without itself deciding the exit
    # code. Gated on a non-empty population (not on `passed`) so the mislabeled rows
    # of an all-mislabeled leg — exactly the gold-authoring defect that trips the
    # positive control — are still surfaced as warnings here.
    if p3_result is not None and p3_result.n_questions > 0:
        if p3_result.false_resolves:
            log.error(
                "E7 P3 FALSE-RESOLVE(s) (the Risk #3 safety failure — an "
                "unanswerable question auto-resolved; the US-055/059 ceiling "
                "gate governs this rate): %s",
                ", ".join(d.question_id for d in p3_result.false_resolves),
            )
        if p3_result.mislabeled:
            log.warning(
                "E7 P3 mislabeled row(s) — escalated before the faithfulness "
                "gate, so the gold retrieval was not actually strong; "
                "re-author: %s",
                ", ".join(
                    f"{d.question_id}({d.escalate_leg})"
                    for d in p3_result.mislabeled
                ),
            )

    # Pinned fail (US-059): the deterministic gate-only legs + the P3 positive
    # control + the weekly ceiling decide this runner's exit code. Non-zero exit
    # AFTER the JSON is written, so the detail is preserved — same shape as E6's leak
    # gate. The decision is a pure function of the scored legs (unit-tested directly).
    failed = e7_pinned_invariants_failed(
        p1a_result=p1a_result,
        p1b_result=p1b_result,
        non_disclosure=non_disclosure,
        p3_result=p3_result,
        ceiling_verdict=ceiling_verdict,
        p3_mislabel_ratio_max=p3_mislabel_ratio_max,
    )
    return 1 if failed else 0


def main() -> None:
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
