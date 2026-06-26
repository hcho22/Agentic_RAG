"""US-075 (ADR-0008): swappable `RateLimiter` seam — default Postgres, optional Redis.

The public widget surface (US-076 per-key + per-session/IP limit, US-077 per-
workspace circuit breaker) drives PAID retrieval + LLM draft/judge calls, so it is
a cost-amplification DoS target. This module is the abuse-counter seam those
stories draw down against, mirroring the existing provider factories in the repo
(`reranking.build_reranker`, `web_search.build_web_search_provider`,
`parsing.build_parser`): an ABC + an env-selected factory so a call site is
byte-identical no matter which backend is configured.

Backends (`RATE_LIMITER=postgres|redis`, default `postgres`):

  * PostgresRateLimiter (DEFAULT) — counters live in Postgres
    (`rate_limit_counters`, migration 20260624170000) and are reached through two
    SECURITY DEFINER RPCs (`rate_limit_hit`, `rate_limit_count`) granted to the
    service role only, called over PostgREST exactly like US-071's
    `resume_conversation`. No new infra, durable across restarts, correct
    cross-instance.
  * RedisRateLimiter (OPTIONAL, documented for scale) — the same protocol backed
    by Redis for deployments that outgrow Postgres counter contention. `redis` is
    an OPTIONAL dependency (NOT in requirements.txt); the factory lazily imports
    it and raises a clear, actionable error when `RATE_LIMITER=redis` but the
    package is absent.

WHY THERE IS NO IN-PROCESS / IN-MEMORY BACKEND (ADR-0008, recorded here on
purpose): an in-memory counter is per-instance, so behind N replicas it admits up
to N× the intended rate (each instance counts only its own slice), and it resets
to zero on every restart/redeploy — turning a deploy into a free abuse window.
Both defeat the entire point of a cost-DoS guard, so an in-memory adapter is
deliberately NOT offered. Durable + cross-instance is the floor, not an upgrade.

Algorithm (both backends): a sliding-window counter using two adjacent fixed
windows — the current bucket's exact count plus the previous bucket weighted by
the fraction of it still inside the trailing window. This smooths the fixed-window
edge burst into a proper sliding bound while staying bounded to <=2 live buckets
per (key, window) (no per-hit row/key growth). The Postgres weighting lives in the RPC; the
Redis adapter mirrors it in the client (its own clock), a documented approximation
under cross-instance clock skew — acceptable for any distributed limiter.

Tests: `python -m backend.test_us075_rate_limiter` (a unit layer that always runs
— factory selection, no-in-memory, decision shape, fail-closed config, Redis
lazy-import error — plus an integration layer that skips cleanly without a local
Supabase: durable incr/peek across a simulated restart, and the limit decision).
"""

from __future__ import annotations

import math
import os
import time
from abc import ABC, abstractmethod
from typing import Literal

import httpx
from pydantic import BaseModel

RateLimiterName = Literal["postgres", "redis"]

DEFAULT_RATE_LIMITER: RateLimiterName = "postgres"


class RateLimitDecision(BaseModel):
    """The outcome of one `RateLimiter.hit`.

    `count` is the post-increment sliding-window estimate (this hit included);
    `allowed` is `count <= limit`. The two travel together so a caller never has
    to re-read the counter to decide (no time-of-check/time-of-use gap).
    """

    allowed: bool
    count: int
    limit: int
    window_seconds: int


class RateLimiter(ABC):
    """A durable, cross-instance sliding-window counter.

    Implementations MUST be safe to call concurrently and MUST persist counts
    outside the process (so a restart does not reset them) and outside any single
    instance (so replicas share one window). `key` is an opaque string the caller
    composes (e.g. `"key:<public_key>"`, `"ip:<addr>"`, `"ws:<workspace_id>"`);
    this seam assigns it no meaning and enforces no trust boundary — it only counts.
    """

    name: str

    @abstractmethod
    async def hit(
        self, key: str, *, limit: int, window_seconds: int, cost: int = 1
    ) -> RateLimitDecision:
        """Record `cost` against `key`'s window; return the post-increment decision.

        A blocked hit (estimate already over `limit`) still counts, by design: it
        keeps a hammering caller's own window saturated while a caller that backs
        off recovers as the window slides.
        """
        ...

    @abstractmethod
    async def count(self, key: str, *, window_seconds: int) -> int:
        """Read `key`'s current sliding-window estimate WITHOUT recording a hit."""
        ...

    async def aclose(self) -> None:
        """Release any backend resources this limiter OWNS.

        Default no-op: the Postgres backend borrows an injected `httpx` client (the
        caller closes it). The Redis backend, which opens its own client, overrides
        this to close it.
        """
        return None


