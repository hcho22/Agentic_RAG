"""US-075 tests: the swappable `RateLimiter` seam (default Postgres, optional Redis).

Two layers, the same shape as the other support-surface tests:

  * a UNIT layer (always runs, no DB / no secrets / no Redis server) — the factory
    selection + fail-closed config, the deliberate absence of an in-memory backend,
    the `RateLimitDecision` shape, the ABC contract, the Redis lazy-import error,
    the Postgres backend's PostgREST request/response handling over a MockTransport,
    and the Redis backend's sliding-window math over a fake client; and
  * an INTEGRATION layer (skips cleanly without a local Supabase + the migration)
    encoding the PRD US-075 Validation Test against the REAL seam over PostgREST:
    increment key Z N times, simulate a backend "restart" (a fresh limiter +
    client), read Z back at value N (durable, not in-process), and confirm the
    limit decision flips when the window is exceeded.

Run:
    python -m backend.test_us075_rate_limiter

The integration layer needs a local Supabase (PostgREST at SUPABASE_URL, default
http://127.0.0.1:54321), the well-known local service-role key, and migration
20260624170000 applied; it skips cleanly otherwise.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

import httpx

# Allow `python -m backend.test_us075_rate_limiter` from the repo root.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from rate_limiting import (  # noqa: E402
    DEFAULT_RATE_LIMITER,
    PostgresRateLimiter,
    RateLimitDecision,
    RateLimiter,
    RedisRateLimiter,
    build_rate_limiter,
    get_rate_limiter_name,
)

LOCAL_SERVICE_ROLE_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImV4cCI6MTk4MzgxMjk5Nn0."
    "EGIM96RAZx35lJzdJsyH-qQwv8Hdp7fsn3W0YpN81IU"
)


def _env(key: str, default: str | None = None) -> str | None:
    val = os.environ.get(key)
    return val if val else default


class _EnvGuard:
    """Set/clear env vars for the duration of a `with` block, then restore."""

    def __init__(self, **overrides: str | None) -> None:
        self._overrides = overrides
        self._saved: dict[str, str | None] = {}

    def __enter__(self) -> "_EnvGuard":
        for k, v in self._overrides.items():
            self._saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *exc: object) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _expect_raises(fn, exc_type, label: str) -> None:
    try:
        fn()
    except exc_type:
        return
    except Exception as e:  # wrong-but-raised: surface it
        raise AssertionError(
            f"{label}: expected {exc_type.__name__}, got {type(e).__name__}: {e}"
        ) from e
    raise AssertionError(f"{label}: expected {exc_type.__name__}, nothing raised")


# -----------------------------------------------------------------------------
# UNIT layer (always runs)
# -----------------------------------------------------------------------------


def test_name_selection_and_default() -> None:
    # Default is Postgres (no env set).
    with _EnvGuard(RATE_LIMITER=None):
        assert get_rate_limiter_name() == "postgres"
    assert DEFAULT_RATE_LIMITER == "postgres"
    # Explicit selection, case/space-insensitive.
    with _EnvGuard(RATE_LIMITER="redis"):
        assert get_rate_limiter_name() == "redis"
    with _EnvGuard(RATE_LIMITER="  Postgres  "):
        assert get_rate_limiter_name() == "postgres"
    # Empty string falls back to the default.
    with _EnvGuard(RATE_LIMITER=""):
        assert get_rate_limiter_name() == "postgres"
    print("  name selection: default=postgres, redis selectable, normalized")


def test_no_in_memory_backend() -> None:
    # The whole point of the seam is durable + cross-instance; an in-memory /
    # "none" option would silently disable the cost-DoS guard, so it must be
    # rejected, not quietly accepted.
    for bad in ("memory", "inmemory", "in-process", "none", "local"):
        with _EnvGuard(RATE_LIMITER=bad):
            _expect_raises(get_rate_limiter_name, ValueError, f"RATE_LIMITER={bad}")
    print("  no in-memory backend: memory/none/local all rejected")


def test_postgres_factory_fail_closed() -> None:
    async def run() -> None:
        http = httpx.AsyncClient()
        try:
            # Missing http client.
            _expect_raises(
                lambda: build_rate_limiter(
                    "postgres", supabase_url="http://x", service_role_key="k"
                ),
                ValueError,
                "postgres without http",
            )
            # Missing SUPABASE_URL.
            with _EnvGuard(SUPABASE_URL=None, SUPABASE_SERVICE_ROLE_KEY=None):
                _expect_raises(
                    lambda: build_rate_limiter(
                        "postgres", http=http, service_role_key="k"
                    ),
                    ValueError,
                    "postgres without url",
                )
                # Missing service-role key (the deny-all RLS demands it).
                _expect_raises(
                    lambda: build_rate_limiter(
                        "postgres", http=http, supabase_url="http://x"
                    ),
                    ValueError,
                    "postgres without service-role key",
                )
            # All present -> a PostgresRateLimiter.
            rl = build_rate_limiter(
                "postgres", http=http, supabase_url="http://x", service_role_key="k"
            )
            assert isinstance(rl, PostgresRateLimiter) and rl.name == "postgres"
            # Env-resolved variant (no explicit overrides).
            with _EnvGuard(
                SUPABASE_URL="http://env-host", SUPABASE_SERVICE_ROLE_KEY="env-key"
            ):
                rl2 = build_rate_limiter("postgres", http=http)
                assert isinstance(rl2, PostgresRateLimiter)
            print(
                "  postgres factory: fail-closed on http/url/key, builds with all present"
            )
        finally:
            await http.aclose()

    asyncio.run(run())


def test_redis_factory_requires_url_and_package() -> None:
    with _EnvGuard(REDIS_URL=None):
        _expect_raises(
            lambda: build_rate_limiter("redis"),
            ValueError,
            "redis without REDIS_URL",
        )
    # With a URL but (presumably) no redis package installed, the lazy import must
    # raise a clear, actionable RuntimeError rather than an opaque ImportError. If
    # redis IS installed in this environment, from_url succeeds and we get a real
    # RedisRateLimiter instead — both outcomes are acceptable; what must NOT happen
    # is a bare ImportError leaking out.
    try:
        import redis.asyncio  # type: ignore[import-untyped, import-not-found]  # noqa: F401

        have_redis = True
    except ImportError:
        have_redis = False

    if have_redis:
        rl = build_rate_limiter("redis", redis_url="redis://127.0.0.1:6379/0")
        assert isinstance(rl, RedisRateLimiter) and rl.name == "redis"
        print("  redis factory: package present -> RedisRateLimiter built")
    else:
        _expect_raises(
            lambda: build_rate_limiter("redis", redis_url="redis://127.0.0.1:6379/0"),
            RuntimeError,
            "redis without package",
        )
        print("  redis factory: missing package -> clear RuntimeError (not ImportError)")


def test_decision_shape_and_abc() -> None:
    d = RateLimitDecision(allowed=False, count=7, limit=5, window_seconds=60)
    assert d.allowed is False and d.count == 7 and d.limit == 5
    assert d.window_seconds == 60
    # The ABC cannot be instantiated (abstract methods unimplemented).
    _expect_raises(lambda: RateLimiter(), TypeError, "instantiate ABC")  # type: ignore[abstract]
    # Both concrete backends expose the SAME protocol — this is what makes the
    # factory swap call-site-free: every caller talks only to these methods.
    for cls in (PostgresRateLimiter, RedisRateLimiter):
        assert issubclass(cls, RateLimiter)
        for m in ("hit", "count", "aclose"):
            assert callable(getattr(cls, m)), f"{cls.__name__}.{m}"
    print("  decision shape ok; ABC abstract; both backends share the protocol")


def test_postgres_backend_over_mock_transport() -> None:
    """Drive PostgresRateLimiter against a MockTransport that emulates the two RPCs,
    verifying the request bodies and the PostgREST response parsing (no DB)."""

    counters: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path.endswith("/rpc/rate_limit_hit"):
            key = body["p_key"]
            assert isinstance(body["p_limit"], int)
            assert isinstance(body["p_window_seconds"], int)
            counters[key] = counters.get(key, 0) + int(body["p_cost"])
            cur = counters[key]
            # `returns table(...)` -> one-row array, the real PostgREST shape.
            return httpx.Response(
                200,
                json=[
                    {
                        "allowed": cur <= body["p_limit"],
                        "current_count": cur,
                        "limit_value": body["p_limit"],
                        "window_seconds": body["p_window_seconds"],
                    }
                ],
            )
        if request.url.path.endswith("/rpc/rate_limit_count"):
            # `returns bigint` -> a bare scalar.
            return httpx.Response(200, json=counters.get(body["p_key"], 0))
        return httpx.Response(404, json={"message": "no such rpc"})

    async def run() -> None:
        transport = httpx.MockTransport(handler)
        http = httpx.AsyncClient(transport=transport)
        try:
            rl = build_rate_limiter(
                "postgres",
                http=http,
                supabase_url="http://mock",
                service_role_key="svc",
            )
            # 3 hits under a limit of 5 -> all allowed, count climbs 1,2,3.
            for expected in (1, 2, 3):
                d = await rl.hit("ip:1.2.3.4", limit=5, window_seconds=60)
                assert d.count == expected and d.allowed is True and d.limit == 5
            # Peek does not increment.
            assert await rl.count("ip:1.2.3.4", window_seconds=60) == 3
            # Push over the limit -> allowed flips False, count keeps rising.
            d4 = await rl.hit("ip:1.2.3.4", limit=5, window_seconds=60, cost=3)
            assert d4.count == 6 and d4.allowed is False
        finally:
            await http.aclose()

    asyncio.run(run())
    print("  postgres backend: RPC bodies + response parsing verified over mock")


def test_redis_backend_over_fake_client() -> None:
    """Validate the Redis backend's sliding-window math + decision over a fake
    client, so the algorithm is exercised with no Redis server."""

    class _FakeRedis:
        def __init__(self) -> None:
            self.store: dict[str, int] = {}
            self.closed = False

        async def eval(self, script: str, numkeys: int, *args: object):
            cur_key, prev_key = str(args[0]), str(args[1])
            cost = int(str(args[2]))
            self.store[cur_key] = self.store.get(cur_key, 0) + cost
            return [self.store[cur_key], self.store.get(prev_key, 0)]

        async def get(self, k: str):
            v = self.store.get(k)
            return None if v is None else str(v)

        async def aclose(self) -> None:
            self.closed = True

    async def run() -> None:
        fake = _FakeRedis()
        rl = RedisRateLimiter(fake)
        # Within a single (large) window, prev bucket is empty -> estimate == hits.
        for expected in (1, 2, 3, 4):
            d = await rl.hit("key:abc", limit=3, window_seconds=3600)
            assert d.count == expected
            assert d.allowed is (expected <= 3)
        assert await rl.count("key:abc", window_seconds=3600) == 4
        await rl.aclose()
        assert fake.closed is True, "aclose must close an owned client"

    asyncio.run(run())
    print("  redis backend: sliding-window math + decision + aclose verified over fake")


# -----------------------------------------------------------------------------
# INTEGRATION layer (skips cleanly without a local Supabase + migration)
# -----------------------------------------------------------------------------


async def _rpc_available(http: httpx.AsyncClient, base_url: str, headers: dict) -> bool:
    """True iff the rate_limit_count RPC answers (migration applied + reachable)."""
    try:
        r = await http.post(
            f"{base_url}/rest/v1/rpc/rate_limit_count",
            headers=headers,
            json={"p_key": f"probe:{uuid.uuid4()}", "p_window_seconds": 60},
        )
    except httpx.HTTPError:
        return False
    return r.status_code == 200


async def _delete_key(
    http: httpx.AsyncClient, base_url: str, headers: dict, key: str
) -> None:
    """Service-role cleanup of a test key's counter rows (RLS-bypassed)."""
    try:
        await http.delete(
            f"{base_url}/rest/v1/rate_limit_counters",
            headers=headers,
            params={"bucket_key": f"eq.{key}"},
        )
    except httpx.HTTPError:
        pass


