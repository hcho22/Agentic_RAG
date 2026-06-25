"""US-072: widget public-key primitives (ADR-0008).

The widget `public_key` is the NON-SECRET identifier embedded in a buyer's page
JS (the loader <script>, US-083). It is NOT a credential — do not confuse it with
either of the two real secrets in this codebase:

  * `conversation_tokens` (US-071) holds a hashed *customer* credential signed
    with nothing; the public_key here is the opposite — world-readable, stored in
    the clear, granting no access by itself.
  * `supabase_jwt.mint_supabase_jwt` (US-068) mints the *bot's* identity token
    with `SUPABASE_JWT_SECRET`; the public_key is unrelated to that signing
    surface entirely.

A public_key only NAMES which workspace's support bot a widget instance talks to.
Resolution (`main.py`) maps it to `(workspace_id, …)` ONLY after gating on
`revoked_at IS NULL`; the leaked-key blast radius is the already-public KB, and
the hard abuse controls are the rate limit + circuit breaker (US-076/077), not
the key's secrecy.

These primitives are pure (no DB, no secrets) so they are always unit-testable;
the DB resolution / issuance / revoke plumbing lives in `main.py` (the widget +
admin endpoints) and is exercised by the integration layer of
`test_us072_widget_keys.py`.
"""

from __future__ import annotations

import secrets

# A human-recognizable, namespaced prefix so a public_key is obviously a widget
# key wherever it surfaces (client JS, logs, the admin UI) and cannot be mistaken
# for an opaque customer token (US-071) or a JWT. `pk` underlines "public key".
PUBLIC_KEY_PREFIX = "wk_pk_"

# The public_key is non-secret, so this entropy is for GLOBAL UNIQUENESS /
# unguessable enumeration, not confidentiality. 24 bytes base64url-encodes to a
# ~32-char suffix — comfortably collision-free for the unique constraint and not
# enumerable.
_PUBLIC_KEY_NBYTES = 24


def generate_public_key() -> str:
    """Return a fresh non-secret widget public key, e.g. ``wk_pk_<urlsafe>``.

    The returned value is embedded verbatim in client JS (it is NOT secret) and
    stored in the clear as `widget_keys.public_key`. The prefix makes its role
    self-evident; the random suffix makes it globally unique and non-enumerable.
    """
    return f"{PUBLIC_KEY_PREFIX}{secrets.token_urlsafe(_PUBLIC_KEY_NBYTES)}"


def is_widget_public_key(value: str | None) -> bool:
    """Cheap shape guard: does `value` look like a widget public key?

    Used to fail-fast on malformed resolution input before any DB round-trip (a
    blank or arbitrary string is rejected without touching Postgres). It is a
    FORMAT check only — it asserts nothing about existence or not-revoked status;
    that authoritative gate is the service-role lookup in `main.py`.
    """
    return bool(value) and value.startswith(PUBLIC_KEY_PREFIX) and len(value) > len(
        PUBLIC_KEY_PREFIX
    )
