"""US-047: deterministic cosine-defined retrieval gate (ADR-0003).

The support face answers or escalates via a deterministic deflection pipeline
(US-049) — escalate-vs-answer is *control flow*, never a model `escalate()`
tool. The cheap left operand of that decision is this **retrieval gate**: pure
arithmetic on the raw, pre-fusion vector cosine (`cosine_similarity`, US-046)
that calls a query's retrieval "weak" — meaning *escalate before any draft or
faithfulness-judge call* — when the best hit is below `tau_sim` or too few hits
clear `match_threshold`. Because it makes no LLM/reranker call, it short-circuits
the expensive faithfulness gate (US-048) on genuinely-no-context queries.

It reads only `cosine_similarity`, never `similarity` — after hybrid fusion the
latter is the RRF rank artifact (a small absolute number, US-021), so a query
with a high RRF score but a low cosine must still be called weak. A reranker,
when present, overwrites `similarity` with its calibrated score but leaves
`cosine_similarity` intact (`reranking.py` `model_copy`s only `similarity`), so
the gate contract stays cosine and the gate survives deletion of the optional
reranker module (R4) — `escalation.py` imports nothing from `reranking`.

US-048: `faithfulness_gate` is the OR's expensive right operand, reached only
when the retrieval gate calls retrieval strong. It makes **exactly one**
structured-output judge call (ADR-0006 runtime judge role; `gpt-4o-mini` /
`haiku`-class) verifying the drafted answer is grounded in its retrieved chunks,
and fails **closed** — any judge error / refusal / parse failure / timeout is
treated as unfaithful (⇒ escalate), never auto-sent. This runtime gate is a
NET-NEW one-call check, NOT the offline RAGAS `faithfulness` metric in
`evals/retrieval/ragas.py` (which decomposes claims across several calls and
runs weekly); the same English word "faithfulness" names two distinct
machineries on two different latency budgets.

US-049: `run_deflection_pipeline` wires the two gates into the exact ADR-0003
control flow — `retrieve (hybrid, once) → retrieval gate → [if strong] draft →
faithfulness gate → answer-or-escalate` — as deterministic control flow, never a
model `escalate()` tool and never the M1 agentic loop (`MAX_TOOL_ITERATIONS` in
`main.py`). The OR short-circuits on its cheap left operand: a weak retrieval
escalates having made ZERO draft and ZERO judge calls. On any escalate the
customer-facing message is a fixed generic deferral with NO reason/access
metadata; the decision tags live only on the internal result fields (for logging
/ the US-067 conversation status), never in `customer_message`.

US-050: `EscalationConfig` (+ the standalone `get_false_resolve_ceiling`) is the
ONE place the gate knobs are resolved from env and validated. The gates and the
pipeline above read no environment — they take explicit params, staying pure and
testable — so this config layer supplies the validated `tau_sim` / `n_min` /
`faithfulness_cutoff` the support endpoint (US-066+) spreads into
`run_deflection_pipeline` (alongside `retrieval.get_similarity_threshold()` for
`match_threshold`). The **false-resolve ceiling** is a separate eval-time knob —
the one number a buyer sets as their risk tolerance, consumed by the E7 sweep
(US-058) and the E8 gate (US-059) — and is deliberately kept OFF the per-request
path (off `EscalationConfig` entirely) so it cannot leak into the latency path.
"""

from __future__ import annotations

import logging
import os
from typing import Literal

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field

from retrieval import DEFAULT_TOP_K, SearchDocumentsResult, hybrid_search

log = logging.getLogger("agentic_rag.escalation")


class RetrievalGateDecision(BaseModel):
    """Outcome of the cosine retrieval gate — pure data, deterministic.

    `strong=True` means retrieval is good enough to attempt a drafted answer;
    `strong=False` means escalate without drafting. `top1_cosine` is the best
    raw cosine across results (`None` when there were no vector hits at all),
    `n_cleared` is how many rows cleared `match_threshold`, and `reason` is a
    short, machine-stable tag for logging/eval only (every weak reason starts
    with `"weak"`). It is never shown to the customer — the escalation message
    is the generic deferral (US-049), with no gate metadata leaked.
    """

    model_config = ConfigDict(frozen=True)

    strong: bool
    top1_cosine: float | None
    n_cleared: int
    reason: str


