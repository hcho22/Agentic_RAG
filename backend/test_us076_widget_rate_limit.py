"""US-076: per-key + per-session/IP sliding-window rate limit on the widget surface.

Two layers, the same shape as the other support-surface tests
(`test_us073_widget_key_origin.py`, `test_us074_widget_cors.py`):

  * a UNIT layer (always runs, no DB / no app import): the TWO-WINDOW decision
    logic the endpoint relies on, exercised against a fake `RateLimiter` that
    conforms to the US-075 ABC. This is the core contract — a request is refused
    when EITHER the per-key OR the per-session/IP window is over its limit, and a
    blocked hit still counts (so a hammering caller stays saturated). It also pins
    that the fake honours the `RateLimitDecision` shape so the integration layer is
    testing the same protocol production uses.

  * an INTEGRATION / SECURITY layer (skips cleanly when the FastAPI app cannot be
    imported), encoding the PRD US-076 "Validation Test" end-to-end through the
    real `POST /widget/keys/resolve` endpoint via a FastAPI TestClient with a fake
    limiter installed on `main._RATE_LIMITER` and the US-072 resolve gate mocked.
    The endpoint charges the two windows with a SHORT-CIRCUIT between them
    (per-session FIRST, before the DB resolve; per-key only AFTER the resolve
    proves the key exists — so a non-existent key never mints a per-key counter
    row, and a session-throttle does no resolve at all):

      Setup: key K with a low per-session limit (3) AND a low per-key limit (3);
      one session S (one X-Forwarded-For IP), a fresh session S2 (another IP).
      1. Send 3 messages from S up to the per-session limit -> all 200 (key at 3).
      2. Send one more from S                               -> 429 (per-session;
         charged FIRST, so it short-circuits BEFORE the resolve and BEFORE any
         per-key write — the block is the SESSION window on its own and the
         per-key counter is untouched).
      3. Send from a FRESH session S2 under the same key   -> 429 (per-key; S2's
         own session window is fresh (1 <= 3), and only now does its per-key hit
         push the key to 4 > 3 — the block is the KEY window enforcing even from
         a new session).
      Plus: the per-SESSION throttle never calls the DB resolve (short-circuited);
      the per-KEY throttle DOES call the cheap resolve existence-check but reaches
      NO retrieval/LLM / `{active: true}`; a throttle is a 429-with-Retry-After,
      DISTINCT from a 200 deferral; and a limiter whose `hit()` raises FAILS OPEN
      (the endpoint returns 200, not 500 — the counter store is not a request-path
      SPOF). The helper layer covers `_widget_client_ip` (prefers the
      X-Forwarded-For hop, falls back to the socket peer) and the
      no-limiter-configured clean no-op (support-unconfigured surface, 503s
      elsewhere).

    Failure indicator (the bug a test MUST catch): no retrieval/LLM/DB call is
    short-circuited on breach (the throttle still does costly work), only ONE of
    the two windows enforces (a fresh session bypasses an exhausted per-key
    window, or an exhausted session is not refused while the key has headroom), or
    a transient limiter-backend error 500s the public widget surface instead of
    failing open.

Run:
    python -m backend.test_us076_widget_rate_limit

The unit layer needs nothing. The integration layer needs only an importable
backend (it installs a fake in-memory limiter and mocks the DB resolve; no
Supabase round-trip, no OpenAI).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from rate_limiting import RateLimitDecision, RateLimiter  # noqa: E402

LISTED = "https://client.example"


class _FakeLimiter(RateLimiter):
    """A deterministic in-memory counter conforming to the US-075 `RateLimiter` ABC.

    For TEST USE ONLY — production deliberately has no in-memory backend (it would
    under-count per replica and reset on restart; see rate_limiting.py). Within a
    single test process and one window this fixed counter is a faithful stand-in
    for the seam: it counts per opaque key, every hit increments (a blocked hit
    still counts), and `allowed` is `count <= limit`, exactly the contract the
    endpoint draws down against.
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
    """A limiter whose every `hit()` raises — to prove the endpoint FAILS OPEN.

    The Postgres backend raises on any non-200 counter RPC, and those counters
    share the same Postgres as the resolve/retrieval path; an isolated glitch must
    NOT 500 the public widget surface (the limiter is not a request-path SPOF), so
    the enforcement helpers swallow backend errors and ALLOW the request.
    """

    name = "raising"

    async def hit(
        self, key: str, *, limit: int, window_seconds: int, cost: int = 1
    ) -> RateLimitDecision:
        raise RuntimeError("counter backend down")

    async def count(self, key: str, *, window_seconds: int) -> int:
        raise RuntimeError("counter backend down")


