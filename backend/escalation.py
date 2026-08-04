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

Issue #97: `answer_gate` is a SECOND, distinct runtime judge — grounding and
answering are orthogonal, and the faithfulness gate only checks the former. A
draft that says "I don't have that information about X" carries zero unsupported
claims, so the faithfulness judge scores it `supported=True` and (before this
gate) it was auto-sent and counted as a deflection though it answered nothing.
The retrieval gate cannot catch it either: retrieval is *strong* precisely
because the chunk is topically adjacent (the right subject, the wrong fact). So a
separate operand — composed per ADR-0003 as deterministic control flow, not a
model tool — verifies the draft actually ANSWERS the customer's question, and
fails **closed** like the faithfulness gate (any judge error ⇒ escalate). It runs
only on the would-be-answered path (after the draft clears faithfulness), so it
adds ONE judge call to a turn that was about to auto-resolve — exactly the
population at risk — and none to any escalate path.

US-049: `run_deflection_pipeline` wires the gates into the exact ADR-0003
control flow — `retrieve (hybrid, once) → retrieval gate → [if strong] draft →
faithfulness gate → answer gate → answer-or-escalate` — as deterministic control
flow, never a model `escalate()` tool and never the M1 agentic loop
(`MAX_TOOL_ITERATIONS` in `main.py`). The OR short-circuits on its cheap left
operand: a weak retrieval escalates having made ZERO draft and ZERO judge calls.
On any escalate the customer-facing message is a fixed generic deferral with NO
reason/access metadata; the decision tags live only on the internal result
fields (for logging / the US-067 conversation status), never in
`customer_message`.