def retrieval_gate(
    results: list[SearchDocumentsResult],
    tau_sim: float,
    n_min: int,
    match_threshold: float,
) -> RetrievalGateDecision:
    """Judge retrieval strong/weak from raw cosine alone (ADR-0003).

    `strong = (top1_cosine >= tau_sim) AND (n_cleared >= n_min)`, where
    `top1_cosine` is the max `cosine_similarity` across `results` and
    `n_cleared` counts rows whose `cosine_similarity >= match_threshold`. Empty
    results — or results carrying no cosine at all (keyword-only rows) — are
    weak: there is no calibrated score to clear `tau_sim`.

    Pure arithmetic on scores: no LLM, no reranker, no I/O, so identical inputs
    always yield an identical decision. Range-validation of the knobs is the
    caller's job (US-050 config), not the gate's — the gate is total over any
    floats.
    """
    if not results:
        return RetrievalGateDecision(
            strong=False, top1_cosine=None, n_cleared=0, reason="weak: empty_results"
        )

    cosines = [r.cosine_similarity for r in results if r.cosine_similarity is not None]
    if not cosines:
        # Only keyword-only rows (no embedding) — no cosine to threshold on.
        return RetrievalGateDecision(
            strong=False, top1_cosine=None, n_cleared=0, reason="weak: no_vector_cosine"
        )

    top1_cosine = max(cosines)
    n_cleared = sum(1 for c in cosines if c >= match_threshold)

    cleared_tau = top1_cosine >= tau_sim
    cleared_count = n_cleared >= n_min
    strong = cleared_tau and cleared_count

    if strong:
        reason = "strong"
    elif not cleared_tau:
        reason = f"weak: top1_cosine {top1_cosine:.4f} < tau_sim {tau_sim:.4f}"
    else:
        reason = f"weak: n_cleared {n_cleared} < n_min {n_min}"

    return RetrievalGateDecision(
        strong=strong,
        top1_cosine=top1_cosine,
        n_cleared=n_cleared,
        reason=reason,
    )


# -----------------------------------------------------------------------------
# US-048: one-call runtime faithfulness gate.
#
# IMPORTANT: this is the RUNTIME gate, net-new — NOT the offline RAGAS
# faithfulness metric (`evals/retrieval/ragas.py`). RAGAS decomposes the answer
# into claims and makes several judge calls per answer, weekly and off the
# latency path; this gate makes EXACTLY ONE structured-output call on the cheap
# runtime-judge model and runs inline on every drafted support reply. Same word
# "faithfulness", two different machineries — never conflate them.
# -----------------------------------------------------------------------------

DEFAULT_JUDGE_MODEL = "gpt-4o-mini"

_JUDGE_SYSTEM_PROMPT = (
    "You are a strict faithfulness judge for an automated customer-support "
    "answer. You are given CONTEXT (retrieved document chunks) and a draft "
    "ANSWER. Decide whether EVERY factual claim in the ANSWER is directly "
    "supported by the CONTEXT. An answer that adds facts not in the context, "
    "contradicts the context, or relies on outside knowledge is NOT supported. "
    "An answer that merely says it cannot help, with no unsupported claims, is "
    "trivially supported. Judge only grounding — not tone, completeness, or "
    "helpfulness. Return `supported` and a `score` in [0,1] for how grounded "
    "the answer is (1.0 = every claim clearly supported, 0.0 = clearly "
    "unsupported or contradicted)."
)


class FaithfulnessJudgment(BaseModel):
    """Structured-output schema the runtime judge returns in its single call.

    Kept deliberately tiny — one boolean and one score — so the call is a fast,
    cheap, single round trip (the antithesis of RAGAS claim-decomposition). The
    `[0,1]` bound on `score` is stated in the description and enforced by
    clamping in `faithfulness_gate` rather than as a JSON-schema constraint, so
    strict structured-output mode never rejects a slightly-out-of-range value
    (matching the constraint-free `DocumentMetadata` convention).
    """

    supported: bool = Field(
        ...,
        description=(
            "True iff every factual claim in the ANSWER is directly supported "
            "by the CONTEXT. False if any claim is unsupported, contradicted, "
            "or relies on outside knowledge."
        ),
    )
    score: float = Field(
        ...,
        description=(
            "Confidence in [0,1] that the ANSWER is fully grounded in the "
            "CONTEXT. 1.0 = every claim clearly supported; 0.0 = clearly "
            "unsupported or contradicted."
        ),
    )