# --------------------------------------------------------------------------- #
# Unit layer — the two-window decision logic, always runs. No app import.
# --------------------------------------------------------------------------- #
def _run_unit() -> int:
    checks = 0

    async def _scenario() -> None:
        nonlocal checks
        limiter = _FakeLimiter()
        per_key, per_session, window = 4, 3, 60

        async def enforce(public_key: str, session: str) -> bool:
            """Mirror the endpoint's two-window decision (the split helpers
            `_enforce_widget_session_limit` + `_enforce_widget_key_limit`): hit
            BOTH windows, refuse if EITHER is over. Returns True if allowed."""
            key_d = await limiter.hit(
                f"key:{public_key}", limit=per_key, window_seconds=window
            )
            sess_d = await limiter.hit(
                f"ip:{session}", limit=per_session, window_seconds=window
            )
            return key_d.allowed and sess_d.allowed

        # Session S sends up to the per-session limit -> allowed.
        for _ in range(per_session):
            assert await enforce("K", "S") is True
        checks += 1
        print(f"  unit: {per_session} requests from S within the per-session limit -> allowed")

        # One more from S -> refused by the per-session window (the per-key window
        # still has headroom: key count is 3, under its limit of 4).
        assert await enforce("K", "S") is False, "the per-session window must refuse the over-limit hit"
        assert limiter.counts["key:K"] == 4 and limiter.counts["key:K"] <= per_key, (
            "the per-key window still has headroom -> the block is the SESSION window alone"
        )
        checks += 1
        print("  unit: 1 more from S -> refused by the per-session window (per-key still has headroom)")

        # A FRESH session S2 under the same key: its own session window is empty,
        # but the per-key window is now exhausted (5 > 4), so S2 is refused too.
        assert await enforce("K", "S2") is False, "the per-key window must refuse even a fresh session"
        assert limiter.counts["ip:S2"] == 1 and limiter.counts["ip:S2"] <= per_session, (
            "S2's session window is fresh -> the block is the KEY window alone"
        )
        checks += 1
        print("  unit: a fresh session S2 -> refused by the per-key window (its own session window is fresh)")

        # A blocked hit STILL counts: hammering S keeps climbing while blocked.
        before = limiter.counts["ip:S"]
        await enforce("K", "S")
        assert limiter.counts["ip:S"] == before + 1, "a blocked hit must still increment the window"
        checks += 1
        print("  unit: a blocked hit still increments the window (hammering caller stays saturated)")

        # A different key under the SAME exhausted session is independent on the
        # key axis (so the two windows are genuinely separate buckets).
        d = await limiter.hit("key:OTHER", limit=per_key, window_seconds=window)
        assert d.allowed is True and d.count == 1, "a different key starts its own per-key window"
        checks += 1
        print("  unit: a different key has its own per-key window (independent buckets)")

    asyncio.run(_scenario())

    # The fake honours the production decision shape (so the integration layer
    # tests the same protocol). RateLimitDecision is the US-075 contract object.
    d = RateLimitDecision(allowed=False, count=7, limit=5, window_seconds=60)
    assert d.allowed is False and d.count == 7 and d.limit == 5 and d.window_seconds == 60
    assert isinstance(_FakeLimiter(), RateLimiter), "the fake must conform to the RateLimiter ABC"
    checks += 1
    print("  unit: RateLimitDecision shape + fake conforms to the RateLimiter ABC")

    return checks


