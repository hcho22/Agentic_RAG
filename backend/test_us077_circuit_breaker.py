"""US-077: per-workspace circuit breaker → zero-cost deferral + operator badge.

Two layers, the same shape as the other support-surface tests
(`test_us076_widget_rate_limit.py`, `test_us074_widget_cors.py`):

  * a UNIT layer (always runs, no DB / no app import): the breaker decision logic
    + the guarded-turn control flow, exercised against a fake `RateLimiter` that
    conforms to the US-075 ABC. This is the heart of the story — when the
    per-workspace ceiling is breached the breaker TRIPS, and `run_breaker_guarded_turn`
    then NEVER awaits the (paid) deflection turn (so retrieval + LLM call counters
    do NOT increment — the PRD validation test's "Expected Result"), returns the
    generic deferral with `turn=None` (no partial/garbage answer), and fires the
    `on_trip` escalation hook (the in-app operator badge — a human handoff, so the
    conversation is never counted as a deflection). It also pins the two safety
    stances the breaker shares with US-076: NO-OP when the limiter is unconfigured
    and FAIL-OPEN on a limiter-backend error (a breaker failing CLOSED would defer a
    workspace's entire traffic).

  * a HELPER / INTEGRATION layer (skips cleanly when the FastAPI app cannot be
    imported): drives `main._check_workspace_breaker` against a fake limiter
    installed on `main._RATE_LIMITER`, proving it charges a distinct `ws:<id>`
    bucket, trips once the (low test) per-workspace ceiling is exceeded, is a clean
    no-op when unconfigured, and fails open on a backend error.

Failure indicator (the bug a test MUST catch): a tripped breaker still calls
retrieval/LLM (the short-circuit is gone), fabricates a partial answer instead of
the generic deferral, fails to escalate (no operator badge / would be miscounted
as a deflection), emits an OUTBOUND notification, or the breaker fails CLOSED on a
backend error (defers all traffic).

Run:
    python -m backend.test_us077_circuit_breaker

The unit layer needs nothing. The helper layer needs only an importable backend
(it installs a fake in-memory limiter; no Supabase round-trip, no OpenAI).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from circuit_breaker import (  # noqa: E402
    GENERIC_BREAKER_DEFERRAL,
    BreakerDecision,
    GuardedTurnResult,
    check_workspace_breaker,
    run_breaker_guarded_turn,
    workspace_breaker_key,
)
from escalation import (  # noqa: E402
    DeflectionResult,
    FaithfulnessDecision,
    RetrievalGateDecision,
)
from rate_limiting import RateLimitDecision, RateLimiter  # noqa: E402

WS = "11111111-1111-1111-1111-111111111111"


class _FakeLimiter(RateLimiter):
    """A deterministic in-memory counter conforming to the US-075 `RateLimiter` ABC.

    For TEST USE ONLY — production deliberately has no in-memory backend (it would
    under-count per replica and reset on restart; see rate_limiting.py). Within one
    test process and one window this fixed counter is a faithful stand-in for the
    seam: it counts per opaque key, every hit increments (a blocked hit still
    counts), and `allowed` is `count <= limit`.
    """

    name = "fake"

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.hits: list[str] = []

    async def hit(
        self, key: str, *, limit: int, window_seconds: int, cost: int = 1
    ) -> RateLimitDecision:
        self.counts[key] = self.counts.get(key, 0) + cost
        self.hits.append(key)
        current = self.counts[key]
        return RateLimitDecision(
            allowed=current <= limit,
            count=current,
            limit=limit,
            window_seconds=window_seconds,
        )

    async def count(self, key: str, *, window_seconds: int) -> int:
        return self.counts.get(key, 0)


class _RaisingLimiter(RateLimiter):
    """A limiter whose every `hit()` raises — to prove the breaker FAILS OPEN.

    The Postgres backend raises on any non-200 counter RPC, and those counters
    share the same Postgres as the resolve/retrieval path. A breaker failing CLOSED
    on such a glitch would defer the workspace's ENTIRE traffic (a self-inflicted
    denial of service), so the breaker swallows backend errors and reports
    NOT-tripped.
    """

    name = "raising"

    async def hit(
        self, key: str, *, limit: int, window_seconds: int, cost: int = 1
    ) -> RateLimitDecision:
        raise RuntimeError("counter backend down")

    async def count(self, key: str, *, window_seconds: int) -> int:
        raise RuntimeError("counter backend down")


def _answered_turn() -> DeflectionResult:
    """A stand-in 'answered' DeflectionResult for the non-tripped path."""
    return DeflectionResult(
        action="answered",
        customer_message="Here is the grounded answer.",
        retrieval=RetrievalGateDecision(
            strong=True, top1_cosine=0.92, n_cleared=4, reason="strong"
        ),
        faithfulness=FaithfulnessDecision(
            faithful=True, supported=True, score=0.95, reason="faithful"
        ),
        reason="answered",
    )


# --------------------------------------------------------------------------- #
# Unit layer — breaker decision + guarded-turn control flow. Always runs.
# --------------------------------------------------------------------------- #
def _run_unit() -> int:
    checks = 0

    async def _scenario() -> None:
        nonlocal checks
        limit, window = 3, 60

        # --- check_workspace_breaker: trips once the ceiling is exceeded ------
        limiter = _FakeLimiter()
        for i in range(limit):
            d = await check_workspace_breaker(
                limiter, WS, limit=limit, window_seconds=window
            )
            assert d.tripped is False, f"hit {i + 1} within the ceiling must NOT trip"
        # The next hit pushes count to limit+1 -> tripped.
        d = await check_workspace_breaker(limiter, WS, limit=limit, window_seconds=window)
        assert d.tripped is True, "exceeding the per-workspace ceiling must trip the breaker"
        assert d.count == limit + 1 and d.limit == limit
        checks += 1
        print(f"  unit: breaker trips once the per-workspace ceiling ({limit}/win) is exceeded")

        # The bucket is the distinct `ws:` namespace, never key:/ip: (so it never
        # conflates with the US-076 windows even at the same window size).
        assert limiter.hits and all(h == f"ws:{WS}" for h in limiter.hits)
        assert workspace_breaker_key(WS) == f"ws:{WS}"
        checks += 1
        print("  unit: the breaker charges a distinct `ws:<id>` bucket (not key:/ip:)")

        # A blocked hit STILL counts (a hammering workspace stays tripped).
        before = limiter.counts[f"ws:{WS}"]
        await check_workspace_breaker(limiter, WS, limit=limit, window_seconds=window)
        assert limiter.counts[f"ws:{WS}"] == before + 1, "a blocked hit must still increment"
        checks += 1
        print("  unit: a blocked hit still counts (hammering workspace stays tripped)")

        # NO-OP when unconfigured: limiter None -> never trips, count 0, no raise.
        d = await check_workspace_breaker(None, WS, limit=limit, window_seconds=window)
        assert d.tripped is False and d.count == 0
        checks += 1
        print("  unit: limiter unconfigured (None) -> never trips (clean no-op)")

        # FAIL-OPEN on a backend error -> never trips (availability wins; a breaker
        # failing closed would defer the whole workspace).
        d = await check_workspace_breaker(
            _RaisingLimiter(), WS, limit=limit, window_seconds=window
        )
        assert d.tripped is False, "a limiter-backend error must FAIL OPEN (never trip)"
        checks += 1
        print("  unit: a limiter-backend error FAILS OPEN -> never trips")

        # --- run_breaker_guarded_turn: NOT tripped -> the turn runs normally ---
        fresh = _FakeLimiter()
        turn_calls = {"n": 0}
        trip_calls = {"n": 0}

        async def run_turn() -> DeflectionResult:
            turn_calls["n"] += 1
            return _answered_turn()

        async def on_trip() -> None:
            trip_calls["n"] += 1

        res = await run_breaker_guarded_turn(
            limiter=fresh,
            workspace_id=WS,
            limit=limit,
            window_seconds=window,
            run_turn=run_turn,
            on_trip=on_trip,
        )
        assert isinstance(res, GuardedTurnResult)
        assert res.tripped is False
        assert turn_calls["n"] == 1, "an un-tripped breaker must run the deflection turn"
        assert trip_calls["n"] == 0, "an un-tripped breaker must NOT fire the escalation hook"
        assert res.turn is not None and res.turn.action == "answered"
        assert res.customer_message == res.turn.customer_message
        checks += 1
        print("  unit: breaker NOT tripped -> deflection turn runs, no escalation, answer returned")

        # --- run_breaker_guarded_turn: tripped -> ZERO-COST short-circuit ------
        # Drive the same workspace past the ceiling so the next guarded turn trips.
        for _ in range(limit):
            await check_workspace_breaker(fresh, WS, limit=limit, window_seconds=window)
        turn_calls["n"] = 0
        trip_calls["n"] = 0
        res = await run_breaker_guarded_turn(
            limiter=fresh,
            workspace_id=WS,
            limit=limit,
            window_seconds=window,
            run_turn=run_turn,
            on_trip=on_trip,
        )
        assert res.tripped is True
        # THE central assertion: a tripped breaker NEVER runs the turn, so the
        # retrieval-call and LLM-call counters do NOT increment for that turn.
        assert turn_calls["n"] == 0, (
            "FAILURE INDICATOR: a tripped breaker still ran the deflection turn "
            "(retrieval/LLM were called — the zero-cost short-circuit is broken)"
        )
        assert trip_calls["n"] == 1, "a tripped breaker must fire the escalation hook exactly once"
        assert res.turn is None, "a tripped breaker must NOT fabricate a partial/garbage answer"
        assert res.customer_message == GENERIC_BREAKER_DEFERRAL
        checks += 1
        print("  unit: breaker TRIPPED -> turn NEVER runs (0 retrieval/LLM), escalates, generic deferral")

        # on_trip is OPTIONAL: a trip with no hook still defers cleanly (no raise).
        res = await run_breaker_guarded_turn(
            limiter=fresh,
            workspace_id=WS,
            limit=limit,
            window_seconds=window,
            run_turn=run_turn,
        )
        assert res.tripped is True and res.turn is None and turn_calls["n"] == 0
        checks += 1
        print("  unit: a trip with no on_trip hook still defers cleanly (hook is optional)")

    asyncio.run(_scenario())

    # The customer-facing deferral leaks NOTHING operational: no workspace id, no
    # counts, no scope, no "rate limited". It only routes to a human.
    assert WS not in GENERIC_BREAKER_DEFERRAL
    for leak in ("rate limit", "rate-limit", "workspace", "ceiling", "breaker", "429"):
        assert leak not in GENERIC_BREAKER_DEFERRAL.lower(), f"deferral must not leak {leak!r}"
    assert "human" in GENERIC_BREAKER_DEFERRAL.lower(), "the deferral must promise a human follow-up"
    print("  unit: the generic deferral promises a human and leaks no operational detail")

    # Frozen contract objects (like the gate decisions + RateLimitDecision).
    d = BreakerDecision(tripped=True, count=7, limit=5, window_seconds=60)
    for frozen, field in ((d, "tripped"), (_answered_turn(), "action")):
        try:
            setattr(frozen, field, "x")
            raise AssertionError(f"{type(frozen).__name__} must be frozen")
        except (TypeError, ValueError):
            pass
    assert isinstance(_FakeLimiter(), RateLimiter), "the fake must conform to the RateLimiter ABC"
    checks += 1
    print("  unit: BreakerDecision is frozen + the fake conforms to the RateLimiter ABC")

    return checks


# --------------------------------------------------------------------------- #
# Helper / integration layer — `main._check_workspace_breaker` against a fake
# limiter on `main._RATE_LIMITER`. Skips cleanly if the backend cannot be imported.
# --------------------------------------------------------------------------- #
def _run_helpers() -> int:
    os.environ.setdefault("SUPABASE_URL", "http://127.0.0.1:54321")
    os.environ.setdefault("SUPABASE_ANON_KEY", "anon-test-key")
    os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

    try:
        import main  # noqa: E402
    except Exception as e:  # pragma: no cover - environment-dependent
        print(f"SKIP helpers: cannot import backend app ({e})")
        return 0

    total = 0
    orig_limiter = main._RATE_LIMITER
    orig_ws = main.WIDGET_BREAKER_PER_WORKSPACE

    # Low test ceiling so a few turns exercise the trip path (the PRD's "low test
    # per-workspace ceiling").
    main.WIDGET_BREAKER_PER_WORKSPACE = 2
    fake = _FakeLimiter()
    main._RATE_LIMITER = fake  # type: ignore[assignment]
    try:
        async def _drive() -> None:
            nonlocal total
            # Within the ceiling -> not tripped.
            for _ in range(2):
                d = await main._check_workspace_breaker(WS)
                assert d.tripped is False
            # Exceed it -> tripped.
            d = await main._check_workspace_breaker(WS)
            assert d.tripped is True, "main._check_workspace_breaker must trip past the ceiling"
            # It used the distinct `ws:` bucket.
            assert any(h == f"ws:{WS}" for h in fake.hits), "must charge the `ws:<id>` bucket"
            total += 1
            print("  helper: main._check_workspace_breaker trips past the per-workspace ceiling (ws: bucket)")

        asyncio.run(_drive())

        # No-op when the limiter is unconfigured (support not enabled): never trips.
        main._RATE_LIMITER = None  # type: ignore[assignment]
        d = asyncio.run(main._check_workspace_breaker(WS))
        assert d.tripped is False, "unconfigured limiter -> never trips (no-op)"
        total += 1
        print("  helper: main._check_workspace_breaker is a clean no-op when unconfigured")

        # Fail-open on a backend error: never trips (a breaker must not defer all
        # traffic on a transient counter glitch).
        main._RATE_LIMITER = _RaisingLimiter()  # type: ignore[assignment]
        d = asyncio.run(main._check_workspace_breaker(WS))
        assert d.tripped is False, "a backend error must FAIL OPEN (never trip)"
        total += 1
        print("  helper: main._check_workspace_breaker FAILS OPEN on a backend error")
    finally:
        main._RATE_LIMITER = orig_limiter  # type: ignore[assignment]
        main.WIDGET_BREAKER_PER_WORKSPACE = orig_ws

    print(f"OK: US-077 helpers passed — {total} assertions")
    return total


def main_entry() -> None:
    unit = _run_unit()
    print(f"  ({unit} unit checks passed)")
    _run_helpers()


if __name__ == "__main__":
    main_entry()