class FaithfulnessDecision(BaseModel):
    """Outcome of the runtime faithfulness gate — frozen, like the retrieval
    gate's decision.

    `faithful` is the bottom-line verdict the orchestrator (US-049) acts on:
    `True` ⇒ the drafted answer may auto-send, `False` ⇒ escalate. It is
    `supported AND score >= cutoff`, and is forced `False` on any judge failure
    (fail-closed). `supported` / `score` carry the raw judge output (score
    clamped to `[0,1]`; `0.0` on failure). `reason` is a machine-stable tag for
    logging/eval only — every escalating reason starts with `"unfaithful"` — and
    is never shown to the customer (US-049 returns the generic deferral).
    """

    model_config = ConfigDict(frozen=True)

    faithful: bool
    supported: bool
    score: float
    reason: str


def get_judge_model() -> str:
    """Model for the runtime faithfulness judge (`JUDGE_MODEL` env).

    Defaults to a cheap/fast model (`gpt-4o-mini`) and — unlike the answerer's
    aux-helper selectors — does NOT chain through `OPENAI_MODEL`: the runtime
    judge is deliberately decoupled from the answerer (it has its own `JUDGE_*`
    provider binding too, US-022) and must stay cheap on the request latency
    path, so a big-answerer deployment never silently makes the per-reply gate
    expensive. Selects the model only — the provider/connection is the
    `judge_client` the caller passes in (ADR-0006). On a non-OpenAI judge the
    operator sets `JUDGE_MODEL` to their deployment/model; an unset, wrong model
    just makes the call fail — which fails closed (escalate), never open.
    """
    return os.environ.get("JUDGE_MODEL") or DEFAULT_JUDGE_MODEL


def _render_context(chunks: list[SearchDocumentsResult]) -> str:
    return "\n\n".join(f"[{i + 1}] {c.content}" for i, c in enumerate(chunks))


