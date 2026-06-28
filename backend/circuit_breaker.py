"""US-077 (ADR-0008): per-workspace cost/qps circuit breaker → zero-cost deferral.

The public widget surface drives PAID retrieval + LLM draft/judge calls per
customer turn (the ADR-0003 deflection pipeline, `support_bot.run_bot_deflection_turn`).
US-076 already caps *who* may hammer the surface (per-key + per-session/IP windows);
this module adds the orthogonal **per-workspace ceiling**: a coarse aggregate
qps/cost budget for one workspace's whole bot, so that even legitimate-looking
distributed traffic (many sessions, many keys, all real) cannot run a workspace's
LLM bill away. When that ceiling is breached the breaker TRIPS and the turn
short-circuits to a generic deferral having made **zero retrieval and zero LLM
calls** — a tripped breaker costs ~nothing.

Three load-bearing properties (ADR-0008):

  * SAME SEAM, DIFFERENT BUCKET. The breaker draws down the US-075 `RateLimiter`
    seam exactly like US-076, on a `ws:<workspace_id>` bucket. No new store, no new
    migration — it reuses `rate_limit_counters`. The bucket key is opaque to the
    seam (which only counts); the per-workspace MEANING lives here.

  * ZERO-COST WHEN TRIPPED. `run_breaker_guarded_turn` checks the breaker BEFORE
    invoking the (injected) deflection turn, so a tripped breaker never reaches
    retrieval or the LLM. This is the heart of the story: the short-circuit is
    structural (the turn thunk is simply not awaited), not a flag the pipeline
    checks partway through.

  * ESCALATE, DON'T "RESOLVE". A tripped breaker is a HANDOFF, not a bot answer:
    it emits the generic deferral AND escalates the conversation (the optional
    `on_trip` hook — US-080 wires it to the US-067 status latch). Escalating sets
    `escalated_at`, so the conversation reads as *human-handled* by the derivable
    deflection metric (`resolved AND escalated_at IS NULL` => deflected) and is
    therefore NEVER counted as a deflection (AC). It also never fabricates a
    partial/garbage answer — `GuardedTurnResult.turn` is `None` when tripped.

OPERATOR BADGE = IN-APP REALTIME, ZERO OUTBOUND (same posture as escalation-notify).
"Emit a badge" is not an outbound call. The trip escalates the conversation in
Postgres; the operator dashboard (US-087 `/support/queue`) watches
`status='escalated'` live via the agent's OWN Supabase Realtime `postgres_changes`
subscription under the agent's real JWT. So the badge surfaces purely in-app over
the existing DB + Supabase Realtime — no ESP, no webhook, no Slack. This module
owns only the DECISION and the deferral; the concrete escalation write is the
`on_trip` hook the runtime injects (US-080), which is itself a plain DB write.

CALL SITE: like US-075's seam, the live call site lands with the message-turn
runtime (US-078 lazy conversation creation / US-079 deflection streaming /
US-080 escalation latch). This story ships the breaker primitive + the guarded
runner + `main._check_workspace_breaker`, all unit-tested; US-078–080 invoke
`run_breaker_guarded_turn` (or `main._check_workspace_breaker`) on the message
path with a real turn thunk + escalation hook.

Test: `python -m backend.test_us077_circuit_breaker`.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from pydantic import BaseModel, ConfigDict

from escalation import DeflectionResult
from rate_limiting import RateLimiter

log = logging.getLogger(__name__)

# The single customer-facing message a tripped breaker shows. Like
# escalation.GENERIC_DEFERRAL it is GENERIC ON PURPOSE: it routes the customer to
# a human and leaks NOTHING operational — no workspace id, no counts, no scope, no
# "you are rate limited / your DoS is working". It differs from the retrieval/
# faithfulness deferral only in framing ("high volume" vs "not enough info"),
# which is the PRD's user-facing wording; an alternative is to reuse the exact
# escalation deferral so a customer cannot even distinguish a breaker trip from a
# normal escalation. Either way the customer never learns *why*.
GENERIC_BREAKER_DEFERRAL = (
    "Thanks for reaching out. We're handling an unusually high volume of requests "
    "right now, so I've passed your message along to our team — a human will "
    "follow up with you."
)

# Machine-stable reason tag for ops logging / the conversation's internal state.
# NEVER surfaced to the customer (the customer sees only GENERIC_BREAKER_DEFERRAL),
# mirroring escalation.py's `reason` discipline.
BREAKER_REASON = "circuit_breaker_workspace"


class BreakerDecision(BaseModel):
    """The outcome of one per-workspace breaker check — frozen, like the gate
    decisions in escalation.py and `RateLimitDecision` in rate_limiting.py.

    `tripped` is the inverse of the underlying sliding-window `allowed`: the
    workspace's aggregate budget for this window is spent, so this turn must NOT
    run the (paid) pipeline. `count`/`limit`/`window_seconds` are the underlying
    seam estimate, kept for ops logging (never returned to the customer).
    """

    model_config = ConfigDict(frozen=True)

    tripped: bool
    count: int
    limit: int
    window_seconds: int


class GuardedTurnResult(BaseModel):
    """Outcome of a breaker-guarded customer turn.

    `tripped=True`  => the breaker fired: `customer_message` is the generic
                       deferral, `turn` is `None` (NO answer fabricated, NO
                       retrieval/LLM ran), and the conversation was escalated via
                       the `on_trip` hook (human handoff, not a deflection).
    `tripped=False` => the deflection turn ran normally: `turn` is its
                       `DeflectionResult` and `customer_message` mirrors
                       `turn.customer_message`.
    """

    model_config = ConfigDict(frozen=True)

    tripped: bool
    customer_message: str
    breaker: BreakerDecision
    turn: DeflectionResult | None


def workspace_breaker_key(workspace_id: str) -> str:
    """The opaque seam bucket for a workspace's aggregate budget: `ws:<id>`.

    Distinct namespace from US-076's `key:` / `ip:` buckets, so the per-workspace
    ceiling never conflates with the per-key or per-session windows even at the
    same window size.
    """
    return f"ws:{workspace_id}"


async def check_workspace_breaker(
    limiter: RateLimiter | None,
    workspace_id: str,
    *,
    limit: int,
    window_seconds: int,
    cost: int = 1,
) -> BreakerDecision:
    """Charge the per-workspace `ws:<id>` window; return whether the breaker tripped.

    Draws down the US-075 seam exactly like US-076's `_charge_widget_window`, with
    the SAME two safety stances:

      * NO-OP (never trips) when `limiter is None` — support is unconfigured, so
        the widget endpoints 503 before reaching anything costly; there is nothing
        to break. A built limiter IS the enforcement; its absence is an inert
        surface, never an unprotected one.

      * FAILS OPEN (never trips) on ANY limiter-backend error. `rate_limit_counters`
        live in the SAME Postgres as the resolve/retrieval path, so a transient
        counter-RPC glitch must NOT convert every customer turn into a deferral
        (which would itself be a denial of service, and a self-inflicted one). A
        concise warning is logged (no per-request traceback flood); availability
        wins and the edge/WAF limiter (P5) is the hard bound. NOTE the asymmetry
        vs US-076: a per-request 429 failing open just skips one throttle, whereas
        a breaker failing CLOSED would defer the workspace's ENTIRE traffic — so
        fail-open is even more clearly correct here.

    A blocked hit still counts (the seam guarantees it), so a workspace that keeps
    hammering stays tripped while one that backs off recovers as the window slides.
    """
    if limiter is None:
        return BreakerDecision(
            tripped=False, count=0, limit=limit, window_seconds=window_seconds
        )
    try:
        decision = await limiter.hit(
            workspace_breaker_key(workspace_id),
            limit=limit,
            window_seconds=window_seconds,
            cost=cost,
        )
    except Exception as e:  # noqa: BLE001 - fail OPEN on any backend error
        log.warning(
            "circuit_breaker.limiter_error workspace=%s — failing open (%s)",
            workspace_id,
            e.__class__.__name__,
        )
        return BreakerDecision(
            tripped=False, count=0, limit=limit, window_seconds=window_seconds
        )
    if not decision.allowed:
        log.info(
            "circuit_breaker.tripped workspace=%s window=%ds count=%d/%d",
            workspace_id,
            window_seconds,
            decision.count,
            decision.limit,
        )
    return BreakerDecision(
        tripped=not decision.allowed,
        count=decision.count,
        limit=decision.limit,
        window_seconds=decision.window_seconds,
    )


async def run_breaker_guarded_turn(
    *,
    limiter: RateLimiter | None,
    workspace_id: str,
    limit: int,
    window_seconds: int,
    run_turn: Callable[[], Awaitable[DeflectionResult]],
    on_trip: Callable[[], Awaitable[None]] | None = None,
    cost: int = 1,
) -> GuardedTurnResult:
    """Run one customer turn behind the per-workspace breaker.

    Checks the breaker FIRST. If it trips, `run_turn` is NEVER awaited — so the
    (paid) ADR-0003 deflection pipeline makes zero retrieval and zero LLM calls —
    and the result is the generic deferral with `turn=None`. The optional
    `on_trip` hook fires on a trip to emit the in-app operator badge (US-080 wires
    it to the US-067 escalation latch: a plain DB write that the operator's own
    Supabase Realtime subscription surfaces — zero outbound). If the breaker does
    NOT trip, `run_turn` runs the normal pipeline and its `DeflectionResult` is
    returned verbatim.

    Dependency-injected (`run_turn` / `on_trip` are async thunks) for the same
    reason `support_bot.run_bot_deflection_turn` injects its token minter: it keeps
    this control-flow seam pure and unit-testable with no DB / LLM / JWT, and lets
    the message-turn runtime (US-078–080) supply the real pipeline + escalation
    write. `on_trip` exceptions are NOT swallowed here — escalation is significant
    and the runtime owns its error handling; the customer-facing deferral is still
    carried on the returned result regardless.
    """
    decision = await check_workspace_breaker(
        limiter, workspace_id, limit=limit, window_seconds=window_seconds, cost=cost
    )
    if decision.tripped:
        # Emit the operator badge (escalate) BEFORE returning, so the handoff is
        # recorded as part of the same turn. No retrieval / LLM has run or will.
        if on_trip is not None:
            await on_trip()
        return GuardedTurnResult(
            tripped=True,
            customer_message=GENERIC_BREAKER_DEFERRAL,
            breaker=decision,
            turn=None,
        )
    turn = await run_turn()
    return GuardedTurnResult(
        tripped=False,
        customer_message=turn.customer_message,
        breaker=decision,
        turn=turn,
    )
