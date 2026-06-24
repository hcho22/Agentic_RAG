"""US-068: self-signed Supabase-compatible JWT minting primitive (ADR-0008).

`mint_supabase_jwt(sub, ttl_seconds)` is the **single** place the backend issues
a Supabase-shaped identity token. It self-signs a short-lived HS256 JWT with the
project's `SUPABASE_JWT_SECRET` — the *same* secret GoTrue signs with — so a
token minted here is, to PostgREST and every RLS predicate, indistinguishable
from a GoTrue-issued one: `auth.uid()` resolves to `sub`, the request runs under
the `authenticated` Postgres role, and `exp` is enforced by PostgREST. This makes
the helper a new *issuer* beside GoTrue, **not** a new enforcement path — the
membership/ACL boundary in the database is untouched.

Its only caller is the support bot (US-070): each customer turn mints a ~60s
token for `sub = bot_user_id`, calls `match_chunks` as that principal, and
discards the token. The bot is an ordinary `role='member'` workspace principal
(US-069), so the self-minted token grants exactly the bot's share-to-bot reach
and nothing more.

Why self-sign instead of a GoTrue admin-API session per request (ADR-0008): a
GoTrue `admin.createSession`-style call would add a network round-trip on every
turn *and* drag the service-role key into the request hot path. Self-signing
needs only the JWT secret the backend already must trust to validate inbound
tokens, costs no round-trip, and keeps the service-role key out of the per-turn
path. The trade is that the backend now *holds a signing secret*, recorded below.

Threat-model (P5) line — NEW signing surface. Before US-068 the backend held only
the anon key (a public, non-signing key) and forwarded user tokens it never
minted; the Identity Boundary was wholly GoTrue (ADR-0002 P5). US-068 adds the
token-minting surface ADR-0002's P5 line anticipated ("a foreign-JWT exchange
adapter ... would add a token-minting surface requiring its own audit"). The
audit boundary of that surface is exactly this module: it mints ONLY for a
server-resolved `bot_user_id`, never for a customer- or request-supplied `sub`,
the minted token is server-side-only and MUST NEVER reach an HTTP response body,
SSE event, or log line bound for the iframe/client, and the secret lives only in
`SUPABASE_JWT_SECRET` (set on the deploy target, never embedded client-side).
Anyone holding `SUPABASE_JWT_SECRET` can forge any identity — it is the crown
jewel and is scoped accordingly.
"""

from __future__ import annotations

import os
import time

import jwt as pyjwt

# Supabase's `authenticated` role + audience. PostgREST SETs the Postgres role
# from the `role` claim and resolves `auth.uid()` from `sub`; `aud` matches what
# GoTrue stamps so the token is accepted under a `jwt-aud = authenticated` config.
_AUTHENTICATED = "authenticated"
_ALGORITHM = "HS256"


def _resolve_secret(secret: str | None) -> str:
    """Return the signing secret, fail-closed.

    Resolves at call time (not import) so a deployment that never enables the
    support bot need not set `SUPABASE_JWT_SECRET`, and tests can set it before
    the first mint. `secret` is an explicit override for tests; production passes
    nothing and the env var is read.
    """
    resolved = secret if secret is not None else os.environ.get("SUPABASE_JWT_SECRET")
    if not resolved:
        raise RuntimeError(
            "SUPABASE_JWT_SECRET is not configured. It is required to mint the "
            "support-bot token (US-068/US-070). Set it to the project's JWT "
            "secret (the same value GoTrue signs with) on the deploy target. It "
            "is a signing secret — never embed it client-side."
        )
    return resolved


def mint_supabase_jwt(
    sub: str,
    ttl_seconds: int,
    *,
    secret: str | None = None,
) -> str:
    """Mint a short-lived HS256 Supabase-compatible JWT for `sub`.

    The returned token is server-side-only (see module docstring): it MUST NOT be
    written to any client-facing response, SSE event, or log line.

    Args:
        sub: the `auth.users` id the token authenticates as (e.g. `bot_user_id`).
            Stringified — `auth.uid()` resolves to this value. Must not be None
            or stringify to empty/whitespace (it would mint a bogus principal).
        ttl_seconds: lifetime in seconds; must be positive. The bot uses ~60s.
        secret: test-only override for the signing secret. Production omits it and
            the secret is read from `SUPABASE_JWT_SECRET`.

    Returns:
        A compact HS256 JWT string with claims `sub`, `role='authenticated'`,
        `aud='authenticated'`, `iat`, `exp` (= `iat + ttl_seconds`).

    Raises:
        ValueError: `sub` is None or stringifies to empty/whitespace, or
            `ttl_seconds` is not a positive integer.
        RuntimeError: `SUPABASE_JWT_SECRET` is unset (and no `secret` override).
    """
    if sub is None:
        raise ValueError("sub must not be None")
    sub_str = str(sub)
    if not sub_str.strip():
        raise ValueError("sub must not be empty or whitespace-only")
    if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool):
        raise ValueError("ttl_seconds must be an int")
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    signing_secret = _resolve_secret(secret)

    now = int(time.time())
    claims = {
        "sub": sub_str,
        "role": _AUTHENTICATED,
        "aud": _AUTHENTICATED,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    return pyjwt.encode(claims, signing_secret, algorithm=_ALGORITHM)