US-050: `EscalationConfig` (+ the standalone `get_false_resolve_ceiling`) is the
ONE place the gate knobs are resolved from env and validated. The gates and the
pipeline above read no environment — they take explicit params, staying pure and
testable — so this config layer supplies the validated `tau_sim` / `n_min` /
`faithfulness_cutoff` the support endpoint (US-066+) spreads into
`run_deflection_pipeline` (alongside `retrieval.get_similarity_threshold()` for
`match_threshold`). ONE gate knob sits outside that rule and is named here so the
exception is visible rather than discovered: `JUDGE_TEMPERATURE` (issue #104) is
read from env by `_judge_sampling_kwargs()` on EVERY gate invocation, with no
parameter override - unlike `JUDGE_MODEL`, which at least has the gates' `model=`
argument. It is deliberately not an `EscalationConfig` field because it is not a
per-request decision input: it is a property of the JUDGE DEPLOYMENT's request
contract, resolved alongside `get_judge_model()` and never varied per call site,
so threading it through the pipeline would suggest a caller may legitimately
choose a different sampler per turn. The statement above still holds for every
knob `EscalationConfig` does own. The **false-resolve ceiling** is a separate
eval-time knob — the one number a buyer sets as their risk tolerance, consumed by
the E7 sweep (US-058) and the E8 gate (US-059) — and is deliberately kept OFF the
per-request path (off `EscalationConfig` entirely) so it cannot leak into the
latency path.
"""

from __future__ import annotations

import functools
import logging
import math
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


# Both runtime gates are SAFETY gates that fail closed, and a gate that returns a
# different verdict on identical input is not doing that: it is sampling one. The
# 2026-08-03 E7 investigation measured the answer gate returning `answers=True`
# on 2 of 5 identical calls for the same (question, draft) pair — the send/escalate
# decision was partly a coin flip. Both gates now pin the sampler.
#
# What the pin buys, stated exactly: it REMOVES THE SAMPLER as a source of variance
# in the send/escalate verdict. It is not a guarantee that an identical (question,
# draft) can never yield a different verdict - no `seed` is passed, and
# provider-side temperature 0 is best-effort, not contractual. The measured defect
# it removes is real; the residual is model-side nondeterminism, which is a
# different and much smaller thing.
#
# The escape hatch exists because ADR-0006 lets an operator point `JUDGE_MODEL` at
# their own deployment, and not every deployment accepts the argument. There are
# TWO distinct rejections, and they do NOT share a remedy:
#
#   (1) The PARAMETER is refused outright. First-party OpenAI reasoning models
#       (o-series, `gpt-5-*`) do this, at ANY value including the pinned default,
#       so it is a 400 on every call from the moment the deployment is pointed
#       there. The remedy is `JUDGE_TEMPERATURE=none`, which omits the parameter
#       entirely - there is no number that works.
#
#   (2) The VALUE is refused as outside that endpoint's accepted range. This one
#       CANNOT fire at the shipped default: `DEFAULT_JUDGE_TEMPERATURE` is 0.0,
#       which is inside every provider's range including an Anthropic-compatible
#       endpoint's `[0,1]`. It only happens once an operator has explicitly set a
#       number above that endpoint's cap, and the remedy is then to set a value
#       the endpoint accepts (`0` keeps the gates pinned). Reaching for the
#       opt-out here would un-pin a safety gate the operator could have kept
#       pinned; it is the alternative only if they would rather send nothing at
#       all. `_JUDGE_TEMPERATURE_MAX` does not protect against this and is not
#       trying to: a validator cannot know each bring-your-own endpoint's range.
#
# Either way the escape hatch is a typed-out operator decision rather than
# something inferred from a provider's error text - guessing at free-text 400s on a
# safety path is unsafe in both directions, since reading one too loosely un-pins a
# gate on an echoed payload and reading one too strictly leaves the gate failing
# closed on every turn. Setting the knob to a number is an explicit operator
# decision to give up the determinism these gates depend on.
#
# The knob resolves as: unset ⇒ the pinned default; blank ⇒ the pinned default;
# `none` ⇒ omit the parameter; anything else ⇒ a number, or the pinned default.
# Blank is deliberately NOT the opt-out. An empty environment variable is a common
# accidental state - a bare `-e JUDGE_TEMPERATURE` in Docker, an empty configMap
# value, a trailing `JUDGE_TEMPERATURE=` in a .env - and treating that accident as
# an opt-out silently returns a safety gate to sampling, which is the exact defect
# this knob exists to remove. Every sibling knob in this module (`_env_unit_float`,
# `_env_min_int`) already reads unset/blank as the default; this one being
# different was the bug, not the convention. Un-pinning must be typed out.
DEFAULT_JUDGE_TEMPERATURE = 0.0

# The range providers accept for a chat-completion `temperature`. A value outside
# it is a 400 on every call, which fails both gates closed on every turn - and the
# latch site cannot tell that from a deliberate escalate, so the conversations it
# hits are permanently silenced (issue #105). Reachable by a fat-fingered digit, so
# it falls back rather than shipping. This is OpenAI's range and stays that way: a
# bring-your-own endpoint with a NARROWER one is a provider disagreement no
# validator can enumerate, so clearing this bound is necessary and not sufficient -
# an operator whose endpoint refuses the number they chose lowers it to one that
# endpoint accepts, not a wider or provider-forked bound here.
_JUDGE_TEMPERATURE_MAX = 2.0


@functools.lru_cache(maxsize=None)
def _resolve_judge_temperature(raw: str | None) -> float | None:
    """Parse one raw `JUDGE_TEMPERATURE` value; see `get_judge_temperature`.

    Cached on the raw string so a MISCONFIGURED knob warns once per distinct
    value rather than once per judge call. `_judge_sampling_kwargs` runs on every
    gate invocation and both gates run on every customer turn, so an uncached
    warning would emit two lines per turn forever for a static config error and
    bury the very misconfiguration it is trying to surface. Keying on the raw
    value (rather than caching the resolved result outright) means the env is
    still read on every call, so a caller that changes `JUDGE_TEMPERATURE` — the
    tests do — can never read a stale value.
    """
    if raw is None or raw.strip() == "":
        return DEFAULT_JUDGE_TEMPERATURE
    if raw.strip().lower() == "none":
        return None
    try:
        value = float(raw)
    except ValueError:
        log.warning(
            "JUDGE_TEMPERATURE=%r is not a number; using the deterministic "
            "default %.1f",
            raw,
            DEFAULT_JUDGE_TEMPERATURE,
        )
        return DEFAULT_JUDGE_TEMPERATURE
    if not math.isfinite(value) or not 0.0 <= value <= _JUDGE_TEMPERATURE_MAX:
        log.warning(
            "JUDGE_TEMPERATURE=%r is not a finite value in [0,%.1f]; the judge API "
            "would reject it on every call, failing both gates closed and "
            "permanently latching every affected conversation, so using the "
            "deterministic default %.1f instead",
            raw,
            _JUDGE_TEMPERATURE_MAX,
            DEFAULT_JUDGE_TEMPERATURE,
        )
        return DEFAULT_JUDGE_TEMPERATURE
    return value


def get_judge_temperature() -> float | None:
    """Sampling temperature for the two runtime gates (`JUDGE_TEMPERATURE` env).

    Resolves as: unset ⇒ `DEFAULT_JUDGE_TEMPERATURE`; blank ⇒
    `DEFAULT_JUDGE_TEMPERATURE`; the literal `none` ⇒ `None`, meaning "send no
    `temperature` at all". That is the documented remedy for exactly one failure
    (ADR-0006): a judge deployment that refuses the PARAMETER itself, as
    first-party OpenAI reasoning models (o-series, `gpt-5-*`) do at any value
    including the pinned default. An endpoint that instead refuses a VALUE as
    outside its own narrower range is a different case with a different remedy -
    it cannot fire at the pinned default, and the fix is a number that endpoint
    accepts (see the `DEFAULT_JUDGE_TEMPERATURE` block). The typed-out `none` is
    the ONLY way to un-pin: a deliberate opt-out is legitimate, an accidental one
    is not.

    A value that is not a number, is not finite, or falls outside
    `[0, _JUDGE_TEMPERATURE_MAX]` warns and falls back to the deterministic
    default. Note the deliberate difference from the sibling `_env_unit_float`,
    which RAISES on the same input: this knob must not fail the boot, because
    falling back to the pinned default is always safe here, whereas raising would
    take the gate offline entirely.
    """
    return _resolve_judge_temperature(os.environ.get("JUDGE_TEMPERATURE"))


def _judge_sampling_kwargs() -> dict[str, float]:
    """Sampling kwargs both runtime gates splat into their one judge call."""
    temperature = get_judge_temperature()
    return {} if temperature is None else {"temperature": temperature}


# Model-name prefixes of judge deployments KNOWN to refuse the `temperature`
# parameter outright - case (1) of the `DEFAULT_JUDGE_TEMPERATURE` block. This is a
# hand-maintained list of names observed to reject the argument, and it is the ONLY
# input to the boot check below: nothing here looks at any provider response.
#
# Edit THIS tuple when a new refusing model ships. It WILL go stale - the list
# cannot know about models released after it was written, and it cannot see a
# bring-your-own endpoint that refuses the parameter under an unrelated name. A
# `JUDGE_MODEL` it does not recognise gets NO warning at all.
_TEMPERATURE_REFUSING_MODEL_PREFIXES = ("o1", "o3", "o4", "gpt-5")


def warn_if_judge_rejects_temperature() -> None:
    """Log ONCE at boot if the judge model is a KNOWN temperature-refuser.

    Best-effort operator aid, nothing more. It compares the configured
    `JUDGE_MODEL` against the hand-maintained
    `_TEMPERATURE_REFUSING_MODEL_PREFIXES` and warns when the pin is also in
    effect. It does NOT guarantee detection, does not prevent the breakage, and
    does not make the upgrade safe: a refusing deployment whose name is not in that
    tuple - a newer model, or an OpenAI-compatible endpoint under any other name -
    is missed silently. The whole claim is that it reduces the chance of a silent
    surprise for the names we already know about.

    It exists because the pin is a NEW request parameter on an upgrade path: an
    operator already running one of these models has a working deployment today,
    and after the pin every judge call 400s, both gates fail closed on every turn,
    and per issue #105 the latch site cannot tell that from a deliberate escalate,
    so affected conversations latch to `escalated` permanently. Docs alone do not
    reach an operator mid-upgrade.

    Boot-time only, by construction: it is called from the startup hook, never from
    `_judge_parse` or either gate, so it adds nothing to the request path and can
    never alter a gate decision. It never raises and never blocks startup - a
    warning that could take the service down would be worse than the surprise it
    warns about.
    """
    if get_judge_temperature() is None:
        return
    model = get_judge_model()
    if not model.lower().startswith(_TEMPERATURE_REFUSING_MODEL_PREFIXES):
        return
    log.warning(
        "judge_temperature.known_refusing_model JUDGE_MODEL=%r is a known "
        "reasoning model that rejects the `temperature` parameter with a 400. The "
        "faithfulness and answer gates send temperature=%s on every call, so every "
        "judge call will fail and BOTH GATES WILL FAIL CLOSED ON EVERY TURN; per "
        "issue #105 the latch site cannot tell that from a deliberate escalate, so "
        "affected conversations latch to status='escalated' permanently and "
        "repairing the configuration does not un-latch them. Remedy: set "
        "JUDGE_TEMPERATURE=none to omit the parameter. This check matches only the "
        "known names in escalation._TEMPERATURE_REFUSING_MODEL_PREFIXES and is "
        "best-effort: a refusing deployment under any other name gets no warning.",
        model,
        get_judge_temperature(),
    )


async def _judge_parse(
    judge_client: AsyncOpenAI,
    *,
    model: str,
    messages: list[dict[str, str]],
    response_format: type[BaseModel],
) -> object:
    """The ONE structured-output judge call both runtime gates make.

    Exactly one call, with no exception: the sampling kwargs resolved from
    `JUDGE_TEMPERATURE` are splatted straight in. There is deliberately no retry
    and no per-model learning here. A judge deployment that will not accept the
    `temperature` PARAMETER is a CONFIGURATION fact an operator states once with
    `JUDGE_TEMPERATURE=none`, not something this module infers from a provider's
    free-text 400 - see the `DEFAULT_JUDGE_TEMPERATURE` block for why that
    inference is unsafe in both directions on a safety path, and for the separate
    case of an endpoint refusing an operator-chosen VALUE as out of its range.

    Every failure - auth, rate limit, timeout, network, any 400 - propagates
    unchanged to the caller's fail-closed handler. This function exists so both
    gates agree on sampling in one place rather than two.
    """
    return await judge_client.chat.completions.parse(
        model=model,
        messages=messages,
        response_format=response_format,
        **_judge_sampling_kwargs(),
    )


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

    Sampled at `JUDGE_TEMPERATURE` (default 0) - see `get_judge_temperature`.
    """
    resolved_model = model or get_judge_model()
    user_prompt = (
        f"CONTEXT:\n{_render_context(chunks)}\n\n"
        f"ANSWER:\n{draft}\n\n"
        "Is every claim in the ANSWER supported by the CONTEXT?"
    )
    try:
        completion = await _judge_parse(
            judge_client,
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
# Issue #97: one-call runtime ANSWER-COMPLETENESS gate.
#
# The faithfulness gate proves the draft doesn't LIE; it says nothing about
# whether the draft ANSWERS. A grounded non-answer ("I don't have that
# information about X") is trivially faithful — zero unsupported claims — so
# without this gate it auto-sends and is counted as a deflection though the
# customer got nothing. This is a SECOND, orthogonal judge call that verifies the
# draft actually addresses the customer's question, and — like the faithfulness
# gate — fails **closed**: any judge error / refusal / parse failure / timeout is
# treated as a non-answer (⇒ escalate), never auto-sent. It compares the QUESTION
# against the DRAFT (NOT the chunks — grounding is the other gate's job); it is
# the runtime companion to the OFFLINE-only `answer_relevancy` RAGAS metric
# (`evals/retrieval/ragas.py`), which gates CI regressions, never an individual
# customer reply.
#
# Issue #104: the rubric originally caught only the shape that ANNOUNCES itself —
# a draft saying it lacks the information. It missed the shape where the CORPUS's
# own answer is a deferral. Asked "what is the return shipping fee for a return
# over 20 lbs?", the corpus says "quoted per-case for returns over 20 lbs"
# (`db_seed/corpus/returns-process.md:33`); a draft restating that is faithful,
# fluent, and occupies exactly the slot the question asked about, so the gate read
# the slot as filled and auto-sent. The customer still has no fee. Same for a book
# warranty "at the discretion of customer service"
# (`db_seed/corpus/warranty-terms.md:29`).
#
# Those two shapes are ORTHOGONAL, and the fix has to name the second explicitly:
# whether the draft admits ignorance is a fact about the DRAFTER, whether the
# customer ends up holding the requested value is a fact about the ANSWER. Before
# the added clause the gate keyed mostly on the first, which the drafter emits or
# omits at its own sampling temperature — so the send/escalate decision partly rode
# on a phrasing coin flip (`backend/test_answer_gate_rubric.py` pins both shapes).
# -----------------------------------------------------------------------------

_ANSWER_JUDGE_SYSTEM_PROMPT = (
    "You are a strict answer-completeness judge for an automated customer-support "
    "reply. You are given the customer's QUESTION and a draft ANSWER. Decide "
    "whether the ANSWER actually answers the QUESTION — that it provides the "
    "specific information the customer asked for. A reply that says it does not "
    "have the information, that it cannot help, that it is unsure, or that it is "
    "deferring the customer to a human, or that answers only a DIFFERENT question "
    "than the one asked, does NOT answer the question. A reply that only tells the "
    "customer the answer is quoted case-by-case, is set at someone's discretion, is "
    "decided by staff, or is otherwise not published does NOT answer the question "
    "either: the customer still does not have the specific information they asked "
    "for, however accurately or confidently the reply states that policy. Judge "
    "ONLY whether the "
    "question is answered — not grounding, tone, or politeness (a blunt but "
    "responsive answer still answers; a warm apology that gives no information "
    "does not). Return `answers` and a `score` in [0,1] for how completely the "
    "QUESTION is answered (1.0 = fully and directly answered, 0.0 = not answered "
    "at all)."
)


class AnswerJudgment(BaseModel):
    """Structured-output schema the answer-completeness judge returns per call.

    Mirrors `FaithfulnessJudgment` — one boolean and one score for a single fast
    round trip. The `[0,1]` bound on `score` is stated in the description and
    enforced by clamping in `answer_gate`, not as a JSON-schema constraint, so
    strict structured-output mode never rejects a slightly-out-of-range value.
    """

    answers: bool = Field(
        ...,
        description=(
            "True iff the ANSWER actually answers the customer's QUESTION with "
            "the specific information requested. False if it defers, says it "
            "lacks the information, cannot help, answers a different question, "
            "or only reports that the requested value is case-by-case, "
            "discretionary, or unpublished."
        ),
    )
    score: float = Field(
        ...,
        description=(
            "Confidence in [0,1] that the QUESTION is fully and directly "
            "answered. 1.0 = fully answered; 0.0 = not answered at all."
        ),
    )


class AnswerDecision(BaseModel):
    """Outcome of the runtime answer-completeness gate — frozen, like the other
    gate decisions.

    `answers` is the bottom-line verdict the orchestrator (US-049) acts on:
    `True` ⇒ the (already-faithful) draft may auto-send, `False` ⇒ escalate. It
    is `addressed AND score >= cutoff`, forced `False` on any judge failure
    (fail-closed). `addressed` / `score` carry the raw judge output (score clamped
    to `[0,1]`; `0.0` on failure). `reason` is a machine-stable tag for logging /
    eval only — every escalating reason starts with `"non_answer"` — and is never
    shown to the customer (US-049 returns the generic deferral).
    """

    model_config = ConfigDict(frozen=True)

    answers: bool
    addressed: bool
    score: float
    reason: str


async def answer_gate(
    judge_client: AsyncOpenAI,
    question: str,
    draft: str,
    cutoff: float,
    *,
    model: str | None = None,
) -> AnswerDecision:
    """Verify a drafted answer actually answers the question via ONE judge call.

    Makes exactly one `chat.completions.parse` structured-output call on the
    runtime-judge client/model and returns `answers = addressed AND
    score >= cutoff`. Any failure mode — SDK/API error, timeout, refusal, empty
    choices, missing parsed payload — fails **closed**: `answers=False`
    (escalate), never open. Compares the QUESTION against the DRAFT only; grounding
    is `faithfulness_gate`'s job, not this gate's.

    Sampled at `JUDGE_TEMPERATURE` (default 0) - see `get_judge_temperature`.
    """
    resolved_model = model or get_judge_model()
    user_prompt = (
        f"QUESTION:\n{question}\n\n"
        f"ANSWER:\n{draft}\n\n"
        "Does the ANSWER actually answer the QUESTION?"
    )
    try:
        completion = await _judge_parse(
            judge_client,
            model=resolved_model,
            messages=[
                {"role": "system", "content": _ANSWER_JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            response_format=AnswerJudgment,
        )
    except Exception as e:  # noqa: BLE001 — any SDK/API/timeout failure fails closed
        log.warning("answer-completeness judge call failed: %s", e)
        return _non_answer("judge_error")

    if not completion.choices:
        log.warning("answer-completeness judge returned no choices")
        return _non_answer("judge_no_choices")
    message = completion.choices[0].message
    if getattr(message, "refusal", None):
        log.warning("answer-completeness judge refused: %s", message.refusal)
        return _non_answer("judge_refusal")
    judgment = getattr(message, "parsed", None)
    if judgment is None:
        log.warning("answer-completeness judge returned no parsed payload")
        return _non_answer("judge_no_payload")

    score = max(0.0, min(1.0, judgment.score))
    answers = judgment.answers and score >= cutoff
    if answers:
        reason = "answers"
    elif not judgment.answers:
        reason = "non_answer: judge_unaddressed"
    else:
        reason = f"non_answer: score {score:.4f} < cutoff {cutoff:.4f}"
    return AnswerDecision(
        answers=answers,
        addressed=judgment.answers,
        score=score,
        reason=reason,
    )


# The tags `_non_answer` is called with - the branches where the JUDGE ITSELF
# failed, as opposed to `judge_unaddressed` / `score < cutoff`, which are verdicts
# the judge actually reached. Both shapes fail closed to `answers=False`, so the
# `reason` is the ONLY thing that tells them apart; a caller measuring what the
# rubric concludes must not count a dead judge as a non-answer verdict (invariant
# 12: measured nothing must never be reportable as a measurement). Note that
# `judge_unaddressed` shares the `judge_` prefix, so this is an exact-match set
# rather than a prefix test.
JUDGE_FAILURE_TAGS = ("judge_error", "judge_no_choices", "judge_refusal", "judge_no_payload")


def _non_answer(tag: str) -> AnswerDecision:
    """The fail-closed decision: the judge itself failed, escalate.

    NEVER raises, including on an unregistered tag. Every fail-closed branch of
    `answer_gate` returns through here - one of them the blanket
    `except Exception` handler - and that function's contract is that it always
    yields an escalate decision on the customer request path. So an unregistered
    tag logs and still escalates; it must not turn a graceful escalate into an
    exception (AGENTS.md invariant 4).

    The anti-drift guarantee - a new failure branch cannot be added without
    `judge_failure_tag` learning to recognise it, drift that would leave a dead
    judge once again indistinguishable from a real non-answer verdict - is a
    SOURCE-level property, so it is pinned statically by
    `test_answer_gate_rubric.test_every_non_answer_tag_is_registered`: it reads
    these call sites with `ast` and asserts set-equality with
    `JUDGE_FAILURE_TAGS`. That holds unconditionally, unlike an `assert`, which
    `python -O` strips out of an optimized deployment entirely.
    """
    if tag not in JUDGE_FAILURE_TAGS:
        log.error(
            "unregistered judge failure tag %r: judge_failure_tag() will report this "
            "dead judge as a real non-answer verdict until the tag is added to "
            "JUDGE_FAILURE_TAGS",
            tag,
        )
    return AnswerDecision(
        answers=False, addressed=False, score=0.0, reason=f"non_answer: {tag}"
    )


def judge_failure_tag(reason: str) -> str | None:
    """The `JUDGE_FAILURE_TAGS` entry an `AnswerDecision.reason` reports, if any.

    `None` means the judge was reached and returned a verdict (including a
    legitimate `judge_unaddressed`), so the decision reflects the rubric.
    """
    for tag in JUDGE_FAILURE_TAGS:
        if reason == f"non_answer: {tag}":
            return tag
    return None


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
    draft/judge call), `answer` (the answer-completeness decision, issue #97;
    `None` unless the draft reached the answer gate — i.e. it cleared
    faithfulness), and `reason` (a machine-stable tag that must NEVER be surfaced
    to the customer). An `action == "answered"` result ALWAYS carries a non-None
    `answer` with `answers=True`: the gate is a mandatory operand on the send
    path, never skipped.
    """

    model_config = ConfigDict(frozen=True)

    action: Literal["answered", "escalated"]
    customer_message: str
    retrieval: RetrievalGateDecision
    faithfulness: FaithfulnessDecision | None
    answer: AnswerDecision | None = None
    reason: str

    @property
    def escalated(self) -> bool:
        return self.action == "escalated"


def _escalated(
    retrieval: RetrievalGateDecision,
    faithfulness: FaithfulnessDecision | None,
    reason: str,
    answer: AnswerDecision | None = None,
) -> DeflectionResult:
    """The sole constructor of an escalated result: `customer_message` is ALWAYS
    the generic deferral, so the gate `reason` can never leak to the customer.

    `answer` is the answer-completeness decision (issue #97) when the escalate
    was that gate's call; `None` on every earlier escalate (retrieval
    short-circuit, draft error/empty, unfaithful draft) where the answer gate
    never ran."""
    return DeflectionResult(
        action="escalated",
        customer_message=GENERIC_DEFERRAL,
        retrieval=retrieval,
        faithfulness=faithfulness,
        answer=answer,
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
    answer_cutoff: float,
    top_k: int = DEFAULT_TOP_K,
    answerer_model: str | None = None,
    judge_model: str | None = None,
    workspace_id: str | None = None,
) -> DeflectionResult:
    """Answer or escalate one support message via the ADR-0003 deflection pipeline.

    Control flow (deterministic, never a model `escalate()` tool, never the M1
    agentic loop):

        retrieve (hybrid, ONCE) → retrieval gate
            → weak  ⇒ escalate now (ZERO draft, ZERO judge calls)
            → strong ⇒ draft → faithfulness gate
                → unfaithful ⇒ escalate (answer gate never runs)
                → faithful   ⇒ answer-completeness gate (issue #97)
                    → answers    ⇒ answer (send the draft)
                    → non-answer ⇒ escalate (a grounded non-answer is NOT a
                                   deflection — it answered nothing)

    `supabase_headers` MUST carry the customer's/bot's JWT so retrieval runs
    under RLS + the workspace membership clause — that membership, resolved from
    `auth.uid()`, IS the trust boundary (ADR-0002), never a passed id. `workspace_id`
    is the separate, optional, NON-security active-workspace narrowing filter
    (US-070): when the support-bot turn passes the conversation's `workspace_id`
    it narrows retrieval to that one workspace's documents; `None` (the
    knowledge-assistant path) leaves retrieval exactly as before. It is forwarded
    to `hybrid_search` as the ordinary `filter_workspace_id` param, distinct from
    the JWT that carries identity. On every escalate the customer sees only
    `GENERIC_DEFERRAL`; the gate `reason` stays on the internal result.

    User-initiated "talk to a human" is NOT handled here — that is a separate
    widget button owned by the support-surface section (US-066+); this pipeline
    only makes the automatic answer-vs-escalate decision.
    """
    # The OR's left operand: cheap, retrieval-grounded. Retrieve ONCE (hybrid),
    # then gate on raw cosine. hybrid_search embeds under the embedder role and
    # forwards the JWT headers (identity/boundary) plus the optional non-security
    # `workspace_id` narrowing filter — the boundary stays auth.uid()-resolved.
    chunks = await hybrid_search(
        openai_client=embedder_client,
        http=http,
        supabase_url=supabase_url,
        supabase_headers=supabase_headers,
        query=message,
        top_k=top_k,
        workspace_id=workspace_id,
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
    if not faithfulness.faithful:
        return _escalated(
            retrieval, faithfulness=faithfulness, reason=faithfulness.reason
        )

    # Issue #97: a faithful draft still may not ANSWER — a grounded "I don't have
    # that information" is trivially faithful. The answer gate is the second,
    # orthogonal operand on the send path; it fails closed. It runs ONLY here
    # (after the draft cleared faithfulness), so it adds one judge call to a turn
    # that was about to auto-resolve and none to any escalate path.
    answer = await answer_gate(
        judge_client, message, draft, answer_cutoff, model=judge_model
    )
    if not answer.answers:
        return _escalated(
            retrieval, faithfulness=faithfulness, reason=answer.reason, answer=answer
        )

    return DeflectionResult(
        action="answered",
        customer_message=draft,
        retrieval=retrieval,
        faithfulness=faithfulness,
        answer=answer,
        reason="answered",
    )


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
# Issue #97 answer-completeness gate cutoff: the minimum `answers` score a
# faithful draft must clear to auto-send. A clear non-answer scores near 0 and a
# real answer near 1, so a mid default cleanly separates them; conservative
# pending the E7 sweep, which should price the false-*escalate* trade this gate
# introduces rather than assume it.
DEFAULT_ANSWER_CUTOFF = 0.5

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
    `faithfulness_cutoff` for the faithfulness gate (US-048), and `answer_cutoff`
    for the answer-completeness gate (issue #97). It deliberately does NOT carry
    the false-resolve ceiling (`get_false_resolve_ceiling`) — that is an
    eval-time population metric, structurally kept off this object so it cannot
    leak into the latency path.

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
    answer_cutoff: float = Field(..., ge=0.0, le=1.0)

    @classmethod
    def from_env(cls) -> EscalationConfig:
        """Resolve + validate the global escalation knobs from the environment.

        Each knob is parsed and range-checked (`tau_sim`/`faithfulness_cutoff`/
        `answer_cutoff` in [0,1], `n_min` >= 1); a non-numeric or out-of-range
        value raises a `ValueError` naming the offending env var. Omitting a knob
        yields its ADR-0003 / E7-sweep default. Call once at startup so a
        misconfiguration fails the boot, not the first support request.
        """
        return cls(
            tau_sim=_env_unit_float("ESCALATION_TAU_SIM", DEFAULT_TAU_SIM),
            n_min=_env_min_int("ESCALATION_N_MIN", DEFAULT_N_MIN, minimum=1),
            faithfulness_cutoff=_env_unit_float(
                "ESCALATION_FAITHFULNESS_CUTOFF", DEFAULT_FAITHFULNESS_CUTOFF
            ),
            answer_cutoff=_env_unit_float(
                "ESCALATION_ANSWER_CUTOFF", DEFAULT_ANSWER_CUTOFF
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