# --- Postgres backend (default) ----------------------------------------------


class PostgresRateLimiter(RateLimiter):
    """Default backend: counters in Postgres, reached via service-role RPCs.

    Holds the injected `httpx` client + the resolved service-role headers and calls
    the `rate_limit_hit` / `rate_limit_count` RPCs (migration 20260624170000) over
    PostgREST — the same backend-mediated, service-role-only posture as US-071's
    `resume_conversation`. Durable (rows survive a restart) and correct
    cross-instance (one shared store) by construction.

    The service-role key bypasses RLS: it is server-side only and is never logged,
    returned, or built into an error here (errors carry only HTTP status + a body
    snippet, never headers).
    """

    name = "postgres"

    def __init__(
        self, *, http: httpx.AsyncClient, supabase_url: str, service_role_key: str
    ) -> None:
        self._http = http
        self._base_url = supabase_url.rstrip("/")
        # Built once and reused; the key never appears in an error message.
        self._headers = {
            "apikey": service_role_key,
            "Authorization": f"Bearer {service_role_key}",
            "Content-Type": "application/json",
        }

    def _error(self, action: str, response: httpx.Response) -> RuntimeError:
        snippet = (response.text or "")[:300]
        return RuntimeError(
            f"rate limiter (postgres) failed during {action}: "
            f"HTTP {response.status_code} {snippet}"
        )

    async def hit(
        self, key: str, *, limit: int, window_seconds: int, cost: int = 1
    ) -> RateLimitDecision:
        r = await self._http.post(
            f"{self._base_url}/rest/v1/rpc/rate_limit_hit",
            headers=self._headers,
            json={
                "p_key": key,
                "p_limit": int(limit),
                "p_window_seconds": window_seconds,
                "p_cost": int(cost),
            },
        )
        if r.status_code != 200:
            raise self._error("rate_limit_hit", r)
        rows = r.json()
        # `returns table(...)` -> PostgREST returns a one-row array.
        row = rows[0] if isinstance(rows, list) and rows else rows
        return RateLimitDecision(
            allowed=bool(row["allowed"]),
            count=int(row["current_count"]),
            limit=int(row["limit_value"]),
            window_seconds=int(row["window_seconds"]),
        )

    async def count(self, key: str, *, window_seconds: int) -> int:
        r = await self._http.post(
            f"{self._base_url}/rest/v1/rpc/rate_limit_count",
            headers=self._headers,
            json={"p_key": key, "p_window_seconds": window_seconds},
        )
        if r.status_code != 200:
            raise self._error("rate_limit_count", r)
        # `returns bigint` -> PostgREST returns the scalar directly.
        return int(r.json())


# --- Redis backend (optional, documented for scale) --------------------------


class RedisRateLimiter(RateLimiter):
    """Optional scale backend: the same sliding-window counter, backed by Redis.

    Provided for deployments that outgrow Postgres counter contention. `redis` is
    an OPTIONAL dependency — it is NOT in requirements.txt — so this class takes an
    already-constructed async client (the factory builds it after a lazy import).

    Each instance buckets time on ITS OWN clock and weights the previous bucket the
    same way the Postgres RPC does; under cross-instance clock skew the sliding
    estimate is a documented approximation, the same caveat any distributed limiter
    carries. Buckets expire after `2 * window` so the keyspace stays bounded with no
    sweeper. The Lua script makes the increment+read atomic on the Redis side.
    """

    name = "redis"

    # Atomically increment the current bucket, (re)set its TTL, and read the
    # previous bucket. Returns [current, previous]. Keeping this in Lua avoids a
    # read-modify-write race between the INCR and the GET under concurrency.
    _HIT_LUA = """
    local cur = redis.call('INCRBY', KEYS[1], tonumber(ARGV[1]))
    redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[2]))
    local prev = redis.call('GET', KEYS[2])
    return {cur, prev and tonumber(prev) or 0}
    """

    def __init__(self, client: object, *, key_prefix: str = "rl") -> None:
        self._redis = client
        self._prefix = key_prefix

    def _buckets(self, key: str, window_seconds: int) -> tuple[str, str, float]:
        """Return (current_bucket_key, previous_bucket_key, prev_weight)."""
        now = time.time()
        idx = math.floor(now / window_seconds)
        cur_start = idx * window_seconds
        prev_weight = (window_seconds - (now - cur_start)) / window_seconds
        base = f"{self._prefix}:{key}:{window_seconds}"
        return f"{base}:{idx}", f"{base}:{idx - 1}", prev_weight

    async def hit(
        self, key: str, *, limit: int, window_seconds: int, cost: int = 1
    ) -> RateLimitDecision:
        if cost < 0:
            raise ValueError("cost must be >= 0")
        cur_key, prev_key, prev_weight = self._buckets(key, window_seconds)
        ttl_ms = int(window_seconds * 2 * 1000)
        cur, prev = await self._redis.eval(  # type: ignore[attr-defined]
            self._HIT_LUA, 2, cur_key, prev_key, cost, ttl_ms
        )
        estimate = math.ceil(int(cur) + int(prev) * prev_weight)
        return RateLimitDecision(
            allowed=estimate <= limit,
            count=estimate,
            limit=limit,
            window_seconds=window_seconds,
        )

    async def count(self, key: str, *, window_seconds: int) -> int:
        cur_key, prev_key, prev_weight = self._buckets(key, window_seconds)
        cur = await self._redis.get(cur_key)  # type: ignore[attr-defined]
        prev = await self._redis.get(prev_key)  # type: ignore[attr-defined]
        cur_n = int(cur) if cur is not None else 0
        prev_n = int(prev) if prev is not None else 0
        return math.ceil(cur_n + prev_n * prev_weight)

    async def aclose(self) -> None:
        # This backend OWNS its client (the factory built it), so it closes it.
        aclose = getattr(self._redis, "aclose", None) or getattr(
            self._redis, "close", None
        )
        if aclose is not None:
            await aclose()