# --------------------------------------------------------------------------- #
# Integration / security layer — drives the real endpoint via TestClient with a
# fake limiter installed and the US-072 resolve gate mocked. Skips cleanly if the
# backend cannot be imported.
# --------------------------------------------------------------------------- #
def _run_integration() -> int:
    os.environ.setdefault("SUPABASE_URL", "http://127.0.0.1:54321")
    os.environ.setdefault("SUPABASE_ANON_KEY", "anon-test-key")
    os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

    try:
        import main  # noqa: E402
        from fastapi.testclient import TestClient  # noqa: E402
    except Exception as e:  # pragma: no cover - environment-dependent
        print(f"SKIP integration: cannot import backend app ({e})")
        return 0

    from widget_keys import generate_public_key  # noqa: E402

    pk = generate_public_key()

    # Mock the US-072 not-revoked resolve gate so the endpoint never hits the DB:
    # the key resolves to a workspace with our listed origin. We also COUNT the
    # calls so we can prove a throttle short-circuits before this runs.
    resolve_calls = {"n": 0}

    async def _fake_resolve(http: object, public_key: str) -> dict | None:
        resolve_calls["n"] += 1
        return {"id": "k", "workspace_id": "w", "allowed_origins": [LISTED]}

    fake = _FakeLimiter()

    orig_resolve = main._resolve_widget_key
    orig_limiter = main._RATE_LIMITER
    orig_per_key = main.WIDGET_RATE_LIMIT_PER_KEY
    orig_per_session = main.WIDGET_RATE_LIMIT_PER_SESSION
    orig_window = main.WIDGET_RATE_LIMIT_WINDOW_SECONDS

    # Low test limits so a few requests exercise the breach path (per the PRD
    # "Key K with a low test window limit"). per_key == per_session == 3: because
    # the per-session window is charged FIRST and short-circuits, a session-throttle
    # does NOT pre-increment the per-key counter — so after 3 ok requests from S the
    # key sits at exactly 3, the 4th from S 429s on session (key untouched), and a
    # fresh session S2's first request is the one that pushes the key to 4 > 3 and
    # trips the per-key window while S2's own session window is still fresh.
    main._resolve_widget_key = _fake_resolve  # type: ignore[assignment]
    main._RATE_LIMITER = fake  # type: ignore[assignment]
    main.WIDGET_RATE_LIMIT_PER_KEY = 3
    main.WIDGET_RATE_LIMIT_PER_SESSION = 3
    main.WIDGET_RATE_LIMIT_WINDOW_SECONDS = 60

    total = 0
    try:
        client = TestClient(main.app)

        def resolve(ip: str) -> int:
            # One session == one X-Forwarded-For IP. The listed Origin keeps the
            # US-073 origin gate happy so any non-throttled request reaches 200.
            r = client.post(
                "/widget/keys/resolve",
                json={"public_key": pk},
                headers={"Origin": LISTED, "X-Forwarded-For": ip},
            )
            return r.status_code

        # Step 1: 3 messages from session S (IP 1.1.1.1), up to the per-session
        # limit -> all 200 active. Each resolves once and charges the key once, so
        # the per-key counter ends at exactly 3.
        for i in range(3):
            code = resolve("1.1.1.1")
            assert code == 200, f"request {i + 1} from S must succeed, got {code}"
        assert resolve_calls["n"] == 3, "each allowed request resolves the key once"
        assert fake.counts["key:" + pk] == 3, "3 allowed requests charge the per-key window to 3"
        total += 1
        print("  step 1: 3 requests from S within the per-session limit -> 200 (key at 3)")

        # Step 2: one more from S -> 429 throttled by the PER-SESSION window, which
        # is charged FIRST and short-circuits: the DB resolve is NOT called and the
        # per-key counter is NOT touched (still 3). So the block is unambiguously
        # the session window, and a session-throttle mints no per-key counter row.
        resolve_before = resolve_calls["n"]
        key_count_before = fake.counts["key:" + pk]
        code = resolve("1.1.1.1")
        assert code == 429, f"the 4th request from S must be throttled (per-session), got {code}"
        assert resolve_calls["n"] == resolve_before, (
            "a per-session throttle must NOT resolve the key (short-circuit; no DB / retrieval / LLM)"
        )
        assert fake.counts["key:" + pk] == key_count_before, (
            "a per-session throttle must NOT charge the per-key window (no counter amplification)"
        )
        total += 1
        print("  step 2: 1 more from S -> 429 (per-session); no DB resolve, per-key counter untouched")

        # Step 3: a FRESH session S2 (IP 2.2.2.2) under the SAME key -> 429
        # throttled by the PER-KEY window even from a new session. The per-key
        # window is charged only AFTER the resolve proves the key real, so this
        # request DOES resolve once (the cheap existence check) — then its per-key
        # hit pushes the key to 4 > 3 and trips. S2's own session window is fresh
        # (count 1 <= 3), so the ONLY reason it is refused is the per-key window,
        # and the 429 means it never reached retrieval/LLM / the `{active}` reply.
        resolve_before = resolve_calls["n"]
        r2 = client.post(
            "/widget/keys/resolve",
            json={"public_key": pk},
            headers={"Origin": LISTED, "X-Forwarded-For": "2.2.2.2"},
        )
        assert r2.status_code == 429, (
            f"a fresh session S2 must be throttled by the per-key window, got {r2.status_code} "
            "(failure indicator: only the per-session window enforces)"
        )
        assert r2.json().get("detail", "") != "" and "active" not in r2.json(), (
            "the per-key throttle reaches no retrieval/LLM and returns no {active: true}"
        )
        assert resolve_calls["n"] == resolve_before + 1, (
            "the per-key path DOES run the cheap resolve existence-check (exactly once) before charging"
        )
        assert fake.counts["ip:2.2.2.2"] <= main.WIDGET_RATE_LIMIT_PER_SESSION, (
            "S2's session window is NOT exhausted -> the block is the per-key window alone"
        )
        total += 1
        print("  step 3: fresh session S2 -> 429 (per-key window enforces even from a new session)")

        # The throttle is a 429 refusal (retry the same request), DISTINCT from the
        # US-077 circuit breaker's 200 generic deferral. Confirm the body + the
        # Retry-After hint, and that it is not a 200.
        r = client.post(
            "/widget/keys/resolve",
            json={"public_key": pk},
            headers={"Origin": LISTED, "X-Forwarded-For": "1.1.1.1"},
        )
        assert r.status_code == 429, "a throttle is a 429, never a 200 (distinct from US-077 deferral)"
        assert "Retry-After" in r.headers, "a throttle carries a Retry-After hint"
        assert "rate limit" in r.json().get("detail", "").lower()
        total += 1
        print("  extra: throttle is a 429 with Retry-After (distinct from the US-077 200 deferral)")

        # Fail-open: a limiter whose every hit() raises (a transient counter-backend
        # blip) must NOT 500 the public widget surface. The enforcement helpers
        # swallow the backend error and ALLOW the request, so a fresh session under
        # the raising limiter resolves to 200 instead of bubbling a 500.
        main._RATE_LIMITER = _RaisingLimiter()  # type: ignore[assignment]
        r = client.post(
            "/widget/keys/resolve",
            json={"public_key": pk},
            headers={"Origin": LISTED, "X-Forwarded-For": "3.3.3.3"},
        )
        assert r.status_code == 200, (
            f"a raising limiter must FAIL OPEN (200), not 500 the widget surface, got {r.status_code}"
        )
        assert r.json().get("active") is True, "the failed-open request still resolves active"
        total += 1
        print("  extra: a raising limiter FAILS OPEN -> 200, never a 500 (counter store is not a SPOF)")

        print(
            f"OK: US-076 integration passed — {total} endpoint assertions; both the "
            "per-key AND per-session/IP windows enforce (a fresh session is still "
            "refused by an exhausted per-key window; an exhausted session is refused "
            "while the key has headroom), and a throttle short-circuits before the "
            "DB resolve / retrieval / LLM"
        )
        return total
    finally:
        main._resolve_widget_key = orig_resolve  # type: ignore[assignment]
        main._RATE_LIMITER = orig_limiter  # type: ignore[assignment]
        main.WIDGET_RATE_LIMIT_PER_KEY = orig_per_key
        main.WIDGET_RATE_LIMIT_PER_SESSION = orig_per_session
        main.WIDGET_RATE_LIMIT_WINDOW_SECONDS = orig_window


