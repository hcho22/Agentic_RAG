"""US-071: opaque per-conversation customer token primitives (ADR-0008, amends ADR-0004).

This token is the anonymous customer's reconnect credential. It is deliberately
NOT a Supabase JWT and is NOT signed with `SUPABASE_JWT_SECRET` — that secret
mints the *bot's* identity token (`backend.supabase_jwt.mint_supabase_jwt`,
US-068), a different mechanism entirely. Conflating the two would drag the
anonymous customer onto the Supabase trust surface, which is exactly what
ADR-0008 keeps them off of.

Instead the token is cryptographically-random opaque bytes:

  * `generate_conversation_token()` -> a fresh 256-bit `secrets.token_urlsafe`
    string, returned in the clear EXACTLY ONCE at conversation creation (only to
    the iframe; US-078 wires that flow).
  * `hash_conversation_token(raw)` -> the SHA-256 hex digest that is the ONLY
    representation stored server-side (`public.conversation_tokens.token_hash`).
    The raw token never touches the database, a log line, an SSE event, or any
    response body other than the single creation response.

Continuity comes solely from the iframe-origin-stored raw token: there is NO
server-side customer-identity table. On every reload the iframe presents the raw
token to the backend, which hashes it and calls the `resume_conversation` RPC
(service-role only) to revalidate (`not expired AND status != 'resolved'`) and
slide the 24h window. Resolve invalidates the token (the RPC's status gate plus
the purge-on-resolve trigger). See `supabase/migrations/20260624160000_conversation_tokens.sql`.

These primitives are pure (no DB, no secrets) so they are always unit-testable;
the DB issue/resume plumbing lives in `main.py` (the widget endpoints) and is
exercised by the integration layer of `test_us071_conversation_tokens.py`.
"""

from __future__ import annotations

import hashlib
import secrets

# ADR-0008 lifetime: ~24h, slid forward on every successful resume (activity)
# while the conversation is not resolved. The slide happens in the
# `resume_conversation` RPC; this constant is the issuance TTL.
CONVERSATION_TOKEN_TTL_SECONDS = 24 * 60 * 60

# 32 bytes = 256 bits of entropy. token_urlsafe base64url-encodes this to a
# ~43-char string, far beyond brute-force/guessing of the stored hash.
_TOKEN_NBYTES = 32


def generate_conversation_token() -> str:
    """Return a fresh cryptographically-random opaque token (NOT a JWT).

    The returned value is the RAW token handed to the iframe exactly once at
    conversation creation. It MUST be hashed via `hash_conversation_token` before
    storage and MUST NEVER be persisted, logged, or echoed anywhere else.
    """
    return secrets.token_urlsafe(_TOKEN_NBYTES)


def hash_conversation_token(raw_token: str) -> str:
    """Return the SHA-256 hex digest stored as `conversation_tokens.token_hash`.

    Hashing is what lets the table hold only a non-reversible fingerprint: a DB
    read (or dump) of `token_hash` cannot reconstruct the raw token, so it cannot
    be replayed to resume a conversation. The backend recomputes this digest from
    the raw token the customer presents and looks the row up by it.

    Raises:
        ValueError: `raw_token` is empty (an empty credential must never resolve).
    """
    if not raw_token:
        raise ValueError("raw_token must be a non-empty string")
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