async def _run_integration() -> None:
    base_url = _env("SUPABASE_URL", "http://127.0.0.1:54321")
    svc_key = _env("SUPABASE_SERVICE_ROLE_KEY", LOCAL_SERVICE_ROLE_KEY)
    assert base_url and svc_key
    headers = {
        "apikey": svc_key,
        "Authorization": f"Bearer {svc_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=10.0) as probe:
        if not await _rpc_available(probe, base_url.rstrip("/"), headers):
            print(
                "SKIP integration: rate_limit_count RPC unreachable "
                "(local Supabase down or migration 20260624170000 not applied)"
            )
            return

    # A huge window so every hit + the read land in one fixed bucket: the sliding
    # estimate then equals the raw count exactly (prev bucket empty), making the
    # durability assertion deterministic. Unique key so runs never collide.
    window = 3600
    n = 5
    durable_key = f"us075:durable:{uuid.uuid4()}"
    limit_key = f"us075:limit:{uuid.uuid4()}"

    cleanup_http = httpx.AsyncClient(timeout=10.0)
    try:
        # --- PRD Validation Test: durability across a "restart" -------------
        # Increment Z N times on one limiter instance...
        http1 = httpx.AsyncClient(timeout=10.0)
        rl1 = build_rate_limiter(
            "postgres", http=http1, supabase_url=base_url, service_role_key=svc_key
        )
        for expected in range(1, n + 1):
            d = await rl1.hit(durable_key, limit=10_000, window_seconds=window)
            assert d.count == expected, f"hit {expected}: got count {d.count}"
            assert d.allowed is True
        await http1.aclose()  # the process holding rl1 goes away (simulated restart)

        # ...then a FRESH limiter + client (the "restarted" backend) reads N back.
        # Counter survived in Postgres, proving it is durable, not in-process.
        http2 = httpx.AsyncClient(timeout=10.0)
        rl2 = build_rate_limiter(
            "postgres", http=http2, supabase_url=base_url, service_role_key=svc_key
        )
        survived = await rl2.count(durable_key, window_seconds=window)
        await http2.aclose()
        assert survived == n, f"counter did not survive restart: {survived} != {n}"
        print(f"  durability: {n} hits survived a simulated restart at value {survived}")

        # --- The limit decision flips when the window is exceeded ------------
        http3 = httpx.AsyncClient(timeout=10.0)
        rl3 = build_rate_limiter(
            "postgres", http=http3, supabase_url=base_url, service_role_key=svc_key
        )
        for expected in (1, 2, 3):
            d = await rl3.hit(limit_key, limit=3, window_seconds=window)
            assert d.count == expected and d.allowed is True, f"under limit: {d}"
        over = await rl3.hit(limit_key, limit=3, window_seconds=window)
        await http3.aclose()
        assert over.count == 4 and over.allowed is False, f"over limit: {over}"
        print("  limit decision: allowed=True up to the limit, then False over it")

        print("OK: US-075 rate limiter is durable across restart and decides the limit")
    finally:
        await _delete_key(cleanup_http, base_url.rstrip("/"), headers, durable_key)
        await _delete_key(cleanup_http, base_url.rstrip("/"), headers, limit_key)
        await cleanup_http.aclose()


# -----------------------------------------------------------------------------


def main() -> None:
    print("US-075 rate limiter — unit layer:")
    test_name_selection_and_default()
    test_no_in_memory_backend()
    test_postgres_factory_fail_closed()
    test_redis_factory_requires_url_and_package()
    test_decision_shape_and_abc()
    test_postgres_backend_over_mock_transport()
    test_redis_backend_over_fake_client()
    print("US-075 rate limiter — integration layer:")
    asyncio.run(_run_integration())


if __name__ == "__main__":
    main()