# --------------------------------------------------------------------------- #
# Helper layer — `_widget_client_ip` session keying + the no-op-when-unconfigured
# path. Skips cleanly if the backend cannot be imported.
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

    class _FakeReq:
        def __init__(self, headers: dict[str, str], client_host: str | None) -> None:
            # Starlette lower-cases header lookups; mirror that with a small shim.
            self.headers = {k.lower(): v for k, v in headers.items()}
            self.client = type("C", (), {"host": client_host})() if client_host else None

    total = 0

    # Prefers the LEFT-most X-Forwarded-For hop (the original client behind a proxy).
    req = _FakeReq({"X-Forwarded-For": "9.9.9.9, 10.0.0.1"}, client_host="172.16.0.1")
    assert main._widget_client_ip(req) == "9.9.9.9", "must prefer the left-most XFF hop"
    total += 1
    print("  helper: _widget_client_ip prefers the left-most X-Forwarded-For hop")

    # Falls back to the socket peer when there is no XFF, then to a constant.
    assert main._widget_client_ip(_FakeReq({}, client_host="172.16.0.1")) == "172.16.0.1"
    assert main._widget_client_ip(_FakeReq({"X-Forwarded-For": "   "}, client_host="172.16.0.1")) == "172.16.0.1"
    assert main._widget_client_ip(_FakeReq({}, client_host=None)) == "unknown"
    total += 1
    print("  helper: _widget_client_ip falls back to the socket peer, then 'unknown'")

    # No-op when the limiter is unconfigured (support not enabled): BOTH split
    # helpers return cleanly without raising and without needing a real limiter —
    # the widget endpoints 503 elsewhere in that case, so there is nothing to
    # limit.
    orig = main._RATE_LIMITER
    main._RATE_LIMITER = None  # type: ignore[assignment]
    try:
        asyncio.run(
            main._enforce_widget_session_limit(_FakeReq({}, client_host="1.2.3.4"))
        )
        asyncio.run(main._enforce_widget_key_limit("wk_pk_whatever"))
    finally:
        main._RATE_LIMITER = orig  # type: ignore[assignment]
    total += 1
    print(
        "  helper: _enforce_widget_session_limit / _enforce_widget_key_limit are "
        "clean no-ops when the limiter is unconfigured"
    )

    # Fail-open: with a limiter whose hit() raises, BOTH split helpers swallow the
    # backend error and return (no raise) rather than letting a transient counter
    # glitch surface as a 500 in the request path.
    main._RATE_LIMITER = _RaisingLimiter()  # type: ignore[assignment]
    try:
        asyncio.run(
            main._enforce_widget_session_limit(_FakeReq({}, client_host="1.2.3.4"))
        )
        asyncio.run(main._enforce_widget_key_limit("wk_pk_whatever"))
    finally:
        main._RATE_LIMITER = orig  # type: ignore[assignment]
    total += 1
    print(
        "  helper: both helpers FAIL OPEN (no raise) when the limiter backend errors"
    )

    print(f"OK: US-076 helpers passed — {total} assertions")
    return total


def main_entry() -> None:
    # Each layer owns its own event loop (the unit + helper layers drive async
    # limiter calls via asyncio.run), so there is no top-level async wrapper.
    unit = _run_unit()
    print(f"  ({unit} unit checks passed)")
    _run_integration()
    _run_helpers()


if __name__ == "__main__":
    main_entry()