# --- Selection + factory (matches the repo's other seams) --------------------


def get_rate_limiter_name() -> RateLimiterName:
    """`RATE_LIMITER` env: `postgres` (default) | `redis`.

    There is intentionally no `none`/in-memory option (see the module docstring):
    the seam's contract is durable + cross-instance, and a no-op limiter would
    silently disable the cost-DoS guard.
    """
    raw = (os.environ.get("RATE_LIMITER") or DEFAULT_RATE_LIMITER).strip().lower()
    if raw not in ("postgres", "redis"):
        raise ValueError(
            f"RATE_LIMITER must be one of postgres|redis, got {raw!r}"
        )
    return raw  # type: ignore[return-value]


def build_rate_limiter(
    name: RateLimiterName,
    *,
    http: httpx.AsyncClient | None = None,
    supabase_url: str | None = None,
    service_role_key: str | None = None,
    redis_url: str | None = None,
) -> RateLimiter:
    """Factory matching `RATE_LIMITER` to a concrete backend.

    Fail-closed on missing configuration AT BUILD TIME (like the reranker /
    web-search factories raise on missing API keys) so a misconfiguration surfaces
    at startup, never as a silently-unprotected request path:

      * `postgres` requires `http` plus `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`
        (resolvable from env or the explicit overrides). The service-role key is
        what the abuse counters' deny-all RLS demands; without it there is no path.
      * `redis` requires the OPTIONAL `redis` package (lazily imported here) and a
        `REDIS_URL` (env or override).

    Swapping the backend is exactly this `name` flip — no call site changes, since
    every caller talks only to the `RateLimiter` ABC.
    """
    if name == "postgres":
        if http is None:
            raise ValueError(
                "RATE_LIMITER=postgres requires an httpx.AsyncClient (http=...)"
            )
        url = supabase_url if supabase_url is not None else os.environ.get("SUPABASE_URL")
        if not url:
            raise ValueError("RATE_LIMITER=postgres requires SUPABASE_URL")
        key = (
            service_role_key
            if service_role_key is not None
            else os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        )
        if not key:
            raise ValueError(
                "RATE_LIMITER=postgres requires SUPABASE_SERVICE_ROLE_KEY "
                "(the abuse counters are deny-all RLS; only the service role can "
                "draw them down). It bypasses RLS — keep it server-side."
            )
        return PostgresRateLimiter(http=http, supabase_url=url, service_role_key=key)

    if name == "redis":
        url = redis_url if redis_url is not None else os.environ.get("REDIS_URL")
        if not url:
            raise ValueError("RATE_LIMITER=redis requires REDIS_URL")
        try:
            import redis.asyncio as redis_asyncio  # type: ignore[import-untyped, import-not-found]
        except ImportError as e:
            raise RuntimeError(
                "RATE_LIMITER=redis selected but the optional 'redis' package is "
                "not installed. It is intentionally not in requirements.txt (the "
                "default backend is Postgres). Install it (`pip install redis`) to "
                "use the Redis adapter."
            ) from e
        client = redis_asyncio.from_url(url, decode_responses=True)
        return RedisRateLimiter(client)

    raise ValueError(f"unhandled rate limiter name: {name}")  # pragma: no cover