async def faithfulness_gate(
    judge_client: AsyncOpenAI,
    draft: str,
    chunks: list[SearchDocumentsResult],
    cutoff: float,
    *,
    model: str | None = None,
) -> FaithfulnessDecision:
    """Verify a drafted answer is grounded in its chunks via ONE judge call.

    Makes exactly one `chat.completions.parse` structured-output call on the
    runtime-judge client/model and returns `faithful = supported AND
    score >= cutoff`. Any failure mode — SDK/API error, timeout, refusal, empty
    choices, missing parsed payload — fails **closed**: `faithful=False`
    (escalate), never open. This is the runtime gate, NOT the offline RAGAS
    metric (see the module banner); it never decomposes claims or makes a second
    call.
    """
    resolved_model = model or get_judge_model()
    user_prompt = (
        f"CONTEXT:\n{_render_context(chunks)}\n\n"
        f"ANSWER:\n{draft}\n\n"
        "Is every claim in the ANSWER supported by the CONTEXT?"
    )
    try:
        completion = await judge_client.chat.completions.parse(
            model=resolved_model,
            messages=[
                {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            response_format=FaithfulnessJudgment,
        )
    except Exception as e:  # noqa: BLE001 — any SDK/API/timeout failure fails closed
        log.warning("faithfulness judge call failed: %s", e)
        return _unfaithful("judge_error")

    if not completion.choices:
        log.warning("faithfulness judge returned no choices")
        return _unfaithful("judge_no_choices")
    message = completion.choices[0].message
    if getattr(message, "refusal", None):
        log.warning("faithfulness judge refused: %s", message.refusal)
        return _unfaithful("judge_refusal")
    judgment = getattr(message, "parsed", None)
    if judgment is None:
        log.warning("faithfulness judge returned no parsed payload")
        return _unfaithful("judge_no_payload")

    score = max(0.0, min(1.0, judgment.score))
    faithful = judgment.supported and score >= cutoff
    if faithful:
        reason = "faithful"
    elif not judgment.supported:
        reason = "unfaithful: judge_unsupported"
    else:
        reason = f"unfaithful: score {score:.4f} < cutoff {cutoff:.4f}"
    return FaithfulnessDecision(
        faithful=faithful,
        supported=judgment.supported,
        score=score,
        reason=reason,
    )


def _unfaithful(tag: str) -> FaithfulnessDecision:
    """The fail-closed decision: unfaithful, score 0, escalate."""
    return FaithfulnessDecision(
        faithful=False, supported=False, score=0.0, reason=f"unfaithful: {tag}"
    )


# -----------------------------------------------------------------------------
# US-049: deterministic deflection pipeline orchestrator.
#
# Runs the exact ADR-0003 control flow as plain control flow — never a model
# `escalate()` tool, never the M1 agentic tool loop (`MAX_TOOL_ITERATIONS` in
# main.py). The model drafts an answer; whether that answer SENDS is decided
# here by the two gates, not by the model.
# -----------------------------------------------------------------------------

# The single customer-facing escalation message. ADR-0003: on escalate the
# customer sees ONLY this generic deferral — never the gate `reason`, the
# retrieval scores, or any access metadata. `_escalated` is the sole constructor
# of an escalated result, so this invariant is structurally enforced.
GENERIC_DEFERRAL = (
    "Thanks for reaching out. I don't have enough information to answer this "
    "confidently, so I've passed it along to our team — a human will follow up "
    "with you."
)

DEFAULT_ANSWERER_MODEL = "gpt-4o-mini"

_DRAFT_SYSTEM_PROMPT = (
    "You are a customer-support assistant. Answer the customer's question using "
    "ONLY the information in the provided CONTEXT. Do not use outside knowledge "
    "and do not invent specifics. Quote concrete details (numbers, names, steps) "
    "from the context. If the context does not contain the answer, say briefly "
    "that you don't have that information — never guess. Keep the answer concise "
    "and directly responsive."
)


def get_answerer_model() -> str:
    """Model used to DRAFT the support answer — the answerer role's `OPENAI_MODEL`
    (ADR-0006), default `gpt-4o-mini`. Selects the model only; the provider /
    connection is the `answerer_client` the caller injects."""
    return os.environ.get("OPENAI_MODEL") or DEFAULT_ANSWERER_MODEL


class DeflectionResult(BaseModel):
    """Outcome of the deflection pipeline — frozen, like the gate decisions.

    `customer_message` is the ONLY field ever shown to the customer: the drafted
    answer when `action == "answered"`, the fixed `GENERIC_DEFERRAL` when
    `action == "escalated"`. The remaining fields are internal diagnostics for
    logging and the US-067 conversation status — `retrieval` (always present),
    `faithfulness` (`None` when the retrieval gate short-circuited before any
    draft/judge call), and `reason` (a machine-stable tag that must NEVER be
    surfaced to the customer).
    """

    model_config = ConfigDict(frozen=True)

    action: Literal["answered", "escalated"]
    customer_message: str
    retrieval: RetrievalGateDecision
    faithfulness: FaithfulnessDecision | None
    reason: str

    @property
    def escalated(self) -> bool:
        return self.action == "escalated"


def _escalated(
    retrieval: RetrievalGateDecision,
    faithfulness: FaithfulnessDecision | None,
    reason: str,
) -> DeflectionResult:
    """The sole constructor of an escalated result: `customer_message` is ALWAYS
    the generic deferral, so the gate `reason` can never leak to the customer."""
    return DeflectionResult(
        action="escalated",
        customer_message=GENERIC_DEFERRAL,
        retrieval=retrieval,
        faithfulness=faithfulness,
        reason=reason,
    )


async def draft_support_answer(
    answerer_client: AsyncOpenAI,
    message: str,
    chunks: list[SearchDocumentsResult],
    *,
    model: str | None = None,
) -> str:
    """Draft a support answer grounded in `chunks` via ONE plain chat completion.

    Deliberately a single `chat.completions.create` with NO `tools` — this is
    not the agentic loop; the model only writes prose, it does not decide to
    resolve or call retrieval. Returns the answer text (`""` if the model
    produced none — the caller treats empty as escalate)."""
    resolved_model = model or get_answerer_model()
    completion = await answerer_client.chat.completions.create(
        model=resolved_model,
        messages=[
            {"role": "system", "content": _DRAFT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"CONTEXT:\n{_render_context(chunks)}\n\n"
                    f"CUSTOMER QUESTION:\n{message}"
                ),
            },
        ],
    )
    if not completion.choices:
        return ""
    return completion.choices[0].message.content or ""


async def run_deflection_pipeline(
    *,
    embedder_client: AsyncOpenAI,
    answerer_client: AsyncOpenAI,
    judge_client: AsyncOpenAI,
    http: httpx.AsyncClient,
    supabase_url: str,
    supabase_headers: dict[str, str],
    message: str,
    tau_sim: float,
    n_min: int,
    match_threshold: float,
    faithfulness_cutoff: float,
    top_k: int = DEFAULT_TOP_K,
    answerer_model: str | None = None,
    judge_model: str | None = None,
) -> DeflectionResult:
    """Answer or escalate one support message via the ADR-0003 deflection pipeline.

    Control flow (deterministic, never a model `escalate()` tool, never the M1
    agentic loop):

        retrieve (hybrid, ONCE) → retrieval gate
            → weak  ⇒ escalate now (ZERO draft, ZERO judge calls)
            → strong ⇒ draft → faithfulness gate
                → faithful   ⇒ answer (send the draft)
                → unfaithful ⇒ escalate

    `supabase_headers` MUST carry the customer's/bot's JWT so retrieval runs
    under RLS + the workspace membership clause — the pipeline passes no
    principal or workspace id (ADR-0002). On every escalate the customer sees
    only `GENERIC_DEFERRAL`; the gate `reason` stays on the internal result.

    User-initiated "talk to a human" is NOT handled here — that is a separate
    widget button owned by the support-surface section (US-066+); this pipeline
    only makes the automatic answer-vs-escalate decision.
    """
    # The OR's left operand: cheap, retrieval-grounded. Retrieve ONCE (hybrid),
    # then gate on raw cosine. hybrid_search embeds under the embedder role and
    # forwards only the JWT headers — no workspace id crosses the boundary.
    chunks = await hybrid_search(
        openai_client=embedder_client,
        http=http,
        supabase_url=supabase_url,
        supabase_headers=supabase_headers,
        query=message,
        top_k=top_k,
    )
    retrieval = retrieval_gate(chunks, tau_sim, n_min, match_threshold)
    if not retrieval.strong:
        # Short-circuit: the cheap operand decided. No draft, no judge call.
        return _escalated(retrieval, faithfulness=None, reason=f"retrieval_{retrieval.reason}")

    # The OR's expensive right operand: draft, then verify the draft is grounded.
    try:
        draft = await draft_support_answer(
            answerer_client, message, chunks, model=answerer_model
        )
    except Exception as e:  # noqa: BLE001 — a draft failure escalates (fail closed)
        log.warning("deflection draft generation failed: %s", e)
        return _escalated(retrieval, faithfulness=None, reason="draft_error")
    if not draft.strip():
        return _escalated(retrieval, faithfulness=None, reason="draft_empty")

    faithfulness = await faithfulness_gate(
        judge_client, draft, chunks, faithfulness_cutoff, model=judge_model
    )
    if faithfulness.faithful:
        return DeflectionResult(
            action="answered",
            customer_message=draft,
            retrieval=retrieval,
            faithfulness=faithfulness,
            reason="answered",
        )
    return _escalated(retrieval, faithfulness=faithfulness, reason=faithfulness.reason)


# -----------------------------------------------------------------------------
# US-050: escalation config — typed, validated global knobs (ADR-0003).
#
# The gates (US-047/048) and the pipeline (US-049) take their knobs as explicit
# params and read NO environment themselves — that keeps them pure and testable.
# This section is the one place those knobs are resolved from env, validated
# once, and frozen. It mirrors `retrieval.get_similarity_threshold` (the same
# parse → range-check → clear `ValueError` shape) and the `ProviderConfig`
# value-object convention (frozen pydantic model + `from_env`). The support
# endpoint (US-066+) builds one `EscalationConfig` at startup and spreads its
# fields into `run_deflection_pipeline`, passing `retrieval.get_similarity_
# threshold()` for `match_threshold` — the gate's per-row floor IS the existing
# retrieval similarity threshold, not a new escalation knob.
#
# Per-workspace tuning is deferred but config-SHAPED: a future per-workspace
# override (e.g. an `escalation_config` row keyed by `workspace_id`) would
# resolve ON TOP OF this global default — read the global via `from_env`, then
# overlay the workspace's stored knobs — with NO schema migration implied here.
# v1 is a single global config.
# -----------------------------------------------------------------------------

# ADR-0003 worked-example defaults (US-047/048). Placeholders until the E7 sweep
# (US-058) computes the deflection-maximizing knee under the false-resolve
# ceiling and promotes its recommended knob values here; a buyer overrides any
# of them via the env vars below.
DEFAULT_TAU_SIM = 0.4
DEFAULT_N_MIN = 2
DEFAULT_FAITHFULNESS_CUTOFF = 0.7

# The buyer's single risk-tolerance number: the maximum fraction of
# should-escalate (P3) questions allowed to auto-resolve. Consumed ONLY by the
# E7 sweep / knee selection (US-058) and the E8 CI gate (US-059) — NEVER by the
# per-request pipeline (a single request has no population to take a rate over).
# Conservative default pending the E7 sweep. Kept OFF `EscalationConfig` on
# purpose so it cannot be wired into the latency path by accident.
DEFAULT_FALSE_RESOLVE_CEILING = 0.05


def _env_unit_float(name: str, default: float) -> float:
    """Parse a `[0,1]`-bounded float env knob (mirrors `get_similarity_threshold`).

    Unset/blank ⇒ `default`; a non-float or out-of-range value raises a
    `ValueError` naming the env var, so a fat-fingered knob fails the boot rather
    than silently degrading the gate.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        v = float(raw)
    except ValueError as e:
        raise ValueError(f"{name} must be a float, got {raw!r}") from e
    if not 0.0 <= v <= 1.0:
        raise ValueError(f"{name} must be in [0,1], got {v}")
    return v


def _env_min_int(name: str, default: int, minimum: int) -> int:
    """Parse an integer env knob with an inclusive lower bound (`value >= minimum`)."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        v = int(raw)
    except ValueError as e:
        raise ValueError(f"{name} must be an int, got {raw!r}") from e
    if v < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {v}")
    return v


class EscalationConfig(BaseModel):
    """The three global escalation gate knobs — validated, frozen (US-050).

    Carries ONLY the per-request gate parameters the deflection pipeline
    consumes: `tau_sim` and `n_min` for the retrieval gate (US-047),
    `faithfulness_cutoff` for the faithfulness gate (US-048). It deliberately
    does NOT carry the false-resolve ceiling (`get_false_resolve_ceiling`) — that
    is an eval-time population metric, structurally kept off this object so it
    cannot leak into the latency path.

    The gate's per-row `match_threshold` is NOT here either: it is the existing
    retrieval similarity threshold (`retrieval.get_similarity_threshold`, env
    `SEARCH_SIMILARITY_THRESHOLD`), reused so that "a row cleared retrieval"
    means the same thing in the gate as in retrieval. The endpoint passes it
    alongside these fields.

    The `Field` bounds make the object self-validating on direct construction
    (defense in depth); `from_env` range-checks first and raises a `ValueError`
    naming the offending env var (the operator-facing path).
    """

    model_config = ConfigDict(frozen=True)

    tau_sim: float = Field(..., ge=0.0, le=1.0)
    n_min: int = Field(..., ge=1)
    faithfulness_cutoff: float = Field(..., ge=0.0, le=1.0)

    @classmethod
    def from_env(cls) -> EscalationConfig:
        """Resolve + validate the global escalation knobs from the environment.

        Each knob is parsed and range-checked (`tau_sim`/`faithfulness_cutoff` in
        [0,1], `n_min` >= 1); a non-numeric or out-of-range value raises a
        `ValueError` naming the offending env var. Omitting a knob yields its
        ADR-0003 / E7-sweep default. Call once at startup so a misconfiguration
        fails the boot, not the first support request.
        """
        return cls(
            tau_sim=_env_unit_float("ESCALATION_TAU_SIM", DEFAULT_TAU_SIM),
            n_min=_env_min_int("ESCALATION_N_MIN", DEFAULT_N_MIN, minimum=1),
            faithfulness_cutoff=_env_unit_float(
                "ESCALATION_FAITHFULNESS_CUTOFF", DEFAULT_FAITHFULNESS_CUTOFF
            ),
        )


def get_false_resolve_ceiling() -> float:
    """The buyer's risk-tolerance number: max allowed false-resolve fraction.

    `ESCALATION_FALSE_RESOLVE_CEILING` in [0,1], default
    `DEFAULT_FALSE_RESOLVE_CEILING`. This is "the one number a buyer sets" — the
    ceiling the E7 sweep selects the knee under (US-058) and the E8 CI gate
    enforces (US-059). It is intentionally a STANDALONE getter, not a field on
    `EscalationConfig`, so it stays out of the per-request deflection pipeline
    (the failure mode US-050 guards against).
    """
    return _env_unit_float(
        "ESCALATION_FALSE_RESOLVE_CEILING", DEFAULT_FALSE_RESOLVE_CEILING
    )
