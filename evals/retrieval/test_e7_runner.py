"""US-052/053/054/055/056/057/058/059 validation test: E7 legs + metrics + sweep + P1b.

Drives the **real** `e7_runner.run_e7_p1a` / `run_e7_p2` / `run_e7_p3` /
`run_e7_p1b` end to end with call-counting fakes (no DB, no network, no LLM) — the
gate they call is the **real** `escalation.retrieval_gate`, so this test breaks if
a future PR breaks the gate — then feeds the real per-leg results into the real
`e7_runner.compute_e7_metrics` and the real `e7_runner.run_e7_sweep`.

P1a (US-052) — covers the PRD validation test:
  * a P1a question whose answer is absent from the corpus -> decision `escalate`,
    caught at the retrieval gate, with ZERO draft and ZERO judge calls; the
    recorded `top1_cosine` / `n_cleared` make a near-miss visible;
plus the failure indicators:
  * a P1a row that CLEARS the gate (strong retrieval) is flagged `correct=False`
    and fails the run — a retrieval-leg false-resolve the eval must detect (so a
    "pass" is never structurally blind);
  * the gate thresholds the real pre-fusion COSINE, not the RRF `similarity` rank
    artifact (a keyword-only row with a huge `similarity` but no cosine is weak);
  * a run with no P1a rows is NOT a pass (structurally blind guard);
  * the decision path is deterministic (same input -> same output).

P2 (US-053) — covers the PRD validation test:
  * a P2 question with strong gold support + a hand-authored reference -> decision
    `auto_resolve`, counted toward deflection, with the offline-judge faithfulness
    score recorded and exactly 1 draft + 1 judge call;
plus the failure indicators:
  * a P2 row that escalates is counted as a `false_escalate` — at the retrieval
    gate (weak, 0 draft/0 judge), on an empty draft (1 draft/0 judge), or at the
    faithfulness leg (judge below the floor, 1 draft/1 judge);
  * the faithfulness leg is the OFFLINE judge (it receives the reference the
    runtime one-call gate never sees), not the runtime gate;
  * a run with no P2 rows is NOT a pass (structurally blind deflection guard).

P3 (US-054) — the moat. Covers the PRD validation test:
  * a P3 question with strong retrieval but an unfaithful draft -> decision
    `escalate` at the FAITHFULNESS leg, `correct=True`, NOT counted as a
    false-resolve, with the offline faithfulness score recorded (1 draft/1 judge);
plus the failure indicators:
  * a P3 row that AUTO-RESOLVES (faithful draft) is a `false_resolve` — the Risk #3
    safety failure — tallied toward the false-resolve rate, never silently passed;
  * a P3 row that escalates at the RETRIEVAL gate is `mislabeled` (its retrieval
    was not actually strong, so it never exercised the faithfulness gate) — it
    escalated (safe) but is NOT a false-resolve and NOT correct;
  * the faithfulness floor is inclusive and flips the P3 verdict vs P2;
  * a run with no P3 rows is NOT a pass (structurally blind false-resolve guard);
  * a run whose P3 rows are ALL mislabeled (none exercise the faithfulness gate) is
    NOT a pass either — the rate reads a vacuous measured 0%, so the positive control
    requires >=1 EXERCISED row, not merely a non-empty population.

US-055 — consolidated metrics. Covers the PRD validation test:
  * a known mix of P1a/P2/P3 producing one wrong auto-resolve (a P3 false-resolve)
    and one wrong escalate (a P2 false-escalate) -> the three rates match the
    hand-computed numerator/denominator exactly, and the false-resolve count
    includes the wrong auto-resolve;
plus the failure indicators:
  * the false-resolve number is NOT folded into a generic accuracy score — it
    carries its own numerator/denominator + per-population breakdown, and the
    ceiling-GATED rate is the P3 faithfulness-leg only (the retrieval-leg P1a/P1b
    false-resolves are surfaced monitor-only, so always-escalating true-negatives
    never dilute the safety signal);
  * a rate over an empty rate-bearing population is `None` (blind), never a false
    0.0, so an opt-out leg (P2/P3 not run) cannot masquerade as passing;
  * only the false-resolve rate is flagged as the pinned safety metric.

US-056 — knob sweep + knee. Covers the PRD validation test:
  * a labeled subset and a 2×2×2 knob grid with a KNOWN knee under a
    false-resolve ≤ 0.05 ceiling -> the curve lists every grid point's
    (deflection, false-resolve); the knee is the highest-deflection point with
    false-resolve ≤ ceiling; the knee's knob values are reported;
plus the failure indicators:
  * a knee with false-resolve ABOVE the ceiling is never chosen (a higher-deflection
    but infeasible point is rejected in favor of the feasible one);
  * when NO point satisfies the ceiling, that is reported explicitly
    (`knee_reason="no_point_under_ceiling"`), not silently downgraded to least-bad;
  * the objective is the ceiling-constrained one, not "maximize accuracy";
  * the grid memoizes per question, so an 8-point sweep drafts/judges each
    question at most once (the curve reflects knobs, not LLM re-sampling).

US-057 — P1b no-access replay (identity-based isolation check). Covers the PRD
validation test:
  * a P2 question replayed under a no-access viewer (gold filtered out) returns NO
    gold chunk, so the row is clean — with 0 draft / 0 judge calls — whether the
    answerability gate reads weak OR strong off legitimately-granted non-gold;
plus the failure indicators:
  * a P1b row whose no-access retrieval RETURNS a gold chunk (`leaked_gold_ids`
    non-empty) is flagged `correct=False` and fails the run (isolation/disclosure);
  * the leak check is decoupled from the gate: a strong gate off NON-gold is NOT a
    leak, and a gold chunk returned BELOW tau_sim (gate weak) IS still a leak;
  * NO privileged second pass: the leg is given only the no-access retrieval
    callable and retrieves exactly once per row (it receives the full question
    dict so the gold can be revoked, never a privileged/owner retrieval);
  * only P2 rows are replayed; an empty P2 population is NOT a pass;
  * a P1b leak is surfaced monitor-only in the consolidated false-resolve breakdown
    and hard-fails unconditionally, but is excluded from the ceiling-gated rate (US-055).

US-058 — P1b non-disclosure byte equality (escalation path only). Covers the PRD
validation test:
  * a P1a row and a P1b row (a P2 question under a no-access viewer) that BOTH
    escalate, carrying DIFFERENT internal gate reasons, both show the customer the
    byte-for-byte SAME generic deferral — derived through the REAL
    `escalation._escalated` / `GENERIC_DEFERRAL` path — so a no-access escalation
    discloses no existence bit;
plus the failure indicators:
  * injecting an access-denied reason into the P1b customer output makes the
    assertion FAIL loudly (it really pins the invariant, not a trivial pass);
  * US-058 is DECOUPLED from the US-057 identity leak check: a legitimate non-gold
    draft (gate strong, no gold returned) is NOT flagged here — the gold-leak case
    is owned entirely by the US-057 identity check;
  * an empty P1b population is NOT a pass (structurally-blind guard).

US-059 — false-resolve ceiling gate. Covers the PRD core + failure indicators:
  * a measured faithfulness-leg (P3) false-resolve rate ABOVE the buyer's ceiling is
    a breach that fails the run (the pinned safety invariant the E8 gate enforces);
  * equality is feasible (rate == ceiling does NOT breach), matching the knee's
    `false_resolve <= ceiling`, and a rate strictly below passes;
  * a None rate (no P3 faithfulness-leg rows scored) is "not measured", never a
    breach — the per-leg blindness guards own structural blindness;
  * the gate is INERT on a per-PR (P1a/P1b-only, no P3) run — the gated rate is
    unmeasured (None) there, so it cannot red-bar a merge when the deterministic
    gates pass (the retrieval-leg P1a/P1b false-resolves are pinned separately);
  * the verdict to_dict + render carry ceiling/rate/n/d/breached for the weekly
    issue body.

Run:
    python -m evals.retrieval.test_e7_runner
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "backend"))

from escalation import GENERIC_DEFERRAL, EscalationConfig  # noqa: E402
from retrieval import SearchDocumentsResult  # noqa: E402

from evals.retrieval.e7_runner import (  # noqa: E402
    DEFAULT_P3_MISLABEL_RATIO_MAX,
    E7Metrics,
    E7P1aResult,
    E7P1bNonDisclosure,
    E7P1bResult,
    E7P2Result,
    E7P3Result,
    E7Sweep,
    P1aDecision,
    P1bDecision,
    P1bLeak,
    P2Decision,
    P3Decision,
    SweepPoint,
    assert_false_resolve_ceiling,
    assert_p1b_non_disclosure,
    compute_e7_metrics,
    e7_pinned_invariants_failed,
    get_p3_mislabel_ratio_max,
    render_e7_false_resolve_ceiling_section,
    render_e7_p1b_non_disclosure_section,
    run_e7_p1a,
    run_e7_p1b,
    run_e7_p2,
    run_e7_p3,
    run_e7_sweep,
)

JUDGE_MODEL = "claude-haiku-4-5"  # the offline cross-family judge model id

TAU, N_MIN, THRESH = 0.4, 2, 0.3
CONFIG = EscalationConfig(tau_sim=TAU, n_min=N_MIN, faithfulness_cutoff=0.7)


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


# --- fixtures -------------------------------------------------------------


def _row(chunk_id: str, cosine: float, *, similarity: float | None = None) -> SearchDocumentsResult:
    """A vector/hybrid row carrying a real pre-fusion cosine (US-046)."""
    return SearchDocumentsResult(
        id=chunk_id,
        document_id=f"doc-{chunk_id}",
        chunk_index=0,
        content=f"content {chunk_id}",
        similarity=cosine if similarity is None else similarity,
        filename=f"{chunk_id}.txt",
        cosine_similarity=cosine,
    )


def _kw_row(chunk_id: str, rrf: float) -> SearchDocumentsResult:
    """A keyword-only row: a big RRF `similarity` rank artifact, NO cosine."""
    return SearchDocumentsResult(
        id=chunk_id,
        document_id=f"doc-{chunk_id}",
        chunk_index=0,
        content=f"content {chunk_id}",
        similarity=rrf,
        filename=f"{chunk_id}.txt",
        cosine_similarity=None,
    )


WEAK = [_row("a", 0.10), _row("b", 0.09)]  # top1 0.10 < tau -> weak
STRONG = [_row("a", 0.70), _row("b", 0.60)]  # top1 >= tau AND 2 >= n_min -> strong
KW_ONLY = [_kw_row("k", 4.0)]  # huge RRF similarity, but no cosine -> weak
NEAR_MISS = [_row("a", 0.39)]  # top1 0.39 just below tau 0.40; clears thresh -> n_cleared 1
# top1 0.50, 2 rows clear thresh 0.3: strong at tau_sim 0.40, weak at tau_sim 0.60
# -> the τ_sim sweep lever flips MID's gate (US-056 sweep fixtures).
MID = [_row("a", 0.50), _row("b", 0.32)]

# US-057 (identity-based leak): a no-access retrieval that RETURNED the gold chunk.
# `_p2`/`_p3` gold is "warranty-terms:0", and the default `_run_p1b` chunk-id→
# stable-id map is identity over the row ids, so a row whose id IS the gold stable id
# is a measured leak. WEAK/STRONG (ids "a"/"b") are NON-gold, so a strong gate off
# them is NOT a leak. GOLD_LEAK_STRONG leaks the gold AND clears the gate;
# GOLD_LEAK_WEAK returns the gold at a low cosine (gate WEAK) — still a leak, proving
# the identity check is decoupled from the answerability gate.
GOLD = "warranty-terms:0"
GOLD_LEAK_STRONG = [_row(GOLD, 0.70), _row("b", 0.60)]
GOLD_LEAK_WEAK = [_row(GOLD, 0.20)]


class _FakeRetriever:
    """Call-counting fake `Retrieve`: maps question text -> canned rows.

    The orchestrator is given ONLY this retrieval callable — there is no answerer
    or judge client anywhere on the P1a path, so "0 draft / 0 judge calls" is
    structural, not a policy. `calls` proves retrieval ran exactly once per row.
    """

    def __init__(self, rows_by_question: dict[str, list[SearchDocumentsResult]]) -> None:
        self.rows_by_question = rows_by_question
        self.calls = 0
        self.seen: list[str] = []

    async def __call__(self, question: str) -> list[SearchDocumentsResult]:
        self.calls += 1
        self.seen.append(question)
        return self.rows_by_question.get(question, [])


def _p1a(qid: str, question: str) -> dict:
    return {"id": qid, "escalation": "no_context", "question": question}


def _run(
    questions: list[dict],
    rows_by_question: dict[str, list[SearchDocumentsResult]],
) -> tuple[E7P1aResult, _FakeRetriever]:
    retriever = _FakeRetriever(rows_by_question)
    result = asyncio.run(
        run_e7_p1a(
            questions=questions,
            retrieve=retriever,
            config=CONFIG,
            match_threshold=THRESH,
        )
    )
    return result, retriever


# --- tests ----------------------------------------------------------------


def test_p1a_escalates_at_gate() -> None:
    """PRD core: every genuinely-no-context P1a row escalates at the retrieval
    gate with 0 draft / 0 judge calls; top1_cosine + n_cleared are recorded."""
    questions = [
        _p1a("e7-p1a-01", "What is Acme Co's stock ticker symbol?"),
        _p1a("e7-p1a-02", "What is the parental leave policy?"),
        _p1a("e7-p1a-03", "Which charities does Acme sponsor?"),
    ]
    rows = {q["question"]: WEAK for q in questions}
    result, retriever = _run(questions, rows)

    _check(result.n_questions == 3, f"expected 3 P1a rows, got {result.n_questions}")
    _check(retriever.calls == 3, f"retrieval must run once per row, got {retriever.calls}")
    _check(result.total_draft_calls == 0, "P1a leg must make ZERO draft calls")
    _check(result.total_judge_calls == 0, "P1a leg must make ZERO judge calls")
    _check(result.passed is True, "all-escalate, no-draft/judge run must pass")
    _check(result.cleared_gate == [], "no P1a row should clear the gate")
    for d in result.decisions:
        _check(d.decision == "escalate", f"{d.question_id}: expected escalate, got {d.decision}")
        _check(d.correct is True, f"{d.question_id}: escalate is the correct P1a outcome")
        _check(d.gate_strong is False, f"{d.question_id}: gate must be weak")
        _check(d.top1_cosine == 0.10, f"{d.question_id}: top1_cosine must be recorded, got {d.top1_cosine}")
        _check(d.n_cleared == 0, f"{d.question_id}: n_cleared recorded, got {d.n_cleared}")
        _check(d.draft_calls == 0 and d.judge_calls == 0, "per-row 0 draft/0 judge")
    print("ok: P1a rows escalate at the retrieval gate, 0 draft + 0 judge, scores recorded")


def test_near_miss_is_visible() -> None:
    """A near-miss (top1_cosine just below tau_sim) still escalates, but the
    recorded top1_cosine / n_cleared expose how close it was (US-052)."""
    questions = [_p1a("e7-p1a-near", "An off-topic but lexically-adjacent question.")]
    result, _ = _run(questions, {questions[0]["question"]: NEAR_MISS})
    d = result.decisions[0]
    _check(d.decision == "escalate", f"near-miss must still escalate, got {d.decision}")
    _check(d.top1_cosine == 0.39, f"near-miss top1_cosine recorded, got {d.top1_cosine}")
    _check(d.n_cleared == 1, f"the row cleared match_threshold, so n_cleared==1, got {d.n_cleared}")
    _check("tau_sim" in d.gate_reason, f"reason should name the failing knob, got {d.gate_reason!r}")
    _check(result.passed is True, "an escalated near-miss is still a pass")
    print("ok: a near-miss escalates and records top1_cosine/n_cleared for visibility")


def test_p1a_clearing_gate_is_flagged() -> None:
    """Failure indicator: a P1a row whose retrieval is (wrongly) strong WOULD draft
    — a retrieval-leg false-resolve. It is flagged correct=False and fails the run,
    proving the eval detects the defect (a pass is never structurally blind)."""
    questions = [
        _p1a("e7-p1a-ok", "A genuinely off-topic question."),
        _p1a("e7-p1a-leak", "A no-context question with deceptively strong retrieval."),
    ]
    rows = {
        questions[0]["question"]: WEAK,
        questions[1]["question"]: STRONG,
    }
    result, _ = _run(questions, rows)

    _check(result.passed is False, "a P1a row clearing the gate must fail the run")
    leaked = result.cleared_gate
    _check([d.question_id for d in leaked] == ["e7-p1a-leak"], f"the strong row must be flagged, got {leaked}")
    bad = leaked[0]
    _check(bad.decision == "draft", f"a cleared P1a row would draft, got {bad.decision}")
    _check(bad.correct is False, "a drafted P1a row is incorrect")
    _check(bad.gate_strong is True, "the gate called retrieval strong on the bad row")
    # Even on a failing run, no draft/judge call was actually made — the leg only
    # records that the row WOULD have drafted; it never executes one.
    _check(result.total_draft_calls == 0 and result.total_judge_calls == 0, "still 0 draft/0 judge")
    print("ok: a P1a row that clears the gate is flagged as a false-resolve risk and fails the run")


def test_gate_reads_cosine_not_rrf_similarity() -> None:
    """The gate (and so E7) thresholds the real pre-fusion COSINE, never the RRF
    `similarity` rank artifact: a keyword-only row with a huge similarity but no
    cosine is weak -> escalate (US-046/047)."""
    questions = [_p1a("e7-p1a-kw", "A question that only matches lexically.")]
    result, _ = _run(questions, {questions[0]["question"]: KW_ONLY})
    d = result.decisions[0]
    _check(d.decision == "escalate", f"keyword-only (no cosine) must be weak -> escalate, got {d.decision}")
    _check(d.top1_cosine is None, f"no vector cosine -> top1_cosine None, got {d.top1_cosine}")
    _check("no_vector_cosine" in d.gate_reason, f"reason should flag the missing cosine, got {d.gate_reason!r}")
    _check(result.passed is True, "a correctly-escalated keyword-only row is a pass")
    print("ok: the gate thresholds cosine, not the RRF similarity artifact")


def test_empty_p1a_is_not_a_pass() -> None:
    """A run with no P1a rows is structurally blind — it must NOT pass (a zero
    false-resolve over zero questions is a false pass; mirrors E6's control)."""
    questions = [
        {
            "id": "e7-p2",
            "escalation": "answerable_faithful",
            "question": "How long is the warranty?",
            "gold_stable_ids": ["warranty-terms:0"],
        },
    ]
    result, retriever = _run(questions, {})
    _check(result.n_questions == 0, f"no P1a rows expected, got {result.n_questions}")
    _check(retriever.calls == 0, "non-P1a rows must not be scored by the P1a leg")
    _check(result.passed is False, "a run scoring zero P1a rows must not pass")
    print("ok: an empty P1a population is not a pass (structurally-blind guard)")


def test_non_p1a_rows_ignored() -> None:
    """Only `no_context` rows are scored by the P1a leg; P2/P3 rows are ignored
    (their faithfulness legs are US-053/054)."""
    questions = [
        _p1a("e7-p1a-1", "An off-topic question."),
        {
            "id": "e7-p2-1",
            "escalation": "answerable_faithful",
            "question": "warranty?",
            "gold_stable_ids": ["warranty-terms:0"],
        },
        {
            "id": "e7-p3-1",
            "escalation": "should_escalate",
            "question": "jewelry warranty?",
            "gold_stable_ids": ["warranty-terms:0"],
        },
    ]
    result, retriever = _run(questions, {questions[0]["question"]: WEAK})
    _check(result.n_questions == 1, f"only the P1a row should be scored, got {result.n_questions}")
    _check(retriever.seen == [questions[0]["question"]], f"only the P1a question retrieved, got {retriever.seen}")
    _check(result.decisions[0].question_id == "e7-p1a-1", "the scored row is the P1a one")
    print("ok: non-P1a rows are ignored by the P1a leg")


def test_deterministic() -> None:
    """Same fake input -> identical scored output (the gate is pure arithmetic, so
    the P1a leg can hard-block per-PR without LLM flakiness, US-059)."""
    questions = [_p1a("e7-p1a-d", "An off-topic question.")]
    rows = {questions[0]["question"]: WEAK}
    first, _ = _run(questions, rows)
    second, _ = _run(questions, rows)
    _check(first.to_dict() == second.to_dict(), "identical inputs must yield identical decisions")
    print("ok: the P1a scoring path is deterministic")


def test_to_dict_shape() -> None:
    """The result + decision JSON carry the audited fields (decision, scores, and
    the pinned 0 draft/judge counters)."""
    questions = [_p1a("e7-p1a-s", "An off-topic question.")]
    result, _ = _run(questions, {questions[0]["question"]: WEAK})
    d = result.to_dict()
    for key in ("population", "label", "tau_sim", "n_min", "match_threshold",
                "n_questions", "total_draft_calls", "total_judge_calls",
                "cleared_gate", "passed", "decisions"):
        _check(key in d, f"result dict missing {key!r}")
    _check(d["population"] == "P1a", f"population must be P1a, got {d['population']!r}")
    _check(d["label"] == "no_context", f"label must be no_context, got {d['label']!r}")
    dec = d["decisions"][0]
    for key in ("question_id", "decision", "expected", "correct", "gate_strong",
                "top1_cosine", "n_cleared", "gate_reason", "n_results",
                "draft_calls", "judge_calls"):
        _check(key in dec, f"decision dict missing {key!r}")
    _check(isinstance(result.decisions[0], P1aDecision), "decisions are P1aDecision instances")
    print("ok: result/decision to_dict carry the audited fields")


# --- P2 (US-053) fixtures + tests -----------------------------------------


class _FakeAnswerer:
    """Call-counting fake `Draft`: maps question text -> canned draft answer.

    Records (question, n_chunks) so a test can prove the drafter saw the
    retrieved chunks. Default draft is non-empty (a normal answer)."""

    def __init__(self, drafts_by_question: dict[str, str] | None = None) -> None:
        self.drafts_by_question = drafts_by_question or {}
        self.calls = 0
        self.seen: list[tuple[str, int]] = []

    async def __call__(self, message: str, chunks: list[SearchDocumentsResult]) -> str:
        self.calls += 1
        self.seen.append((message, len(chunks)))
        return self.drafts_by_question.get(message, "Electronics carry a 12-month warranty.")


class _FakeJudge:
    """Call-counting fake offline `Judge`: maps question -> {faithfulness, helpfulness}.

    Records every call's args (crucially the `reference`, which the runtime
    one-call gate never receives) so a test can prove the OFFLINE judge ran.
    Default score is a faithful 5/5."""

    def __init__(self, scores_by_question: dict[str, dict[str, int]] | None = None) -> None:
        self.scores_by_question = scores_by_question or {}
        self.calls = 0
        self.seen: list[dict] = []

    async def __call__(
        self,
        question: str,
        reference: str,
        chunks: list[SearchDocumentsResult],
        draft_text: str,
    ) -> dict[str, int]:
        self.calls += 1
        self.seen.append(
            {
                "question": question,
                "reference": reference,
                "n_chunks": len(chunks),
                "draft": draft_text,
            }
        )
        return self.scores_by_question.get(question, {"faithfulness": 5, "helpfulness": 5})


def _p2(qid: str, question: str, *, reference: str = "a hand-authored reference") -> dict:
    return {
        "id": qid,
        "escalation": "answerable_faithful",
        "question": question,
        "gold_stable_ids": ["warranty-terms:0"],
        "reference": reference,
    }


def _run_p2(
    questions: list[dict],
    rows_by_question: dict[str, list[SearchDocumentsResult]],
    *,
    drafts_by_question: dict[str, str] | None = None,
    scores_by_question: dict[str, dict[str, int]] | None = None,
    faithfulness_judge_min: int = 4,
) -> tuple[E7P2Result, _FakeRetriever, _FakeAnswerer, _FakeJudge]:
    retriever = _FakeRetriever(rows_by_question)
    answerer = _FakeAnswerer(drafts_by_question)
    judge = _FakeJudge(scores_by_question)
    result = asyncio.run(
        run_e7_p2(
            questions=questions,
            retrieve=retriever,
            draft=answerer,
            judge=judge,
            config=CONFIG,
            match_threshold=THRESH,
            judge_model=JUDGE_MODEL,
            faithfulness_judge_min=faithfulness_judge_min,
        )
    )
    return result, retriever, answerer, judge


def test_p2_auto_resolves_when_faithful() -> None:
    """PRD core: a P2 row with strong retrieval + a faithful draft auto-resolves,
    is counted toward deflection, records the offline faithfulness score, and makes
    exactly 1 draft + 1 judge call."""
    q = _p2("e7-p2-01", "How long is the electronics warranty?")
    result, retriever, answerer, judge = _run_p2(
        [q], {q["question"]: STRONG}, scores_by_question={q["question"]: {"faithfulness": 5, "helpfulness": 4}}
    )

    _check(result.n_questions == 1, f"expected 1 P2 row, got {result.n_questions}")
    _check(retriever.calls == 1 and answerer.calls == 1 and judge.calls == 1,
           f"exactly 1 retrieve/draft/judge call, got {retriever.calls}/{answerer.calls}/{judge.calls}")
    d = result.decisions[0]
    _check(d.decision == "auto_resolve", f"a faithful P2 must auto-resolve, got {d.decision}")
    _check(d.correct is True and d.false_escalate is False, "auto-resolve is the correct, non-false-escalate P2 outcome")
    _check(d.escalate_leg is None, f"auto-resolve has no escalate leg, got {d.escalate_leg!r}")
    _check(d.faithfulness_score == 5, f"the offline faithfulness score must be recorded, got {d.faithfulness_score}")
    _check(d.helpfulness_score == 4, f"the offline helpfulness score must be recorded, got {d.helpfulness_score}")
    _check(d.faithful is True, "score 5 >= floor 4 -> faithful")
    _check(d.draft_calls == 1 and d.judge_calls == 1, "per-row 1 draft + 1 judge")
    _check(result.deflection_rate == 1.0, f"100% deflection, got {result.deflection_rate}")
    _check(result.false_escalate_rate == 0.0, f"0% false-escalate, got {result.false_escalate_rate}")
    _check(result.passed is True, "a non-empty P2 run is structurally valid")
    print("ok: a faithful P2 row auto-resolves, counts as deflection, 1 draft + 1 judge")


def test_p2_uses_offline_judge_with_reference() -> None:
    """Failure indicator: the faithfulness leg must be the OFFLINE judge, not the
    runtime one-call gate. The offline judge receives the hand-authored reference
    (the runtime gate never does) and the drafted answer grounded in the chunks."""
    q = _p2("e7-p2-ref", "How long is the electronics warranty?", reference="12 months from shipped_at.")
    result, _, answerer, judge = _run_p2([q], {q["question"]: STRONG})

    _check(judge.calls == 1, "the offline judge must be invoked once on a strong-retrieval P2")
    seen = judge.seen[0]
    _check(seen["reference"] == "12 months from shipped_at.",
           f"the offline judge must receive the reference, got {seen['reference']!r}")
    _check(seen["question"] == q["question"], "the judge sees the question")
    _check(seen["n_chunks"] == len(STRONG), f"the judge sees the retrieved chunks, got {seen['n_chunks']}")
    _check(isinstance(seen["draft"], str) and bool(seen["draft"]), "the judge scores the drafted answer text")
    _check(answerer.calls == 1, "the real drafter ran exactly once before the judge")
    _check(result.judge_model == JUDGE_MODEL, f"result records the offline judge model, got {result.judge_model!r}")
    print("ok: the P2 faithfulness leg is the offline judge (sees the reference), not the runtime gate")


def test_p2_false_escalate_at_retrieval_gate() -> None:
    """A P2 (answerable) row whose retrieval is weak escalates at the retrieval
    gate -> a false-escalate, with ZERO draft and ZERO judge calls."""
    q = _p2("e7-p2-weak", "How long is the electronics warranty?")
    result, _, answerer, judge = _run_p2([q], {q["question"]: WEAK})

    d = result.decisions[0]
    _check(d.decision == "escalate", f"weak retrieval escalates, got {d.decision}")
    _check(d.false_escalate is True and d.correct is False, "an escalated P2 is a false-escalate")
    _check(d.escalate_leg == "retrieval", f"escalated at the retrieval leg, got {d.escalate_leg!r}")
    _check(d.faithfulness_score is None, "no judge call -> no faithfulness score")
    _check(d.faithful is None, "no judge verdict on the retrieval-gate path")
    _check(d.draft_calls == 0 and d.judge_calls == 0, "weak-gate short-circuit: 0 draft/0 judge")
    _check(answerer.calls == 0 and judge.calls == 0, "no drafter/judge invoked on the weak path")
    _check(result.false_escalate_rate == 1.0 and result.deflection_rate == 0.0, "the rates reflect the false-escalate")
    _check([d.question_id for d in result.false_escalates] == ["e7-p2-weak"], "the row is tallied as a false-escalate")
    print("ok: a weak-retrieval P2 is a false-escalate at the retrieval gate, 0 draft/0 judge")


def test_p2_false_escalate_at_faithfulness_leg() -> None:
    """A P2 row that clears retrieval but whose draft the offline judge scores below
    the floor escalates at the faithfulness leg -> a false-escalate (1 draft, 1 judge)."""
    q = _p2("e7-p2-unfaithful", "How long is the electronics warranty?")
    result, _, answerer, judge = _run_p2(
        [q], {q["question"]: STRONG},
        scores_by_question={q["question"]: {"faithfulness": 2, "helpfulness": 3}},
    )

    d = result.decisions[0]
    _check(d.decision == "escalate", f"a below-floor faithfulness score escalates, got {d.decision}")
    _check(d.false_escalate is True, "an escalated P2 is a false-escalate")
    _check(d.escalate_leg == "faithfulness", f"escalated at the faithfulness leg, got {d.escalate_leg!r}")
    _check(d.faithfulness_score == 2 and d.faithful is False, "the sub-floor score is recorded and judged unfaithful")
    _check(d.draft_calls == 1 and d.judge_calls == 1, "the row was drafted AND judged before escalating")
    _check(answerer.calls == 1 and judge.calls == 1, "exactly one draft + one judge call")
    print("ok: a P2 draft scored below the faithfulness floor is a false-escalate at the faithfulness leg")


def test_p2_empty_draft_escalates_without_judge() -> None:
    """A P2 row whose drafter returns an empty/whitespace answer escalates at the
    draft leg with NO judge call (there is no answer to score)."""
    q = _p2("e7-p2-empty", "How long is the electronics warranty?")
    result, _, answerer, judge = _run_p2(
        [q], {q["question"]: STRONG}, drafts_by_question={q["question"]: "   "}
    )

    d = result.decisions[0]
    _check(d.decision == "escalate", f"an empty draft escalates, got {d.decision}")
    _check(d.escalate_leg == "draft", f"escalated at the draft leg, got {d.escalate_leg!r}")
    _check(d.draft_calls == 1 and d.judge_calls == 0, "drafted once, never judged")
    _check(answerer.calls == 1 and judge.calls == 0, "the judge is not called when there is no draft")
    _check(d.faithfulness_score is None, "no judge -> no faithfulness score")
    print("ok: an empty P2 draft is a false-escalate at the draft leg, no judge call")


def test_p2_faithfulness_floor_is_inclusive() -> None:
    """The faithfulness floor is inclusive: a score exactly at the floor is faithful
    (auto-resolve), one below it is a false-escalate."""
    at = _p2("e7-p2-at", "At-floor question.")
    below = _p2("e7-p2-below", "Below-floor question.")
    result, _, _, _ = _run_p2(
        [at, below],
        {at["question"]: STRONG, below["question"]: STRONG},
        scores_by_question={
            at["question"]: {"faithfulness": 4, "helpfulness": 4},
            below["question"]: {"faithfulness": 3, "helpfulness": 3},
        },
        faithfulness_judge_min=4,
    )
    by_id = {d.question_id: d for d in result.decisions}
    _check(by_id["e7-p2-at"].decision == "auto_resolve", "score == floor is faithful (inclusive)")
    _check(by_id["e7-p2-below"].decision == "escalate", "score < floor is a false-escalate")
    _check(result.deflection_rate == 0.5, f"1 of 2 deflected, got {result.deflection_rate}")
    print("ok: the faithfulness floor is inclusive (>=)")


def test_p2_empty_population_is_not_a_pass() -> None:
    """A run with no P2 rows is structurally blind for deflection — it must NOT
    pass, and non-P2 rows are ignored by the P2 leg."""
    questions = [_p1a("e7-p1a-1", "An off-topic question.")]
    result, retriever, answerer, judge = _run_p2(questions, {})
    _check(result.n_questions == 0, f"no P2 rows expected, got {result.n_questions}")
    _check(retriever.calls == 0 and answerer.calls == 0 and judge.calls == 0, "non-P2 rows are not scored")
    _check(result.passed is False, "a P2 run scoring zero answerable rows must not pass")
    _check(result.deflection_rate is None, "deflection over zero questions is undefined (None)")
    print("ok: an empty P2 population is not a pass (structurally-blind deflection guard)")


def test_p2_to_dict_shape() -> None:
    """The P2 result + decision JSON carry the audited fields (decision, leg,
    offline faithfulness score, rates, call counts)."""
    q = _p2("e7-p2-s", "How long is the electronics warranty?")
    result, _, _, _ = _run_p2([q], {q["question"]: STRONG})
    d = result.to_dict()
    for key in ("population", "label", "tau_sim", "n_min", "match_threshold",
                "faithfulness_judge_min", "judge_model", "n_questions",
                "n_auto_resolved", "n_false_escalate", "deflection_rate",
                "false_escalate_rate", "total_draft_calls", "total_judge_calls",
                "false_escalates", "passed", "decisions"):
        _check(key in d, f"P2 result dict missing {key!r}")
    _check(d["population"] == "P2", f"population must be P2, got {d['population']!r}")
    _check(d["label"] == "answerable_faithful", f"label must be answerable_faithful, got {d['label']!r}")
    dec = d["decisions"][0]
    for key in ("question_id", "decision", "expected", "correct", "false_escalate",
                "escalate_leg", "gate_strong", "top1_cosine", "n_cleared",
                "gate_reason", "faithfulness_score", "helpfulness_score",
                "faithfulness_judge_min", "faithful", "n_results", "draft",
                "draft_calls", "judge_calls"):
        _check(key in dec, f"P2 decision dict missing {key!r}")
    _check(isinstance(result.decisions[0], P2Decision), "decisions are P2Decision instances")
    print("ok: P2 result/decision to_dict carry the audited fields")


# --- P3 (US-054) fixtures + tests -----------------------------------------


def _p3(qid: str, question: str, *, reference: str = "no grounded answer exists; escalate to a human") -> dict:
    """A should_escalate (P3) row: strong-retrieval gold anchor + a should-escalate
    reference (the offline judge's gold, US-054)."""
    return {
        "id": qid,
        "escalation": "should_escalate",
        "question": question,
        "gold_stable_ids": ["warranty-terms:0"],
        "reference": reference,
    }


def _run_p3(
    questions: list[dict],
    rows_by_question: dict[str, list[SearchDocumentsResult]],
    *,
    drafts_by_question: dict[str, str] | None = None,
    scores_by_question: dict[str, dict[str, int]] | None = None,
    faithfulness_judge_min: int = 4,
) -> tuple[E7P3Result, _FakeRetriever, _FakeAnswerer, _FakeJudge]:
    retriever = _FakeRetriever(rows_by_question)
    answerer = _FakeAnswerer(drafts_by_question)
    judge = _FakeJudge(scores_by_question)
    result = asyncio.run(
        run_e7_p3(
            questions=questions,
            retrieve=retriever,
            draft=answerer,
            judge=judge,
            config=CONFIG,
            match_threshold=THRESH,
            judge_model=JUDGE_MODEL,
            faithfulness_judge_min=faithfulness_judge_min,
        )
    )
    return result, retriever, answerer, judge


def test_p3_escalates_at_faithfulness_leg() -> None:
    """PRD core: a P3 row with strong retrieval but an UNFAITHFUL draft escalates
    at the faithfulness leg — the moat working. decision=escalate, correct=True,
    NOT a false-resolve, faithfulness score recorded, exactly 1 draft + 1 judge."""
    q = _p3("e7-p3-01", "What is the warranty period for jewelry?")
    result, retriever, answerer, judge = _run_p3(
        [q], {q["question"]: STRONG},
        scores_by_question={q["question"]: {"faithfulness": 2, "helpfulness": 2}},
    )

    _check(result.n_questions == 1, f"expected 1 P3 row, got {result.n_questions}")
    _check(retriever.calls == 1 and answerer.calls == 1 and judge.calls == 1,
           f"exactly 1 retrieve/draft/judge call, got {retriever.calls}/{answerer.calls}/{judge.calls}")
    d = result.decisions[0]
    _check(d.decision == "escalate", f"an unfaithful P3 must escalate, got {d.decision}")
    _check(d.escalate_leg == "faithfulness", f"escalated at the faithfulness leg, got {d.escalate_leg!r}")
    _check(d.correct is True, "escalating at the faithfulness gate is the correct P3 outcome")
    _check(d.false_resolve is False, "an escalated P3 is NOT a false-resolve")
    _check(d.mislabeled is False, "a faithfulness-leg escalation is not mislabeled")
    _check(d.faithfulness_score == 2 and d.faithful is False, "the sub-floor score is recorded, judged unfaithful")
    _check(d.draft_calls == 1 and d.judge_calls == 1, "drafted AND judged before escalating")
    _check(result.false_resolve_rate == 0.0, f"0% false-resolve, got {result.false_resolve_rate}")
    _check(result.false_resolves == [], "no false-resolves on a correctly-escalated P3")
    _check([x.question_id for x in result.exercised] == ["e7-p3-01"],
           "a faithfulness-leg escalation exercised the gate")
    _check(result.passed is True, "a P3 run that exercised the faithfulness gate is structurally valid")
    print("ok: a P3 with an unfaithful draft escalates at the faithfulness gate (the moat), not a false-resolve")


def test_p3_auto_resolve_is_false_resolve() -> None:
    """Failure indicator: a P3 row whose draft the offline judge scores FAITHFUL
    auto-resolves -> a FALSE-RESOLVE (the Risk #3 safety failure), tallied toward
    the false-resolve rate, flagged correct=False."""
    q = _p3("e7-p3-leak", "What is the warranty period for jewelry?")
    result, _, answerer, judge = _run_p3(
        [q], {q["question"]: STRONG},
        scores_by_question={q["question"]: {"faithfulness": 5, "helpfulness": 2}},
    )

    d = result.decisions[0]
    _check(d.decision == "auto_resolve", f"a faithful draft auto-resolves, got {d.decision}")
    _check(d.false_resolve is True, "an auto-resolved P3 is a false-resolve (the safety failure)")
    _check(d.correct is False, "an auto-resolved P3 is NOT correct")
    _check(d.mislabeled is False, "an auto-resolve is not a mislabeled (pre-faithfulness) escalation")
    _check(d.escalate_leg is None, f"an auto-resolve has no escalate leg, got {d.escalate_leg!r}")
    _check(d.faithfulness_score == 5 and d.faithful is True, "the faithful score is recorded")
    _check(d.draft_calls == 1 and d.judge_calls == 1, "drafted + judged")
    _check([x.question_id for x in result.false_resolves] == ["e7-p3-leak"], "the row is tallied as a false-resolve")
    _check(result.false_resolve_rate == 1.0, f"100% false-resolve, got {result.false_resolve_rate}")
    _check([x.question_id for x in result.exercised] == ["e7-p3-leak"],
           "an auto-resolve reached a faithfulness verdict, so it exercised the gate")
    _check(result.passed is True, "a P3 run that exercised the gate is structurally valid even with a false-resolve (US-055/059 gates the rate)")
    print("ok: a P3 that auto-resolves is caught as a false-resolve (the Risk #3 safety failure)")


def test_p3_retrieval_gate_escalation_is_mislabeled() -> None:
    """Failure indicator: a P3 row whose retrieval is WEAK escalates at the
    retrieval gate — it never exercises the faithfulness gate, so it is
    `mislabeled` (the gold retrieval was not actually strong). It escalated (the
    safe direction), so it is NOT a false-resolve, but it is NOT correct either,
    with ZERO draft and ZERO judge calls."""
    q = _p3("e7-p3-weak", "A should-escalate question whose gold retrieval is weak.")
    result, _, answerer, judge = _run_p3([q], {q["question"]: WEAK})

    d = result.decisions[0]
    _check(d.decision == "escalate", f"weak retrieval escalates, got {d.decision}")
    _check(d.escalate_leg == "retrieval", f"escalated at the retrieval leg, got {d.escalate_leg!r}")
    _check(d.mislabeled is True, "a retrieval-gate escalation never exercised the faithfulness gate -> mislabeled")
    _check(d.correct is False, "a mislabeled P3 is not the correct (faithfulness-leg) outcome")
    _check(d.false_resolve is False, "a mislabeled P3 escalated, so it is NOT a false-resolve")
    _check(d.draft_calls == 0 and d.judge_calls == 0, "weak-gate short-circuit: 0 draft/0 judge")
    _check(answerer.calls == 0 and judge.calls == 0, "no drafter/judge invoked on the weak path")
    _check(result.false_resolve_rate == 0.0, "a mislabeled escalation is not a false-resolve")
    _check([x.question_id for x in result.mislabeled] == ["e7-p3-weak"], "the row is tallied as mislabeled")
    print("ok: a P3 escalating at the retrieval gate is mislabeled (never exercised the faithfulness gate), not a false-resolve")


def test_p3_empty_draft_is_mislabeled() -> None:
    """A P3 row whose drafter returns an empty answer escalates at the draft leg
    with NO judge call — it never reached the faithfulness gate, so it too is
    mislabeled (not a false-resolve)."""
    q = _p3("e7-p3-empty", "What is the warranty period for jewelry?")
    result, _, answerer, judge = _run_p3(
        [q], {q["question"]: STRONG}, drafts_by_question={q["question"]: "   "}
    )

    d = result.decisions[0]
    _check(d.decision == "escalate", f"an empty draft escalates, got {d.decision}")
    _check(d.escalate_leg == "draft", f"escalated at the draft leg, got {d.escalate_leg!r}")
    _check(d.mislabeled is True, "a draft-leg escalation never reached the faithfulness gate -> mislabeled")
    _check(d.correct is False and d.false_resolve is False, "escalated (safe) but not the correct faithfulness-leg outcome")
    _check(d.draft_calls == 1 and d.judge_calls == 0, "drafted once, never judged")
    _check(answerer.calls == 1 and judge.calls == 0, "the judge is not called when there is no draft")
    print("ok: an empty P3 draft escalates at the draft leg and is mislabeled (no judge call)")


def test_p3_uses_offline_judge_with_reference() -> None:
    """The P3 faithfulness leg is the OFFLINE judge: it receives the hand-authored
    should-escalate reference (the runtime one-call gate never sees a reference)."""
    q = _p3("e7-p3-ref", "What is the warranty period for jewelry?",
            reference="The policy does not cover jewelry; escalate to a human.")
    result, _, answerer, judge = _run_p3([q], {q["question"]: STRONG})

    _check(judge.calls == 1, "the offline judge runs once on a strong-retrieval P3")
    seen = judge.seen[0]
    _check(seen["reference"] == "The policy does not cover jewelry; escalate to a human.",
           f"the offline judge must receive the P3 reference, got {seen['reference']!r}")
    _check(seen["question"] == q["question"], "the judge sees the question")
    _check(seen["n_chunks"] == len(STRONG), f"the judge sees the retrieved chunks, got {seen['n_chunks']}")
    _check(result.judge_model == JUDGE_MODEL, f"result records the offline judge model, got {result.judge_model!r}")
    print("ok: the P3 faithfulness leg is the offline judge and receives the should-escalate reference")


def test_p3_faithfulness_floor_inclusive_and_flips_vs_p2() -> None:
    """The faithfulness floor is inclusive, and its verdict FLIPS the P3 outcome
    vs P2: a score AT the floor is faithful -> auto-resolve -> a P3 false-resolve;
    one below it is unfaithful -> escalate at the faithfulness leg -> correct."""
    at = _p3("e7-p3-at", "At-floor question.")
    below = _p3("e7-p3-below", "Below-floor question.")
    result, _, _, _ = _run_p3(
        [at, below],
        {at["question"]: STRONG, below["question"]: STRONG},
        scores_by_question={
            at["question"]: {"faithfulness": 4, "helpfulness": 2},
            below["question"]: {"faithfulness": 3, "helpfulness": 2},
        },
        faithfulness_judge_min=4,
    )
    by_id = {d.question_id: d for d in result.decisions}
    _check(by_id["e7-p3-at"].decision == "auto_resolve", "score == floor is faithful (inclusive) -> auto-resolve")
    _check(by_id["e7-p3-at"].false_resolve is True, "an at-floor (faithful) P3 auto-resolve is a false-resolve")
    _check(by_id["e7-p3-below"].decision == "escalate", "score < floor is unfaithful -> escalate")
    _check(by_id["e7-p3-below"].correct is True, "a below-floor P3 escalates at the faithfulness gate (correct)")
    _check(result.false_resolve_rate == 0.5, f"1 of 2 false-resolved, got {result.false_resolve_rate}")
    print("ok: the faithfulness floor is inclusive and flips the P3 verdict vs P2")


def test_p3_empty_population_is_not_a_pass() -> None:
    """A run with no P3 rows is structurally blind for the false-resolve rate — it
    must NOT pass, and non-P3 rows are ignored by the P3 leg."""
    questions = [_p1a("e7-p1a-1", "An off-topic question."), _p2("e7-p2-1", "warranty?")]
    result, retriever, answerer, judge = _run_p3(questions, {})
    _check(result.n_questions == 0, f"no P3 rows expected, got {result.n_questions}")
    _check(retriever.calls == 0 and answerer.calls == 0 and judge.calls == 0, "non-P3 rows are not scored")
    _check(result.passed is False, "a P3 run scoring zero should-escalate rows must not pass")
    _check(result.false_resolve_rate is None, "the false-resolve rate over zero questions is undefined (None)")
    print("ok: an empty P3 population is not a pass (structurally-blind false-resolve guard)")


def test_p3_all_mislabeled_is_not_a_pass() -> None:
    """Failure indicator: a NON-empty P3 population whose rows ALL escalate before
    the faithfulness gate (here: weak retrieval -> all mislabeled) never exercises
    the gate the ceiling protects. The measured false-resolve rate then reads a
    vacuous 0% (numerator 0 over a non-zero denominator), NOT None — so the positive
    control must key off EXERCISED rows, not n_questions, or the ceiling is silently
    disarmed. `exercised` is empty and `passed` is False, mirroring the empty-P3
    guard."""
    a = _p3("e7-p3-weak-a", "A should-escalate question whose gold retrieval is weak.")
    b = _p3("e7-p3-weak-b", "Another should-escalate question whose retrieval is weak.")
    result, _, answerer, judge = _run_p3(
        [a, b], {a["question"]: WEAK, b["question"]: WEAK}
    )
    _check(result.n_questions == 2, f"two P3 rows scored, got {result.n_questions}")
    _check(all(d.mislabeled for d in result.decisions), "both rows escalated at the retrieval gate -> mislabeled")
    _check(result.exercised == [], "no P3 row reached the faithfulness gate -> exercised is empty")
    _check(result.false_resolve_rate == 0.0, "an all-mislabeled leg reads a vacuous MEASURED 0% (not None)")
    _check(answerer.calls == 0 and judge.calls == 0, "the faithfulness gate was never exercised (0 draft/0 judge)")
    _check(result.passed is False,
           "an all-mislabeled P3 leg never exercises the gate, so it must NOT pass the positive control")
    print("ok: an all-mislabeled P3 population is not a pass (the faithfulness gate was never exercised)")


def test_p3_to_dict_shape() -> None:
    """The P3 result + decision JSON carry the audited fields (decision, the three
    taxonomy flags, leg, offline faithfulness score, the false-resolve rate, call
    counts)."""
    q = _p3("e7-p3-s", "What is the warranty period for jewelry?")
    result, _, _, _ = _run_p3(
        [q], {q["question"]: STRONG},
        scores_by_question={q["question"]: {"faithfulness": 2, "helpfulness": 2}},
    )
    d = result.to_dict()
    for key in ("population", "label", "tau_sim", "n_min", "match_threshold",
                "faithfulness_judge_min", "judge_model", "n_questions",
                "n_escalated_at_faithfulness", "n_false_resolve", "n_mislabeled",
                "mislabel_ratio", "false_resolve_rate", "total_draft_calls",
                "total_judge_calls", "false_resolves", "mislabeled", "passed",
                "decisions"):
        _check(key in d, f"P3 result dict missing {key!r}")
    _check(d["mislabel_ratio"] == 0.0,
           f"a fully-exercised P3 leg has 0 mislabel_ratio, got {d['mislabel_ratio']}")
    _check(d["population"] == "P3", f"population must be P3, got {d['population']!r}")
    _check(d["label"] == "should_escalate", f"label must be should_escalate, got {d['label']!r}")
    dec = d["decisions"][0]
    for key in ("question_id", "decision", "expected", "correct", "false_resolve",
                "mislabeled", "escalate_leg", "gate_strong", "top1_cosine",
                "n_cleared", "gate_reason", "faithfulness_score", "helpfulness_score",
                "faithfulness_judge_min", "faithful", "n_results", "draft",
                "draft_calls", "judge_calls"):
        _check(key in dec, f"P3 decision dict missing {key!r}")
    _check(dec["expected"] == "escalate", f"P3's expected outcome is escalate, got {dec['expected']!r}")
    _check(isinstance(result.decisions[0], P3Decision), "decisions are P3Decision instances")
    print("ok: P3 result/decision to_dict carry the audited fields")


# --- US-055 consolidated metrics fixtures + tests -------------------------


def _p1a_result(rows_by_question: dict[str, list[SearchDocumentsResult]], questions: list[dict]) -> E7P1aResult:
    result, _ = _run(questions, rows_by_question)
    return result


def _mixed_results() -> tuple[E7P1aResult, E7P2Result, E7P3Result]:
    """The PRD US-055 mix: a known P1a/P2/P3 set yielding exactly one wrong
    auto-resolve (a P3 false-resolve) and one wrong escalate (a P2 false-escalate),
    with all other rows correct.

      * P1a: 2 rows, both WEAK -> both correctly escalate (0 cleared the gate).
      * P2:  2 rows STRONG; one faithful (auto-resolve, correct) + one below-floor
        (escalate -> the single false-escalate).
      * P3:  2 rows STRONG; one below-floor (escalate at faithfulness, correct) +
        one faithful (auto-resolve -> the single false-resolve).

    Hand-computed consolidated rates: deflection 1/2, false-escalate 1/2,
    false-resolve 1/2 (the gated faithfulness leg: 1 P3 auto-resolve over 2 P3 rows;
    the P1a retrieval-leg contribution is monitor-only, 0 cleared, excluded from the
    rate).
    """
    p1a = _p1a_result(
        {"q-p1a-a": WEAK, "q-p1a-b": WEAK},
        [_p1a("m-p1a-a", "q-p1a-a"), _p1a("m-p1a-b", "q-p1a-b")],
    )
    p2, _, _, _ = _run_p2(
        [_p2("m-p2-resolve", "q-p2-r"), _p2("m-p2-escalate", "q-p2-e")],
        {"q-p2-r": STRONG, "q-p2-e": STRONG},
        scores_by_question={
            "q-p2-r": {"faithfulness": 5, "helpfulness": 5},
            "q-p2-e": {"faithfulness": 2, "helpfulness": 2},
        },
    )
    p3, _, _, _ = _run_p3(
        [_p3("m-p3-escalate", "q-p3-e"), _p3("m-p3-resolve", "q-p3-r")],
        {"q-p3-e": STRONG, "q-p3-r": STRONG},
        scores_by_question={
            "q-p3-e": {"faithfulness": 2, "helpfulness": 2},
            "q-p3-r": {"faithfulness": 5, "helpfulness": 5},
        },
    )
    return p1a, p2, p3


def test_metrics_consolidate_three_rates() -> None:
    """PRD core: a known P1a/P2/P3 mix with one wrong auto-resolve + one wrong
    escalate -> the three consolidated rates match the hand-computed
    numerator/denominator exactly, and the false-resolve count includes the wrong
    auto-resolve."""
    p1a, p2, p3 = _mixed_results()
    m = compute_e7_metrics(p1a, p2, p3)

    _check(isinstance(m, E7Metrics), "compute_e7_metrics returns an E7Metrics")

    _check(m.deflection.numerator == 1 and m.deflection.denominator == 2,
           f"deflection 1/2 expected, got {m.deflection.numerator}/{m.deflection.denominator}")
    _check(m.deflection.rate == 0.5, f"deflection rate 0.5 expected, got {m.deflection.rate}")
    _check(m.deflection.safety is False, "deflection is a tunable quality metric, not the safety one")

    _check(m.false_escalate.numerator == 1 and m.false_escalate.denominator == 2,
           f"false-escalate 1/2 expected, got {m.false_escalate.numerator}/{m.false_escalate.denominator}")
    _check(m.false_escalate.rate == 0.5, f"false-escalate rate 0.5 expected, got {m.false_escalate.rate}")
    _check(m.false_escalate.safety is False, "false-escalate is a tunable quality metric")

    _check(m.false_resolve.numerator == 1 and m.false_resolve.denominator == 2,
           f"false-resolve 1/2 expected (gated faithfulness leg: 1 P3 auto-resolve "
           f"over 2 P3; P1a monitor-only), got "
           f"{m.false_resolve.numerator}/{m.false_resolve.denominator}")
    _check(m.false_resolve.rate == 0.5, f"false-resolve rate 0.5 expected, got {m.false_resolve.rate}")
    _check(m.false_resolve.safety is True, "false-resolve is the pinned safety metric (US-059)")
    print("ok: the three consolidated rates match the hand-computed numerator/denominator")


def test_metrics_false_resolve_is_faithfulness_leg_gated() -> None:
    """Failure indicator: the ceiling-gated false-resolve number is the
    FAITHFULNESS-leg (P3) rate — the population where a false-resolve can occur once
    a draft clears the retrieval gate. A P1a row that CLEARED the gate (a
    retrieval-leg false-resolve) is carried monitor-only in the breakdown and
    hard-fails the P1a invariant UNCONDITIONALLY, but it must NOT dilute (nor feed)
    the gated rate (US-059)."""
    # P1a: one row clears the gate (STRONG -> retrieval-leg false-resolve) + one weak.
    p1a = _p1a_result(
        {"q-clear": STRONG, "q-weak": WEAK},
        [_p1a("m-p1a-clear", "q-clear"), _p1a("m-p1a-weak", "q-weak")],
    )
    # P3: one auto-resolves (faithfulness-leg false-resolve) + one escalates (correct).
    p3, _, _, _ = _run_p3(
        [_p3("m-p3-resolve", "q3r"), _p3("m-p3-escalate", "q3e")],
        {"q3r": STRONG, "q3e": STRONG},
        scores_by_question={
            "q3r": {"faithfulness": 5, "helpfulness": 5},
            "q3e": {"faithfulness": 2, "helpfulness": 2},
        },
    )
    m = compute_e7_metrics(p1a, None, p3)

    # The gated rate is the P3 faithfulness leg alone (1 auto-resolve / 2 P3 rows) —
    # the P1a leak does NOT inflate the denominator nor the numerator of the rate.
    _check(m.false_resolve.numerator == 1 and m.false_resolve.denominator == 2,
           f"gated false-resolve 1/2 expected (P3 only), got "
           f"{m.false_resolve.numerator}/{m.false_resolve.denominator}")
    _check(m.false_resolve.rate == 0.5, f"gated false-resolve rate 0.5 expected, got {m.false_resolve.rate}")
    by_pop = {c.population: (c.numerator, c.denominator, c.counts_toward_rate)
              for c in m.false_resolve.by_population}
    _check(by_pop == {"P1a": (1, 2, False), "P3": (1, 2, True)},
           f"P1a is surfaced monitor-only and P3 is the gated leg, got {by_pop}")
    # The retrieval-leg leak still hard-fails unconditionally via the P1a invariant.
    _check(p1a.passed is False,
           "a P1a row that cleared the gate fails the P1a invariant regardless of rate")
    print("ok: the gated false-resolve is the P3 faithfulness leg; the P1a leak is monitor-only + hard-fails separately")


def test_metrics_blind_when_leg_absent() -> None:
    """Failure indicator: a rate over an empty rate-bearing population is None
    (blind), never a false 0.0 — so an opt-out P2/P3 leg cannot masquerade as a
    passing 0. With no P3 faithfulness leg (the per-PR P1a-only shape) the gated
    false-resolve rate is NOT measured (None); the P1a contribution is carried
    monitor-only and cannot fabricate a passing 0."""
    # P1a-only run (the per-PR deterministic tripwire shape): no P2, no P3.
    p1a = _p1a_result(
        {"q-a": WEAK, "q-b": WEAK},
        [_p1a("m-p1a-a", "q-a"), _p1a("m-p1a-b", "q-b")],
    )
    m = compute_e7_metrics(p1a, None, None)

    _check(m.deflection.rate is None, f"deflection over 0 answerable is None, got {m.deflection.rate}")
    _check(m.deflection.denominator == 0 and m.deflection.by_population == [],
           "an absent P2 leg contributes nothing to deflection")
    _check(m.false_escalate.rate is None, f"false-escalate over 0 answerable is None, got {m.false_escalate.rate}")

    # With no P3 faithfulness leg the gated safety number is not measured (None) —
    # the P1a contribution is monitor-only, so it does not become a passing 0.
    _check(m.false_resolve.rate is None,
           f"a P1a-only run has no gated faithfulness leg -> None, got {m.false_resolve.rate}")
    _check(m.false_resolve.numerator == 0 and m.false_resolve.denominator == 0,
           f"the gated n/d excludes the monitor-only P1a, got "
           f"{m.false_resolve.numerator}/{m.false_resolve.denominator}")
    p1a_contrib = [c for c in m.false_resolve.by_population if c.population == "P1a"]
    _check(len(p1a_contrib) == 1 and p1a_contrib[0].counts_toward_rate is False,
           "the P1a contribution is surfaced for monitoring but excluded from the rate")
    print("ok: rates over empty rate-bearing populations are None (blind); P1a is monitor-only")


def test_metrics_to_dict_shape() -> None:
    """The consolidated-metrics JSON exposes each rate's numerator/denominator,
    per-population breakdown, and safety flag — so the false-resolve number is
    auditable against the buyer's ceiling (US-059) and never hidden behind a bare
    float."""
    p1a, p2, p3 = _mixed_results()
    d = compute_e7_metrics(p1a, p2, p3).to_dict()

    for name in ("deflection", "false_resolve", "false_escalate"):
        _check(name in d, f"metrics dict missing {name!r}")
        rate = d[name]
        for key in ("name", "rate", "numerator", "denominator", "safety", "by_population"):
            _check(key in rate, f"{name} rate dict missing {key!r}")
        _check(rate["name"] == name, f"{name} rate carries its own name, got {rate['name']!r}")
        for c in rate["by_population"]:
            for key in ("population", "numerator", "denominator", "counts_toward_rate"):
                _check(key in c, f"{name} by_population entry missing {key!r}")

    _check(d["false_resolve"]["safety"] is True, "false-resolve is flagged as the pinned safety metric")
    _check(d["deflection"]["safety"] is False and d["false_escalate"]["safety"] is False,
           "deflection/false-escalate are tunable quality metrics, not safety-pinned")
    fr_pops = {c["population"] for c in d["false_resolve"]["by_population"]}
    _check(fr_pops == {"P1a", "P3"}, f"false-resolve breakdown spans P1a (monitor) + P3 (gated), got {fr_pops}")
    fr_gated = {c["population"]: c["counts_toward_rate"] for c in d["false_resolve"]["by_population"]}
    _check(fr_gated == {"P1a": False, "P3": True},
           f"P1a is monitor-only and P3 is the gated leg, got {fr_gated}")
    _check(d["false_resolve"]["numerator"] == 1 and d["false_resolve"]["denominator"] == 2,
           "the gated headline n/d is the P3 leg alone (1/2), excluding the monitor-only P1a")
    print("ok: the consolidated-metrics to_dict exposes n/d, per-population breakdown, and the safety flag")


# --- US-059 false-resolve ceiling gate tests ------------------------------


def test_ceiling_breach_when_rate_exceeds_ceiling() -> None:
    """US-059 PRD core: a measured faithfulness-leg false-resolve rate ABOVE the
    buyer's ceiling is a breach (the gate fails the run). The _mixed_results mix
    has gated false-resolve 1/2 = 50%; a 5% ceiling is breached, a 50%+ ceiling is
    not."""
    p1a, p2, p3 = _mixed_results()
    m = compute_e7_metrics(p1a, p2, p3)
    _check(m.false_resolve.rate == 0.5, f"fixture gated false-resolve is 50% (P3 leg), got {m.false_resolve.rate}")

    breach = assert_false_resolve_ceiling(m, 0.05)
    _check(breach.breached is True, "50% false-resolve breaches a 5% ceiling")
    _check(breach.passed is False, "a breach does not pass")
    _check(breach.numerator == 1 and breach.denominator == 2,
           f"the verdict echoes the gated metric n/d 1/2, got {breach.numerator}/{breach.denominator}")
    _check(breach.rate == 0.5 and breach.ceiling == 0.05, "verdict carries the measured rate + ceiling")
    print("ok: a false-resolve rate above the ceiling is a breach that fails the run")


def test_ceiling_within_when_rate_at_or_below_ceiling() -> None:
    """Failure indicator: equality is feasible (rate == ceiling does NOT breach),
    matching the knee's `false_resolve <= ceiling` feasibility — and a rate strictly
    below the ceiling passes."""
    p1a, p2, p3 = _mixed_results()  # gated false-resolve 1/2 = 50%
    m = compute_e7_metrics(p1a, p2, p3)

    at = assert_false_resolve_ceiling(m, 0.50)
    _check(at.breached is False, "rate == ceiling (50% vs 50%) is feasible, not a breach")
    _check(at.passed is True, "at-the-ceiling passes")

    below = assert_false_resolve_ceiling(m, 0.75)
    _check(below.breached is False and below.passed is True, "50% under a 75% ceiling passes")
    print("ok: rate at-or-below the ceiling passes (equality is feasible)")


def test_ceiling_blind_rate_is_not_a_breach() -> None:
    """Failure indicator: a None rate (no P3 faithfulness-leg rows scored) is 'not
    measured', NOT a breach — the per-leg blindness guards own that failure, so the
    ceiling gate must not fabricate a pass-or-fail from an empty denominator."""
    # No P3 faithfulness leg -> gated false-resolve denominator 0 -> rate None.
    p1a = _p1a_result({}, [])
    m = compute_e7_metrics(p1a, None, None)
    _check(m.false_resolve.rate is None, f"no gated faithfulness leg yields a None rate, got {m.false_resolve.rate}")

    verdict = assert_false_resolve_ceiling(m, 0.0)
    _check(verdict.breached is False, "a None rate is not a breach even under a 0% ceiling")
    _check(verdict.rate is None and verdict.denominator == 0, "the verdict surfaces the blind n/d")
    print("ok: a blind (None) false-resolve rate is not a breach")


def test_ceiling_inert_on_passing_per_pr_shape() -> None:
    """US-059: the per-PR tripwire shape (P1a/P1b only, no P3 faithfulness leg)
    leaves the gated false-resolve rate UNMEASURED (None), so the ceiling gate is
    structurally inert per-PR — it cannot red-bar a merge when the deterministic
    gates pass (it has teeth only once the weekly P3 faithfulness leg feeds the
    rate; the retrieval-leg P1a/P1b false-resolves are pinned separately)."""
    # Two weak P1a rows: both correctly escalate, 0 cleared the gate; no P3 leg.
    p1a = _p1a_result(
        {"q-a": WEAK, "q-b": WEAK},
        [_p1a("m-p1a-a", "q-a"), _p1a("m-p1a-b", "q-b")],
    )
    m = compute_e7_metrics(p1a, None, None)
    _check(m.false_resolve.rate is None,
           f"a P1a-only run has no gated faithfulness leg -> None, got {m.false_resolve.rate}")

    # A None (unmeasured) rate is never a breach, even at the strictest 0% ceiling.
    verdict = assert_false_resolve_ceiling(m, 0.0)
    _check(verdict.breached is False, "an unmeasured (None) false-resolve never breaches, even at a 0% ceiling")
    print("ok: the ceiling gate is inert on a per-PR (P1a-only, no P3) run")


def test_ceiling_verdict_to_dict_and_render() -> None:
    """The ceiling verdict's JSON exposes ceiling/rate/n/d/breached/passed so the
    weekly workflow can file a precise issue, and the render block names the breach."""
    p1a, p2, p3 = _mixed_results()
    m = compute_e7_metrics(p1a, p2, p3)
    verdict = assert_false_resolve_ceiling(m, 0.05)

    d = verdict.to_dict()
    for key in ("ceiling", "numerator", "denominator", "rate", "breached", "passed"):
        _check(key in d, f"ceiling verdict dict missing {key!r}")
    _check(d["breached"] is True and d["passed"] is False, "to_dict reflects the breach")
    _check(d["rate"] == 0.5 and d["ceiling"] == 0.05, "to_dict carries the measured rate + ceiling")

    lines = render_e7_false_resolve_ceiling_section(verdict)
    body = "\n".join(lines)
    _check("false-resolve ceiling gate" in body.lower(), "render names the ceiling gate")
    _check("BREACH" in body, "render flags the breach verdict")
    print("ok: the ceiling verdict to_dict + render carry the audited fields")


# --- US-059 runner exit-code decision fixtures + tests --------------------


def _clean_p1a() -> E7P1aResult:
    """A passing P1a leg: two weak no-context rows that both correctly escalate
    (0 cleared the gate), so the deterministic per-PR invariant is green."""
    return _p1a_result(
        {"q-a": WEAK, "q-b": WEAK},
        [_p1a("m-p1a-a", "q-a"), _p1a("m-p1a-b", "q-b")],
    )


def _verdict_over(p1a: E7P1aResult, p3: E7P3Result | None, ceiling: float):
    """The ceiling verdict the runner folds into its exit code, built from the real
    consolidated metric over the given legs (no P2/P1b)."""
    return assert_false_resolve_ceiling(compute_e7_metrics(p1a, None, p3), ceiling)


def test_exit_p3_blind_hard_fails() -> None:
    """US-059 (this fix): a requested-but-blind P3 faithfulness leg HARD-FAILS the
    weekly run. The ceiling is now fed SOLELY by P3, so a blind P3 population (gold
    drift) leaves the rate None — `assert_false_resolve_ceiling` is inert (no
    breach) — and WITHOUT this guard the run would exit 0 with the pinned safety
    invariant silently unmeasured. The P3 positive control (passed=False over 0 rows)
    must fail the run closed, mirroring the P1a/P1b/non-disclosure blindness guards."""
    p1a = _clean_p1a()
    p3_blind, _, _, _ = _run_p3([], {})
    _check(p3_blind.n_questions == 0 and p3_blind.passed is False,
           "the P3 leg is structurally blind (0 rows, not a pass)")
    verdict = _verdict_over(p1a, p3_blind, 0.0)
    _check(verdict.breached is False and verdict.rate is None,
           "a blind P3 leaves the ceiling unmeasured (no breach) — the gap this guard closes")

    failed = e7_pinned_invariants_failed(
        p1a_result=p1a, p1b_result=None, non_disclosure=None,
        p3_result=p3_blind, ceiling_verdict=verdict,
    )
    _check(failed is True, "a requested-but-blind P3 leg must hard-fail the run (fail closed)")
    print("ok: a structurally-blind P3 faithfulness leg fails the run closed (US-059 safety guard)")


def test_exit_p3_all_mislabeled_hard_fails() -> None:
    """US-059 (this fix): a NON-empty P3 leg whose rows ALL escalate before the
    faithfulness gate hard-fails the weekly run, closing the second silent-disarm
    path the empty-P3 guard misses. With all rows mislabeled (weak retrieval here),
    `false_resolves == []`, so the ceiling reads a vacuous MEASURED 0% — `breached`
    is False and `assert_false_resolve_ceiling` is inert. WITHOUT the exercised-row
    positive control the run would exit 0 having never touched the faithfulness gate
    the ceiling exists to protect. `E7P3Result.passed` (False when 0 rows exercised)
    must fail the run closed, exactly like the empty-P3 guard."""
    p1a = _clean_p1a()
    a = _p3("m-p3-weak-a", "q-weak-a")
    b = _p3("m-p3-weak-b", "q-weak-b")
    p3_mislabeled, _, _, _ = _run_p3(
        [a, b], {a["question"]: WEAK, b["question"]: WEAK}
    )
    _check(p3_mislabeled.n_questions == 2 and p3_mislabeled.passed is False,
           "a non-empty but all-mislabeled P3 leg never exercises the gate (not a pass)")
    _check(p3_mislabeled.exercised == [], "no P3 row reached the faithfulness gate")
    verdict = _verdict_over(p1a, p3_mislabeled, 0.0)
    _check(verdict.breached is False and verdict.rate == 0.0,
           "an all-mislabeled P3 reads a vacuous measured 0% (no breach) — the gap this guard closes")

    failed = e7_pinned_invariants_failed(
        p1a_result=p1a, p1b_result=None, non_disclosure=None,
        p3_result=p3_mislabeled, ceiling_verdict=verdict,
    )
    _check(failed is True, "an all-mislabeled P3 leg must hard-fail the run (fail closed)")
    print("ok: an all-mislabeled P3 faithfulness leg fails the run closed (US-059 safety guard)")


def test_exit_clean_weekly_does_not_fail() -> None:
    """Control: a clean weekly shape — passing P1a, a non-empty P3 that correctly
    escalates (no false-resolve), ceiling not breached — exits 0. The new P3
    positive-control guard only fires on a BLIND P3, never on a healthy one."""
    p1a = _clean_p1a()
    q = _p3("m-p3-e", "q-e")
    p3, _, _, _ = _run_p3(
        [q], {q["question"]: STRONG},
        scores_by_question={q["question"]: {"faithfulness": 2, "helpfulness": 2}},
    )
    _check(p3.passed is True and p3.false_resolves == [],
           "the P3 leg scored a row and correctly escalated (no false-resolve)")
    verdict = _verdict_over(p1a, p3, 0.05)
    _check(verdict.breached is False, "0% measured false-resolve does not breach a 5% ceiling")

    failed = e7_pinned_invariants_failed(
        p1a_result=p1a, p1b_result=None, non_disclosure=None,
        p3_result=p3, ceiling_verdict=verdict,
    )
    _check(failed is False, "a clean weekly run (passing P1a + healthy P3, no breach) exits 0")
    print("ok: a clean weekly run does not fail on the P3 positive-control guard")


def test_exit_per_pr_shape_without_p3_does_not_fail() -> None:
    """The per-PR tripwire shape (P1a/P1b only, no P3 leg requested) must NOT trip
    the P3 positive-control guard — p3_result is None, so a healthy deterministic run
    still exits 0. The guard fires only on a REQUESTED-but-blind P3 leg, never on its
    legitimate absence per-PR."""
    p1a = _clean_p1a()
    verdict = _verdict_over(p1a, None, 0.0)
    _check(verdict.rate is None and verdict.breached is False,
           "no P3 leg -> unmeasured rate, inert ceiling (the per-PR shape)")

    failed = e7_pinned_invariants_failed(
        p1a_result=p1a, p1b_result=None, non_disclosure=None,
        p3_result=None, ceiling_verdict=verdict,
    )
    _check(failed is False, "a per-PR run with no P3 leg must not fail on the P3 guard")
    print("ok: a per-PR run (no P3 leg) does not trip the P3 positive-control guard")


def test_exit_measured_ceiling_breach_fails() -> None:
    """Regression through the extracted decision: a MEASURED faithfulness-leg
    false-resolve rate above the ceiling still hard-fails the run."""
    p1a, p2, p3 = _mixed_results()  # gated false-resolve 1/2 = 50%
    verdict = assert_false_resolve_ceiling(compute_e7_metrics(p1a, p2, p3), 0.05)
    _check(verdict.breached is True, "50% measured false-resolve breaches a 5% ceiling")

    failed = e7_pinned_invariants_failed(
        p1a_result=p1a, p1b_result=None, non_disclosure=None,
        p3_result=p3, ceiling_verdict=verdict,
    )
    _check(failed is True, "a measured false-resolve rate above the ceiling fails the run")
    print("ok: a measured false-resolve ceiling breach fails the run")


def test_exit_p1a_gate_clear_fails() -> None:
    """Regression through the extracted decision: a P1a no-context row that clears
    the retrieval gate (a retrieval-leg false-resolve) still hard-fails the run
    unconditionally, independent of the P3 ceiling."""
    p1a = _p1a_result(
        {"q-ok": WEAK, "q-leak": STRONG},
        [_p1a("m-ok", "q-ok"), _p1a("m-leak", "q-leak")],
    )
    _check(p1a.passed is False and len(p1a.cleared_gate) == 1, "one P1a row cleared the gate")
    verdict = _verdict_over(p1a, None, 0.0)  # no P3 leg -> ceiling inert

    failed = e7_pinned_invariants_failed(
        p1a_result=p1a, p1b_result=None, non_disclosure=None,
        p3_result=None, ceiling_verdict=verdict,
    )
    _check(failed is True, "a P1a gate clear hard-fails the run regardless of the P3 ceiling")
    print("ok: a P1a retrieval-gate clear still hard-fails through the extracted decision")


# --- Issue #26: P3 mislabel-ratio guard (partial-dilution complement) -------


def test_p3_mislabel_ratio_property() -> None:
    """Issue #26: `mislabel_ratio` is mislabeled / full presented population (the
    same denominator as `false_resolve_rate`, NOT exercised-only), and `None` over an
    empty population. Here 2 of 3 rows escalate at the retrieval gate (mislabeled) and
    1 exercises the faithfulness gate -> 2/3."""
    weak_a = _p3("e7-p3-mr-a", "q-weak-a")
    weak_b = _p3("e7-p3-mr-b", "q-weak-b")
    strong = _p3("e7-p3-mr-c", "q-strong-c")
    result, _, _, _ = _run_p3(
        [weak_a, weak_b, strong],
        {weak_a["question"]: WEAK, weak_b["question"]: WEAK, strong["question"]: STRONG},
        scores_by_question={strong["question"]: {"faithfulness": 2, "helpfulness": 2}},
    )
    _check(result.n_questions == 3, f"3 P3 rows, got {result.n_questions}")
    _check(len(result.mislabeled) == 2 and len(result.exercised) == 1,
           "2 mislabeled (weak) + 1 exercised (strong, escalated at faithfulness)")
    _check(abs(result.mislabel_ratio - 2 / 3) < 1e-9,
           f"mislabel_ratio is 2/3 over the full population, got {result.mislabel_ratio}")
    _check(result.false_resolve_rate == 0.0,
           "no false-resolve, and the mislabeled rows stay in the rate's denominator")

    empty, _, _, _ = _run_p3([], {})
    _check(empty.mislabel_ratio is None, "mislabel_ratio over an empty population is None")
    print("ok: mislabel_ratio is mislabeled/full-population (None when empty)")


def test_exit_p3_partial_mislabel_over_max_fails() -> None:
    """Issue #26 core: a HEAVILY but not entirely mislabeled P3 leg hard-fails the
    weekly run. 2 of 3 rows are mislabeled (2/3 ≈ 67% > the 0.5 max) while 1 row
    exercises the gate, so `passed` is True (the positive control does NOT fire) and,
    with 0 false-resolves, the ceiling is NOT breached — the mislabel-ratio guard is
    therefore the SOLE reason the run fails, exactly the partial-dilution gap the
    issue closes."""
    p1a = _clean_p1a()
    weak_a = _p3("e7-p3-pm-a", "q-weak-a")
    weak_b = _p3("e7-p3-pm-b", "q-weak-b")
    strong = _p3("e7-p3-pm-c", "q-strong-c")
    p3, _, _, _ = _run_p3(
        [weak_a, weak_b, strong],
        {weak_a["question"]: WEAK, weak_b["question"]: WEAK, strong["question"]: STRONG},
        scores_by_question={strong["question"]: {"faithfulness": 2, "helpfulness": 2}},
    )
    _check(p3.passed is True, "≥1 row exercised the gate, so the positive control passes")
    _check(p3.false_resolves == [], "no false-resolve, so the ceiling itself is not breached")
    verdict = _verdict_over(p1a, p3, 0.05)
    _check(verdict.breached is False, "0% measured false-resolve does not breach a 5% ceiling")

    failed = e7_pinned_invariants_failed(
        p1a_result=p1a, p1b_result=None, non_disclosure=None,
        p3_result=p3, ceiling_verdict=verdict, p3_mislabel_ratio_max=0.5,
    )
    _check(failed is True,
           "a partially-but-heavily mislabeled P3 leg (2/3 > 0.5) must fail the run")
    print("ok: a partial-mislabel P3 leg over the ratio max fails the run (issue #26 guard)")


def test_exit_p3_mislabel_ratio_boundary_and_configurable() -> None:
    """Issue #26: the guard fires only when the ratio STRICTLY exceeds the max, and
    the max is configurable. A 1-mislabeled-of-2 leg (ratio exactly 0.5) does NOT trip
    a 0.5 max (`>`, not `>=`), but DOES trip a stricter 0.4 max — the same leg, two
    thresholds."""
    p1a = _clean_p1a()
    weak = _p3("e7-p3-bd-a", "q-weak-a")
    strong = _p3("e7-p3-bd-b", "q-strong-b")
    p3, _, _, _ = _run_p3(
        [weak, strong],
        {weak["question"]: WEAK, strong["question"]: STRONG},
        scores_by_question={strong["question"]: {"faithfulness": 2, "helpfulness": 2}},
    )
    _check(p3.passed is True and p3.mislabel_ratio == 0.5,
           f"1 of 2 rows mislabeled -> ratio 0.5, passed; got {p3.mislabel_ratio}")
    verdict = _verdict_over(p1a, p3, 0.05)

    at_max = e7_pinned_invariants_failed(
        p1a_result=p1a, p1b_result=None, non_disclosure=None,
        p3_result=p3, ceiling_verdict=verdict, p3_mislabel_ratio_max=0.5,
    )
    _check(at_max is False, "ratio exactly at the max does not fire (the bar is 'exceeds', strict >)")

    stricter = e7_pinned_invariants_failed(
        p1a_result=p1a, p1b_result=None, non_disclosure=None,
        p3_result=p3, ceiling_verdict=verdict, p3_mislabel_ratio_max=0.4,
    )
    _check(stricter is True, "the same 0.5-ratio leg fails under a stricter 0.4 max (configurable)")
    print("ok: the mislabel-ratio guard is strict-> and configurable (0.5 passes 0.5, fails 0.4)")


def test_exit_p3_all_mislabeled_owned_by_positive_control_not_ratio_guard() -> None:
    """Issue #26: the empty / all-mislabeled cases stay owned by the positive control
    (one clear failure reason). An all-mislabeled leg has `passed=False`, so the
    ratio guard is gated OFF for it — the run still fails (via the positive control),
    and lowering the ratio guard's threshold to 0 cannot change that outcome, proving
    the two guards partition the space rather than double-decide it."""
    p1a = _clean_p1a()
    a = _p3("e7-p3-am-a", "q-weak-a")
    b = _p3("e7-p3-am-b", "q-weak-b")
    p3, _, _, _ = _run_p3([a, b], {a["question"]: WEAK, b["question"]: WEAK})
    _check(p3.passed is False and p3.exercised == [],
           "an all-mislabeled leg never exercises the gate (positive control owns it)")
    verdict = _verdict_over(p1a, p3, 0.05)

    failed = e7_pinned_invariants_failed(
        p1a_result=p1a, p1b_result=None, non_disclosure=None,
        p3_result=p3, ceiling_verdict=verdict, p3_mislabel_ratio_max=0.99,
    )
    _check(failed is True,
           "an all-mislabeled leg fails via the positive control even at a lax 0.99 ratio max")
    print("ok: all-mislabeled stays owned by the positive control, not the ratio guard")


def test_get_p3_mislabel_ratio_max_env() -> None:
    """Issue #26: the ratio max resolves from E7_P3_MISLABEL_RATIO_MAX with a default
    fallback, and fails CLOSED (ValueError) on an unparseable or out-of-range value —
    a misconfigured safety ceiling must never read as 'no ceiling'."""
    saved = os.environ.get("E7_P3_MISLABEL_RATIO_MAX")
    try:
        os.environ.pop("E7_P3_MISLABEL_RATIO_MAX", None)
        _check(get_p3_mislabel_ratio_max() == DEFAULT_P3_MISLABEL_RATIO_MAX,
               "unset env falls back to the default")

        os.environ["E7_P3_MISLABEL_RATIO_MAX"] = "0.3"
        _check(get_p3_mislabel_ratio_max() == 0.3, "a valid env value is honored")

        for bad in ("1.5", "-0.1", "abc", "nan"):
            os.environ["E7_P3_MISLABEL_RATIO_MAX"] = bad
            raised = False
            try:
                get_p3_mislabel_ratio_max()
            except ValueError:
                raised = True
            _check(raised, f"a bad ratio max {bad!r} must fail closed (ValueError)")
    finally:
        if saved is None:
            os.environ.pop("E7_P3_MISLABEL_RATIO_MAX", None)
        else:
            os.environ["E7_P3_MISLABEL_RATIO_MAX"] = saved
    print("ok: get_p3_mislabel_ratio_max honors env + default and fails closed on bad input")


# --- US-056 knob sweep + knee fixtures + tests ----------------------------


def _run_sweep(
    questions: list[dict],
    rows_by_question: dict[str, list[SearchDocumentsResult]],
    *,
    tau_sims: list[float],
    n_mins: list[int],
    faithfulness_mins: list[int],
    ceiling: float,
    scores_by_question: dict[str, dict[str, int]] | None = None,
    drafts_by_question: dict[str, str] | None = None,
) -> tuple[E7Sweep, _FakeRetriever, _FakeAnswerer, _FakeJudge]:
    retriever = _FakeRetriever(rows_by_question)
    answerer = _FakeAnswerer(drafts_by_question)
    judge = _FakeJudge(scores_by_question)
    result = asyncio.run(
        run_e7_sweep(
            questions=questions,
            retrieve=retriever,
            draft=answerer,
            judge=judge,
            tau_sims=tau_sims,
            n_mins=n_mins,
            faithfulness_mins=faithfulness_mins,
            match_threshold=THRESH,
            faithfulness_cutoff=0.7,
            ceiling=ceiling,
            judge_model=JUDGE_MODEL,
        )
    )
    return result, retriever, answerer, judge


def _sweep_scenario() -> tuple[list[dict], dict, dict]:
    """A labeled subset whose 2×2×2 (τ_sim × N_min × faith) sweep has a KNOWN knee.

      * P1a: 2 rows, WEAK -> never clear the gate (monitor-only, excluded from the
        gated rate).
      * P2:  2 rows, STRONG (strong at every τ_sim) -> deflection depends ONLY on
        the faithfulness floor: scores 5 + 4, so faith≥4 deflects both (1.0),
        faith≥5 deflects one (0.5).
      * P3:  2 rows, MID (strong at τ_sim 0.40, weak at 0.60), scores 5 + 5 ->
        false-resolve depends ONLY on τ_sim: at 0.40 both auto-resolve (the
        moat-breaking false-resolve), at 0.60 both escalate at the gate (0).

    So the gated faithfulness-leg false-resolve = 1.0 at τ_sim 0.40 (both P3 rows
    auto-resolve, 2/2) and 0.0 at τ_sim 0.60 (both escalate at the gate, 0/2), while
    deflection = 1.0 at faith≥4 and 0.5 at faith≥5. Under a 0.05 ceiling only the
    τ_sim 0.60 points are feasible; the deflection-max among them is faith≥4, and
    the N_min tie (1 vs 2, both equal) breaks to the lower index -> the knee is
    (τ_sim=0.60, N_min=1, faith≥4): deflection 1.0, false-resolve 0.0.
    """
    questions = [
        _p1a("s-p1a-a", "sq-p1a-a"),
        _p1a("s-p1a-b", "sq-p1a-b"),
        _p2("s-p2-a", "sq-p2-a"),
        _p2("s-p2-b", "sq-p2-b"),
        _p3("s-p3-a", "sq-p3-a"),
        _p3("s-p3-b", "sq-p3-b"),
    ]
    rows = {
        "sq-p1a-a": WEAK,
        "sq-p1a-b": WEAK,
        "sq-p2-a": STRONG,
        "sq-p2-b": STRONG,
        "sq-p3-a": MID,
        "sq-p3-b": MID,
    }
    scores = {
        "sq-p2-a": {"faithfulness": 5, "helpfulness": 5},
        "sq-p2-b": {"faithfulness": 4, "helpfulness": 4},
        "sq-p3-a": {"faithfulness": 5, "helpfulness": 5},
        "sq-p3-b": {"faithfulness": 5, "helpfulness": 5},
    }
    return questions, rows, scores


def test_sweep_curve_and_knee() -> None:
    """PRD core: a 2×2×2 grid with a known knee under false-resolve ≤ 0.05 emits a
    curve listing each point's (deflection, false-resolve) and picks the
    highest-deflection FEASIBLE point as the knee, reporting its knob values."""
    questions, rows, scores = _sweep_scenario()
    sweep, _, _, _ = _run_sweep(
        questions, rows,
        tau_sims=[0.40, 0.60], n_mins=[1, 2], faithfulness_mins=[4, 5],
        ceiling=0.05, scores_by_question=scores,
    )

    _check(len(sweep.points) == 8, f"a 2×2×2 grid has 8 points, got {len(sweep.points)}")
    _check(isinstance(sweep, E7Sweep), "run_e7_sweep returns an E7Sweep")

    # The curve lists every (defined) point's (deflection, false-resolve), sorted
    # by false-resolve ascending (the operating curve the knee sits on).
    _check(len(sweep.curve) == 8, f"all 8 points are on the curve, got {len(sweep.curve)}")
    for pt in sweep.curve:
        _check("deflection" in pt and "false_resolve" in pt, "each curve point carries (deflection, false_resolve)")
    frs = [pt["false_resolve"] for pt in sweep.curve]
    _check(frs == sorted(frs), f"the curve is sorted by false-resolve ascending, got {frs}")

    # The knee: highest deflection subject to false-resolve ≤ 0.05.
    _check(sweep.knee_reason == "ok", f"a feasible knee exists, got {sweep.knee_reason!r}")
    knee = sweep.knee
    _check(knee is not None, "the knee is reported")
    assert knee is not None  # for the type checker
    _check(knee.tau_sim == 0.60 and knee.n_min == 1 and knee.faithfulness_judge_min == 4,
           f"knee knobs (0.60, 1, 4) expected, got ({knee.tau_sim}, {knee.n_min}, {knee.faithfulness_judge_min})")
    _check(knee.deflection == 1.0 and knee.false_resolve == 0.0,
           f"knee deflection 1.0 / false-resolve 0.0 expected, got {knee.deflection}/{knee.false_resolve}")
    _check(knee.false_resolve is not None and knee.false_resolve <= 0.05, "the knee satisfies the ceiling")

    # Failure indicator: equally-high-deflection points that EXCEED the ceiling
    # must NOT be chosen (the objective is ceiling-constrained, not max-deflection).
    infeasible_equal = [p for p in sweep.points if p.deflection == 1.0 and not p.feasible]
    _check(len(infeasible_equal) == 2, f"two τ_sim=0.40 points also reach deflection 1.0, got {len(infeasible_equal)}")
    _check(all(p.false_resolve is not None and p.false_resolve > 0.05 for p in infeasible_equal),
           "those points exceed the ceiling")
    _check(knee.index not in [p.index for p in infeasible_equal],
           "the knee is a feasible point, never the infeasible equal-deflection one")

    # The knee's knobs are reported as recommended US-050 defaults.
    rec = sweep.recommended_config()
    _check(rec == {"ESCALATION_TAU_SIM": 0.60, "ESCALATION_N_MIN": 1, "faithfulness_judge_min_offline_1_5": 4},
           f"recommended config must report the knee knobs, got {rec}")
    print("ok: the sweep emits the curve and picks the highest-deflection knee under the ceiling")


def test_sweep_memoizes_llm_calls_per_question() -> None:
    """The grid memoizes retrieve/draft/judge per question, so an 8-point sweep
    drafts/judges each question AT MOST ONCE — the curve reflects the knobs, not
    LLM re-sampling, and the grid costs the same LLM as one operating point.

    Without memoization the 2 always-strong P2 rows would draft at all 8 points
    (16) plus the 2 P3 rows at the 4 τ_sim=0.40 points (8) = 24 draft calls."""
    questions, rows, scores = _sweep_scenario()
    sweep, retriever, answerer, judge = _run_sweep(
        questions, rows,
        tau_sims=[0.40, 0.60], n_mins=[1, 2], faithfulness_mins=[4, 5],
        ceiling=0.05, scores_by_question=scores,
    )

    _check(len(sweep.points) == 8, "8-point grid")
    _check(retriever.calls == 6, f"each of the 6 distinct questions is retrieved once, got {retriever.calls}")
    # Only the 4 questions that EVER clear the gate (2 P2 strong everywhere, 2 P3
    # strong at τ_sim 0.40) draft + judge, and only once each.
    _check(answerer.calls == 4, f"≤1 draft per ever-strong question (4), got {answerer.calls} (24 without memoization)")
    _check(judge.calls == 4, f"≤1 judge per drafted question (4), got {judge.calls}")
    print("ok: the sweep memoizes per question — an 8-point grid costs the same LLM as one point")


def test_sweep_no_point_under_ceiling_is_reported() -> None:
    """Failure indicator: when NO grid point achieves false-resolve ≤ ceiling, the
    sweep reports that EXPLICITLY (knee None, reason no_point_under_ceiling) rather
    than silently picking the least-bad point."""
    # 1 P1a (WEAK) + 1 P3 (STRONG, faithful score 5) -> the P3 auto-resolves at
    # every grid point (STRONG is strong at all τ_sim), so the gated faithfulness-leg
    # false-resolve = 1/1 = 1.0 everywhere, above the 0.05 ceiling.
    questions = [_p1a("nb-p1a", "nq-p1a"), _p3("nb-p3", "nq-p3")]
    rows = {"nq-p1a": WEAK, "nq-p3": STRONG}
    scores = {"nq-p3": {"faithfulness": 5, "helpfulness": 5}}
    sweep, _, _, _ = _run_sweep(
        questions, rows,
        tau_sims=[0.40, 0.60], n_mins=[1, 2], faithfulness_mins=[4, 5],
        ceiling=0.05, scores_by_question=scores,
    )

    _check(all(not p.feasible for p in sweep.points), "no point satisfies the ceiling")
    _check(sweep.knee is None, "no knee is selected when the ceiling is unsatisfiable")
    _check(sweep.knee_reason == "no_point_under_ceiling",
           f"the unsatisfiable-ceiling case is reported explicitly, got {sweep.knee_reason!r}")
    _check(sweep.recommended_config() is None, "no recommended config without a knee")
    print("ok: an unsatisfiable ceiling is reported explicitly, never downgraded to least-bad")


def test_sweep_deflection_blind_is_reported() -> None:
    """When grid points satisfy the ceiling but the P2 answerable population is
    empty, deflection is structurally blind, so the sweep reports deflection_blind
    rather than recommending an operating point off a None deflection."""
    # A P3 row that ESCALATES at the faithfulness gate (score 3 < floor) -> gated
    # false-resolve 0/1 = 0.0 (measured + feasible everywhere), but no P2 -> no
    # deflection to maximize. (A P1a-only sweep would leave the gated rate UNMEASURED
    # -> not feasible -> no_point_under_ceiling, not this branch.)
    questions = [_p3("db-p3", "dq-p3")]
    rows = {"dq-p3": STRONG}
    scores = {"dq-p3": {"faithfulness": 3, "helpfulness": 3}}
    sweep, _, _, _ = _run_sweep(
        questions, rows,
        tau_sims=[0.40, 0.60], n_mins=[1, 2], faithfulness_mins=[4, 5],
        ceiling=0.05, scores_by_question=scores,
    )

    _check(all(p.feasible for p in sweep.points), "every point satisfies the ceiling (false-resolve 0.0)")
    _check(all(p.deflection is None for p in sweep.points), "deflection is blind with no P2 rows")
    _check(sweep.knee is None, "no knee off a blind deflection")
    _check(sweep.knee_reason == "deflection_blind",
           f"a blind deflection is reported explicitly, got {sweep.knee_reason!r}")
    print("ok: a feasible-but-deflection-blind sweep is reported explicitly")


def test_sweep_to_dict_shape() -> None:
    """The sweep JSON carries the curve, the per-point metrics, the knee, the
    recommended config, and the ceiling — so the operating point is auditable."""
    questions, rows, scores = _sweep_scenario()
    sweep, _, _, _ = _run_sweep(
        questions, rows,
        tau_sims=[0.40, 0.60], n_mins=[1, 2], faithfulness_mins=[4, 5],
        ceiling=0.05, scores_by_question=scores,
    )
    d = sweep.to_dict()
    for key in ("ceiling", "match_threshold", "judge_model", "faithfulness_cutoff",
                "n_questions", "knee_reason", "knee", "recommended_config",
                "curve", "points"):
        _check(key in d, f"sweep dict missing {key!r}")
    _check(d["ceiling"] == 0.05, f"the ceiling is recorded, got {d['ceiling']}")
    _check(len(d["points"]) == 8, "all grid points are serialized")
    pt = d["points"][0]
    for key in ("index", "tau_sim", "n_min", "faithfulness_judge_min", "deflection",
                "false_resolve", "false_escalate", "feasible", "metrics"):
        _check(key in pt, f"sweep point dict missing {key!r}")
    # the embedded per-point metrics reuse the US-055 consolidation shape
    for name in ("deflection", "false_resolve", "false_escalate"):
        _check(name in pt["metrics"], f"point metrics missing {name!r}")
    _check(d["knee"]["tau_sim"] == 0.60, "the serialized knee carries its knobs")
    _check(isinstance(sweep.points[0], SweepPoint), "points are SweepPoint instances")
    print("ok: the sweep to_dict carries the curve, per-point metrics, knee, and recommended config")


# --- US-057 P1b no-access replay fixtures + tests -------------------------


class _FakeNoAccessRetriever:
    """Call-counting fake `RetrieveNoAccess`: maps question id -> the
    (gold-filtered) rows the no-access viewer would see.

    Records each FULL question dict it received so a test can prove the leg passes
    the dict (needed to revoke THAT question's gold) and that there is exactly ONE
    retrieval per row — there is no privileged/owner retriever anywhere, so a
    "this is actually answerable for someone" signal can never reach the gate.
    """

    def __init__(self, rows_by_id: dict[str, list[SearchDocumentsResult]]) -> None:
        self.rows_by_id = rows_by_id
        self.calls = 0
        self.seen: list[dict] = []

    async def __call__(self, q: dict) -> list[SearchDocumentsResult]:
        self.calls += 1
        self.seen.append(q)
        return self.rows_by_id.get(q["id"], [])


def _run_p1b(
    questions: list[dict],
    rows_by_id: dict[str, list[SearchDocumentsResult]],
    *,
    stable_id_by_chunk_id: dict[str, str] | None = None,
) -> tuple[E7P1bResult, _FakeNoAccessRetriever]:
    retriever = _FakeNoAccessRetriever(rows_by_id)
    # Default: identity map (chunk id == stable id) over every fixture row, so a row
    # whose id is the gold stable id ("warranty-terms:0") leaks and any other id does
    # not. Production passes the real `runner.fetch_stable_id_map` (chunk_id→stable_id).
    if stable_id_by_chunk_id is None:
        stable_id_by_chunk_id = {
            r.id: r.id for rows in rows_by_id.values() for r in rows
        }
    result = asyncio.run(
        run_e7_p1b(
            questions=questions,
            retrieve_no_access=retriever,
            config=CONFIG,
            match_threshold=THRESH,
            stable_id_by_chunk_id=stable_id_by_chunk_id,
        )
    )
    return result, retriever


def test_p1b_no_gold_in_result_passes() -> None:
    """PRD core: a P2 question replayed under a no-access viewer whose gold is filtered
    out returns NO gold chunk, so the row is clean (no leak) with 0 draft / 0 judge
    calls — whether the gate read weak OR strong off legitimately-granted non-gold."""
    questions = [
        _p2("e7-p2-a", "How long is the electronics warranty?"),
        _p2("e7-p2-b", "What is the return window?"),
    ]
    rows = {"e7-p2-a": WEAK, "e7-p2-b": WEAK}  # gold revoked -> only weak non-gold
    result, retriever = _run_p1b(questions, rows)

    _check(result.population == "P1b", f"population must be P1b, got {result.population!r}")
    _check(result.source_label == "answerable_faithful",
           f"P1b replays the P2 population, got source_label {result.source_label!r}")
    _check(result.n_questions == 2, f"both P2 rows replayed, got {result.n_questions}")
    _check(retriever.calls == 2, f"exactly one no-access retrieval per row, got {retriever.calls}")
    _check(result.total_draft_calls == 0 and result.total_judge_calls == 0, "P1b makes 0 draft/0 judge calls (like P1a)")
    _check(result.cleared_gate == [], "no P1b row leaked gold")
    _check(result.passed is True, "a no-leak, no-draft/judge P1b run must pass")
    for d in result.decisions:
        _check(d.decision == "escalate", f"{d.question_id}: weak non-gold retrieval reads escalate, got {d.decision}")
        _check(d.leaked_gold_ids == [], f"{d.question_id}: no gold leaked, got {d.leaked_gold_ids}")
        _check(d.correct is True, f"{d.question_id}: no gold in the result is the correct P1b outcome")
        _check(d.draft_calls == 0 and d.judge_calls == 0, "per-row 0 draft/0 judge")
    print("ok: a P2 question replayed under a no-access viewer returns no gold (clean, no leak)")


def test_p1b_nongold_strong_is_not_a_leak() -> None:
    """REGRESSION (the false positive this fix removes): a no-access viewer whose gold
    is revoked still holds every NON-gold corpus chunk, so its retrieval can read
    STRONG off topic-adjacent non-gold (e.g. the gold doc's sibling). A strong gate
    with NO gold in the result is NOT a leak — the row is clean and the run passes."""
    questions = [_p2("e7-p2-strong-nongold", "How long is the electronics warranty?")]
    rows = {"e7-p2-strong-nongold": STRONG}  # ids a/b are non-gold; gate reads strong
    result, _ = _run_p1b(questions, rows)

    d = result.decisions[0]
    _check(d.gate_strong is True, "the gate reads strong off the non-gold chunks")
    _check(d.decision == "draft", f"a strong gate reports 'draft', got {d.decision}")
    _check(d.leaked_gold_ids == [], f"no gold stable id in the result, got {d.leaked_gold_ids}")
    _check(d.correct is True, "a strong gate off legitimately-granted non-gold is NOT a leak")
    _check(result.cleared_gate == [], "no row leaked gold")
    _check(result.passed is True, "a non-gold strong retrieval must not fail the P1b leg")
    print("ok: a strong gate off legitimately-granted non-gold chunks is not a P1b leak")


def test_p1b_gold_in_result_is_a_leak() -> None:
    """Failure indicator: a P1b row whose no-access retrieval RETURNS a gold chunk
    means the gold leaked to a no-access viewer — an isolation/disclosure failure. It
    is flagged correct=False (identity-measured) and fails the run."""
    questions = [
        _p2("e7-p2-ok", "A question whose gold is correctly filtered."),
        _p2("e7-p2-leak", "A question whose gold leaked to the no-access viewer."),
    ]
    rows = {"e7-p2-ok": WEAK, "e7-p2-leak": GOLD_LEAK_STRONG}
    result, _ = _run_p1b(questions, rows)

    _check(result.passed is False, "a P1b row that returned the gold (a leak) must fail the run")
    leaked = result.cleared_gate
    _check([d.question_id for d in leaked] == ["e7-p2-leak"], f"the leaked row must be flagged, got {leaked}")
    bad = leaked[0]
    _check(bad.leaked_gold_ids == [GOLD], f"the leaked gold stable id is recorded, got {bad.leaked_gold_ids}")
    _check(bad.correct is False, "a P1b row that retrieved the gold is incorrect (a leak)")
    _check(result.total_draft_calls == 0 and result.total_judge_calls == 0, "still 0 draft/0 judge (the leg never drafts)")
    print("ok: a P1b row that returns a gold chunk is flagged as a no-access leak and fails the run")


def test_p1b_gold_below_tau_is_still_a_leak() -> None:
    """The identity leak check is DECOUPLED from the answerability gate: a gold chunk
    returned to the no-access viewer at a cosine BELOW tau_sim (so the gate reads
    WEAK) is STILL a leak. This is the false NEGATIVE the old gate-based check would
    have missed — a real RLS regression that surfaces the gold quietly must flip red."""
    questions = [_p2("e7-p2-quiet-leak", "A question whose gold leaked at a low cosine.")]
    rows = {"e7-p2-quiet-leak": GOLD_LEAK_WEAK}  # gold present, but cosine 0.20 < tau 0.40
    result, _ = _run_p1b(questions, rows)

    d = result.decisions[0]
    _check(d.gate_strong is False, "the gate reads WEAK (the gold cosine is below tau_sim)")
    _check(d.decision == "escalate", f"a weak gate reports 'escalate', got {d.decision}")
    _check(d.leaked_gold_ids == [GOLD], f"the gold is still in the result, got {d.leaked_gold_ids}")
    _check(d.correct is False, "a returned gold chunk is a leak regardless of cosine")
    _check(result.passed is False, "a quiet (sub-threshold) gold leak must still fail the run")
    print("ok: a gold chunk returned below tau_sim (gate weak) is still caught as a P1b leak")


def test_p1b_no_privileged_second_pass() -> None:
    """The leg receives ONLY the no-access retrieval callable (no owner/privileged
    retriever) and calls it exactly once per row, passing the FULL question dict so
    that question's gold can be revoked — structurally there is no second pass that
    could learn the content exists (US-057/058)."""
    questions = [_p2("e7-p2-1", "q1"), _p2("e7-p2-2", "q2")]
    rows = {"e7-p2-1": WEAK, "e7-p2-2": WEAK}
    result, retriever = _run_p1b(questions, rows)

    _check(retriever.calls == len(questions), f"exactly one retrieval per row (no second pass), got {retriever.calls}")
    _check([q["id"] for q in retriever.seen] == ["e7-p2-1", "e7-p2-2"], "each row retrieved once, in order")
    # The leg must pass the FULL dict (not just the text) so the no-access viewer's
    # gold for THAT question can be revoked before retrieving.
    for q in retriever.seen:
        _check("gold_stable_ids" in q, "the no-access retriever receives the full question dict (gold for revocation)")
    _check(result.total_draft_calls == 0 and result.total_judge_calls == 0, "no draft/judge path exists in the P1b leg")
    print("ok: P1b has no privileged second pass — one no-access retrieval per row, full dict passed")


def test_p1b_replays_only_p2_rows() -> None:
    """Only `answerable_faithful` (P2) rows are replayed under the no-access viewer;
    P1a/P3 rows are ignored (P1b is the access-filtered P2 case)."""
    questions = [
        _p1a("e7-p1a-1", "An off-topic question."),
        _p2("e7-p2-1", "warranty?"),
        _p3("e7-p3-1", "jewelry warranty?"),
    ]
    result, retriever = _run_p1b(questions, {"e7-p2-1": WEAK})
    _check(result.n_questions == 1, f"only the P2 row is replayed, got {result.n_questions}")
    _check([q["id"] for q in retriever.seen] == ["e7-p2-1"], f"only the P2 question retrieved, got {retriever.seen}")
    _check(result.decisions[0].question_id == "e7-p2-1", "the scored row is the P2 one")
    print("ok: P1b replays only the P2 population (P1a/P3 ignored)")


def test_p1b_empty_population_is_not_a_pass() -> None:
    """A run with no P2 rows to replay is structurally blind for the access-filtered
    case — it must NOT pass (mirrors P1a's positive-control guard)."""
    questions = [_p1a("e7-p1a-1", "An off-topic question.")]
    result, retriever = _run_p1b(questions, {})
    _check(result.n_questions == 0, f"no P2 rows to replay, got {result.n_questions}")
    _check(retriever.calls == 0, "non-P2 rows are not replayed by the P1b leg")
    _check(result.passed is False, "a P1b run replaying zero P2 rows must not pass")
    print("ok: an empty P1b population is not a pass (structurally-blind guard)")


def test_p1b_to_dict_shape() -> None:
    """The P1b result + decision JSON carry the audited fields (provenance, decision,
    scores, the pinned 0 draft/judge counters)."""
    questions = [_p2("e7-p2-s", "warranty?")]
    result, _ = _run_p1b(questions, {"e7-p2-s": WEAK})
    d = result.to_dict()
    for key in ("population", "source_label", "tau_sim", "n_min", "match_threshold",
                "n_questions", "total_draft_calls", "total_judge_calls",
                "cleared_gate", "passed", "decisions"):
        _check(key in d, f"P1b result dict missing {key!r}")
    _check(d["population"] == "P1b", f"population must be P1b, got {d['population']!r}")
    _check(d["source_label"] == "answerable_faithful", f"source_label must be the P2 label, got {d['source_label']!r}")
    dec = d["decisions"][0]
    for key in ("question_id", "decision", "expected", "correct", "gate_strong",
                "top1_cosine", "n_cleared", "gate_reason", "n_results",
                "returned_stable_ids", "gold_stable_ids", "leaked_gold_ids",
                "draft_calls", "judge_calls"):
        _check(key in dec, f"P1b decision dict missing {key!r}")
    _check(dec["gold_stable_ids"] == [GOLD], f"the replayed gold is recorded, got {dec['gold_stable_ids']}")
    _check(dec["leaked_gold_ids"] == [], f"a clean row leaks no gold, got {dec['leaked_gold_ids']}")
    _check(isinstance(result.decisions[0], P1bDecision), "decisions are P1bDecision instances")
    print("ok: P1b result/decision to_dict carry the audited fields")


def test_metrics_p1b_leak_is_monitor_only_not_gated() -> None:
    """US-055/059: a P1b leak (a no-access row that RETURNED the gold) is a
    zero-tolerance retrieval-leg failure — surfaced in the false-resolve breakdown
    monitor-only and hard-failed UNCONDITIONALLY by the P1b invariant, but EXCLUDED
    from the ceiling-gated faithfulness-leg rate so it never dilutes (nor feeds) it."""
    # P1a: 2 rows, both clean (0 cleared) -> monitor-only 0/2.
    p1a, _ = _run([_p1a("m-p1a-a", "qa"), _p1a("m-p1a-b", "qb")], {"qa": WEAK, "qb": WEAK})
    # P1b: 2 P2 rows replayed under no-access, 1 leak (gold returned) -> monitor-only 1/2.
    p1b, _ = _run_p1b(
        [_p2("m-p2-a", "qp2a"), _p2("m-p2-b", "qp2b")],
        {"m-p2-a": WEAK, "m-p2-b": GOLD_LEAK_STRONG},
    )
    m = compute_e7_metrics(p1a, None, None, p1b)

    fr = m.false_resolve
    # No P3 faithfulness leg ran, so the gated rate is not measured (None) — the P1b
    # leak does NOT become the gated false-resolve number.
    _check(fr.rate is None, f"gated false-resolve is None with no P3 leg, got {fr.rate}")
    _check(fr.numerator == 0 and fr.denominator == 0,
           f"the gated n/d excludes the monitor-only P1a/P1b, got {fr.numerator}/{fr.denominator}")
    by_pop = {c.population: (c.numerator, c.denominator, c.counts_toward_rate)
              for c in fr.by_population}
    _check(by_pop == {"P1a": (0, 2, False), "P1b": (1, 2, False)},
           f"P1a/P1b are surfaced monitor-only, never gated, got {by_pop}")
    _check(fr.safety is True, "false-resolve is the pinned safety metric")
    # The leak still hard-fails unconditionally via the P1b invariant, regardless of rate.
    _check(p1b.passed is False,
           "a P1b row that returned the gold fails the P1b invariant regardless of rate")
    print("ok: a P1b leak is monitor-only (not gated) and hard-fails the P1b invariant unconditionally")


# --- US-058: P1b non-disclosure byte-equality assertion -------------------


def test_p1b_non_disclosure_pass() -> None:
    """PRD core: a no-access P1b escalation shows the customer byte-for-byte the SAME
    generic deferral as a P1a escalation — even though the two rows carry DIFFERENT
    internal gate reasons (different cosines) — so escalating never discloses that
    restricted content exists. The reference is the REAL `escalation.GENERIC_DEFERRAL`,
    not a copy in the eval."""
    # P1a and P1b weak at DIFFERENT cosines -> genuinely different internal reasons.
    p1a, _ = _run([_p1a("nd-p1a", "an absent question")], {"an absent question": [_row("x", 0.05)]})
    p1b, _ = _run_p1b([_p2("nd-p2", "a no-access question")], {"nd-p2": [_row("y", 0.12)]})
    _check(
        p1a.decisions[0].gate_reason != p1b.decisions[0].gate_reason,
        "the P1a and P1b rows must carry different internal gate reasons (else the "
        "invariance proven here is trivial)",
    )

    nd = assert_p1b_non_disclosure(p1a, p1b)
    _check(isinstance(nd, E7P1bNonDisclosure), "the assertion returns an E7P1bNonDisclosure")
    _check(nd.passed is True, "byte-equal customer outputs must pass the non-disclosure assertion")
    _check(nd.leaks == [], f"no leaks expected, got {nd.leaks}")
    _check(nd.n_p1b == 1, f"one P1b row scored, got {nd.n_p1b}")
    _check(nd.p1a_reference == GENERIC_DEFERRAL, "the reference is the real production GENERIC_DEFERRAL")
    _check(
        nd.p1a_reference_bytes == len(GENERIC_DEFERRAL.encode("utf-8")),
        f"the reference byte length is recorded, got {nd.p1a_reference_bytes}",
    )
    print("ok: a P1b escalation shows the same customer bytes as P1a despite a different internal reason")


def test_p1b_non_disclosure_injected_reason_fails() -> None:
    """PRD failure case (validation step 4): inject an access-denied reason into the
    P1b customer output and re-run — the assertion fails LOUDLY, proving it actually
    pins the byte-equality invariant rather than trivially passing."""
    p1a, _ = _run([_p1a("nd-p1a", "absent q")], {"absent q": WEAK})
    p1b, _ = _run_p1b([_p2("nd-p2", "no-access q")], {"nd-p2": WEAK})

    def _leaking_output(d: P1bDecision) -> bytes:
        # Simulate a regression that echoes the access-denied reason to the customer
        # (the existence bit US-058 forbids) — distinct from the P1a deferral bytes.
        return GENERIC_DEFERRAL.encode("utf-8") + b"\nreason=access-denied"

    nd = assert_p1b_non_disclosure(p1a, p1b, p1b_output_fn=_leaking_output)
    _check(nd.passed is False, "an injected access-denied reason must fail the assertion")
    _check([leak.question_id for leak in nd.leaks] == ["nd-p2"], f"the leaking row must be flagged, got {nd.leaks}")
    _check(isinstance(nd.leaks[0], P1bLeak), "a leak is a P1bLeak")
    _check(
        nd.leaks[0].detail == "customer_bytes_differ",
        f"a differing-bytes leak is detailed customer_bytes_differ, got {nd.leaks[0].detail}",
    )
    print("ok: an injected access-denied reason in the P1b output fails the assertion (it really pins the invariant)")


def test_p1b_non_disclosure_ignores_legit_nongold_draft() -> None:
    """US-058 is DECOUPLED from the US-057 identity leak check: it guards the
    escalation path only. A no-access row that drafts from legitimately-granted
    NON-gold chunks (gate strong, no gold returned) is not a restricted-content
    disclosure, so US-058 does NOT flag it — the gold-leak case is owned entirely by
    the US-057 identity check (`E7P1bResult.cleared_gate`)."""
    p1a, _ = _run([_p1a("nd-p1a", "absent q")], {"absent q": WEAK})
    p1b, _ = _run_p1b(
        [_p2("nd-draft", "answerable-from-nongold q")],
        {"nd-draft": STRONG},  # gate strong off non-gold ids; NO gold returned
    )
    _check(p1b.decisions[0].decision == "draft", "the non-gold-strong row drafts (gate strong)")
    _check(p1b.decisions[0].leaked_gold_ids == [], "no gold leaked (the draft is from non-gold)")
    _check(p1b.passed is True, "a legitimate non-gold draft does not fail the US-057 leak check")

    nd = assert_p1b_non_disclosure(p1a, p1b)
    _check(nd.passed is True, "US-058 does not flag a legitimate non-gold draft (escalation-path only)")
    _check(nd.leaks == [], f"no escalation-path bytes to flag, got {nd.leaks}")
    _check(nd.n_p1b == 1, "the row is still counted toward the positive control")
    print("ok: US-058 ignores a legitimate non-gold draft (decoupled from the US-057 identity leak check)")


def test_p1b_non_disclosure_empty_is_not_a_pass() -> None:
    """No P1b rows -> the non-disclosure invariant scored nothing; a zero-leak over
    zero rows is structurally blind, NOT a pass (mirrors the P1a/P1b guards)."""
    p1a, _ = _run([_p1a("nd-p1a", "absent q")], {"absent q": WEAK})
    p1b, _ = _run_p1b([_p1a("not-a-p2", "off-topic")], {})  # no P2 rows -> empty P1b
    _check(p1b.n_questions == 0, "no P2 rows to replay")
    nd = assert_p1b_non_disclosure(p1a, p1b)
    _check(nd.n_p1b == 0, f"no P1b rows scored, got {nd.n_p1b}")
    _check(nd.leaks == [], "no rows means no leaks recorded")
    _check(nd.passed is False, "an empty P1b non-disclosure population must not pass")
    print("ok: an empty P1b non-disclosure population is not a pass (structurally-blind guard)")


def test_p1b_non_disclosure_to_dict_and_render() -> None:
    """The non-disclosure assertion JSON carries the audited fields (the assertion
    statement, the reference bytes, n_p1b, the leak list), and the render names the
    invariant and the verdict."""
    p1a, _ = _run([_p1a("nd-p1a", "absent q")], {"absent q": WEAK})
    p1b, _ = _run_p1b([_p2("nd-p2", "no-access q")], {"nd-p2": WEAK})
    nd = assert_p1b_non_disclosure(p1a, p1b)
    d = nd.to_dict()
    for key in ("assertion", "p1a_reference", "p1a_reference_bytes", "n_p1b",
                "n_leaks", "leaks", "passed"):
        _check(key in d, f"non-disclosure dict missing {key!r}")
    _check(d["passed"] is True, "the clean run passes")
    _check(d["n_leaks"] == 0, "no leaks in the clean run")
    _check(d["p1a_reference"] == GENERIC_DEFERRAL, "the reference is the real deferral text")

    md = "\n".join(render_e7_p1b_non_disclosure_section(nd))
    _check("non-disclosure" in md.lower(), "the rendered section names the invariant")
    _check("PASS" in md, "the rendered verdict shows PASS for a clean run")
    print("ok: the non-disclosure to_dict + render carry the audited fields")


def main() -> int:
    tests = [
        test_p1a_escalates_at_gate,
        test_near_miss_is_visible,
        test_p1a_clearing_gate_is_flagged,
        test_gate_reads_cosine_not_rrf_similarity,
        test_empty_p1a_is_not_a_pass,
        test_non_p1a_rows_ignored,
        test_deterministic,
        test_to_dict_shape,
        test_p2_auto_resolves_when_faithful,
        test_p2_uses_offline_judge_with_reference,
        test_p2_false_escalate_at_retrieval_gate,
        test_p2_false_escalate_at_faithfulness_leg,
        test_p2_empty_draft_escalates_without_judge,
        test_p2_faithfulness_floor_is_inclusive,
        test_p2_empty_population_is_not_a_pass,
        test_p2_to_dict_shape,
        test_p3_escalates_at_faithfulness_leg,
        test_p3_auto_resolve_is_false_resolve,
        test_p3_retrieval_gate_escalation_is_mislabeled,
        test_p3_empty_draft_is_mislabeled,
        test_p3_uses_offline_judge_with_reference,
        test_p3_faithfulness_floor_inclusive_and_flips_vs_p2,
        test_p3_empty_population_is_not_a_pass,
        test_p3_all_mislabeled_is_not_a_pass,
        test_p3_to_dict_shape,
        test_metrics_consolidate_three_rates,
        test_metrics_false_resolve_is_faithfulness_leg_gated,
        test_metrics_blind_when_leg_absent,
        test_metrics_to_dict_shape,
        test_ceiling_breach_when_rate_exceeds_ceiling,
        test_ceiling_within_when_rate_at_or_below_ceiling,
        test_ceiling_blind_rate_is_not_a_breach,
        test_ceiling_inert_on_passing_per_pr_shape,
        test_ceiling_verdict_to_dict_and_render,
        test_exit_p3_blind_hard_fails,
        test_exit_p3_all_mislabeled_hard_fails,
        test_exit_clean_weekly_does_not_fail,
        test_exit_per_pr_shape_without_p3_does_not_fail,
        test_exit_measured_ceiling_breach_fails,
        test_exit_p1a_gate_clear_fails,
        test_p3_mislabel_ratio_property,
        test_exit_p3_partial_mislabel_over_max_fails,
        test_exit_p3_mislabel_ratio_boundary_and_configurable,
        test_exit_p3_all_mislabeled_owned_by_positive_control_not_ratio_guard,
        test_get_p3_mislabel_ratio_max_env,
        test_sweep_curve_and_knee,
        test_sweep_memoizes_llm_calls_per_question,
        test_sweep_no_point_under_ceiling_is_reported,
        test_sweep_deflection_blind_is_reported,
        test_sweep_to_dict_shape,
        test_p1b_no_gold_in_result_passes,
        test_p1b_nongold_strong_is_not_a_leak,
        test_p1b_gold_in_result_is_a_leak,
        test_p1b_gold_below_tau_is_still_a_leak,
        test_p1b_no_privileged_second_pass,
        test_p1b_replays_only_p2_rows,
        test_p1b_empty_population_is_not_a_pass,
        test_p1b_to_dict_shape,
        test_metrics_p1b_leak_is_monitor_only_not_gated,
        test_p1b_non_disclosure_pass,
        test_p1b_non_disclosure_injected_reason_fails,
        test_p1b_non_disclosure_ignores_legit_nongold_draft,
        test_p1b_non_disclosure_empty_is_not_a_pass,
        test_p1b_non_disclosure_to_dict_and_render,
    ]
    for t in tests:
        t()
    print(f"\nPASS: {len(tests)} E7 P1a+P1b+P2+P3+metrics+sweep+ceiling runner (US-052–059) test groups")
    return 0


if __name__ == "__main__":
    sys.exit(main())
