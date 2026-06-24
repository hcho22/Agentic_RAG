"""US-068 test: the self-signed Supabase-compatible JWT minting primitive.

Two layers, in the spirit of `backend/test_share_api.py` (self-minted HS256
JWTs) and `backend/test_us066_conversations_rls.py` (asyncpg seed + PostgREST,
skips cleanly on missing env):

1. Unit (always run, no DB / no secrets): the primitive
   (`backend.supabase_jwt.mint_supabase_jwt`) emits exactly the AC claim set
   (`sub`, `role='authenticated'`, `aud='authenticated'`, `iat`, `exp = iat +
   ttl`), signs HS256 with the given secret, fails closed when the secret is
   unset, validates `ttl_seconds`, and produces a token a verifier rejects once
   past `exp`.

2. Integration (skips cleanly when the DB / `conversations` table is absent):
   mirrors the PRD US-068 Validation Test against a real local Supabase — a token
   minted by the primitive for `sub=B` is accepted by PostgREST and resolves
   `auth.uid()` to B (B, a workspace member, reads its conversation; a token for a
   *different* sub reads 0 rows, proving the identity genuinely comes from the
   token's `sub`); an expired minted token is rejected (401). This is the
   "accepted by PostgREST/match_chunks exactly like a GoTrue-issued JWT" AC, run
   through the same `auth.uid()` + membership RLS path the bot token (US-070)
   will traverse — match_chunks is the same boundary with a heavier fixture, so
   the conversations read is the lighter faithful realization.

Run:
    python -m backend.test_supabase_jwt

Requires (integration layer only; unit layer always runs):
    - Local Supabase running; DATABASE_URL (or PERMISSIONS_TEST_DATABASE_URL)
      pointing at its DB; US-066's `conversations` migration applied.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from pathlib import Path

import asyncpg
import httpx
import jwt as pyjwt

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from supabase_jwt import mint_supabase_jwt  # noqa: E402

# A throwaway secret for the unit layer — never an env value, so the unit tests
# need no configuration. The local-dev project secret (for the integration
# layer) is resolved separately below.
UNIT_SECRET = "unit-test-secret-at-least-32-characters-long-xx"

LOCAL_DB_URL = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
LOCAL_SUPABASE_URL = "http://127.0.0.1:54321"
LOCAL_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6ImFub24iLCJleHAiOjE5ODM4MTI5OTZ9."
    "CRXP1A7WOeoJeXxjNni43kdQwgnWNReilDMblYTn_I0"
)
LOCAL_JWT_SECRET = "super-secret-jwt-token-with-at-least-32-characters-long"
INSTANCE = "00000000-0000-0000-0000-000000000000"


def _env(key: str, default: str | None = None) -> str | None:
    val = os.environ.get(key)
    return val if val else default


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _decode(token: str, secret: str) -> dict:
    """Verify signature + audience exactly as PostgREST/Supabase would."""
    return pyjwt.decode(
        token, secret, algorithms=["HS256"], audience="authenticated"
    )


# --------------------------------------------------------------------------- #
# Unit layer — no DB, no secrets.
# --------------------------------------------------------------------------- #


def test_claims_shape() -> int:
    sub = str(uuid.uuid4())
    before = int(time.time())
    token = mint_supabase_jwt(sub, 60, secret=UNIT_SECRET)
    after = int(time.time())

    claims = _decode(token, UNIT_SECRET)
    _check(claims["sub"] == sub, f"sub mismatch: {claims.get('sub')!r}")
    _check(claims["role"] == "authenticated", f"role: {claims.get('role')!r}")
    _check(claims["aud"] == "authenticated", f"aud: {claims.get('aud')!r}")
    # iat is stamped at mint time, exp is exactly iat + ttl (the latch the bot's
    # ~60s lifetime relies on).
    _check(before <= claims["iat"] <= after, f"iat out of range: {claims['iat']}")
    _check(claims["exp"] == claims["iat"] + 60, f"exp != iat + ttl: {claims}")
    # No surprise claims beyond the documented AC set — the minting surface stays
    # auditable and minimal.
    _check(
        set(claims) == {"sub", "role", "aud", "iat", "exp"},
        f"unexpected claim set: {sorted(claims)}",
    )
    print("  unit: claims shape (sub/role/aud/iat/exp, exp == iat + ttl)")
    return 1


def test_sub_is_stringified() -> int:
    sub = uuid.uuid4()  # a non-str sub (uuid) must serialize to its str form
    claims = _decode(mint_supabase_jwt(sub, 60, secret=UNIT_SECRET), UNIT_SECRET)  # type: ignore[arg-type]
    _check(claims["sub"] == str(sub), f"uuid sub not stringified: {claims['sub']!r}")
    print("  unit: sub is stringified")
    return 1


def test_sub_validation() -> int:
    # Symmetric with ttl validation: a None/empty/whitespace sub must never mint a
    # token (it would resolve auth.uid() to a bogus principal). A uuid/str sub with
    # real content still passes — see test_sub_is_stringified.
    for bad in (None, "", "   ", "\t\n"):
        try:
            mint_supabase_jwt(bad, 60, secret=UNIT_SECRET)  # type: ignore[arg-type]
        except ValueError:
            pass
        else:
            raise AssertionError(f"sub={bad!r} should be rejected")
    print("  unit: sub validation (None / empty / whitespace rejected)")
    return 1


def test_signature_is_bound_to_secret() -> int:
    token = mint_supabase_jwt(str(uuid.uuid4()), 60, secret=UNIT_SECRET)
    # Right secret verifies.
    _decode(token, UNIT_SECRET)
    # Wrong secret is rejected — the token is only as trustworthy as the secret.
    try:
        _decode(token, "a-totally-different-secret-also-32-chars-long")
    except pyjwt.InvalidSignatureError:
        print("  unit: signature bound to secret (wrong secret rejected)")
        return 1
    raise AssertionError("a token verified under the WRONG secret")


def test_expired_token_is_rejected() -> int:
    # The no-DB analog of the PRD's "wait past TTL, retry -> rejected": a real
    # token with a 1s TTL must fail verification once expired (leeway defaults
    # to 0, so a short real wait suffices).
    token = mint_supabase_jwt(str(uuid.uuid4()), 1, secret=UNIT_SECRET)
    _decode(token, UNIT_SECRET)  # fresh: accepted
    time.sleep(1.5)
    try:
        _decode(token, UNIT_SECRET)
    except pyjwt.ExpiredSignatureError:
        print("  unit: expired token rejected (TTL enforced)")
        return 1
    raise AssertionError("an expired token verified successfully")


def test_ttl_validation() -> int:
    sub = str(uuid.uuid4())
    for bad in (0, -1, -60):
        try:
            mint_supabase_jwt(sub, bad, secret=UNIT_SECRET)
        except ValueError:
            pass
        else:
            raise AssertionError(f"ttl_seconds={bad} should be rejected")
    # bool is an int subclass but is not a valid ttl; reject it explicitly so a
    # stray True can't mint a 1s token by accident.
    for bad_type in (True, 1.5, "60", None):
        try:
            mint_supabase_jwt(sub, bad_type, secret=UNIT_SECRET)  # type: ignore[arg-type]
        except ValueError:
            pass
        else:
            raise AssertionError(f"ttl_seconds={bad_type!r} should be rejected")
    print("  unit: ttl validation (non-positive / non-int rejected)")
    return 1


def test_missing_secret_fails_closed() -> int:
    # No override and no env var -> RuntimeError, never a token signed with an
    # empty/None secret.
    saved = os.environ.pop("SUPABASE_JWT_SECRET", None)
    try:
        mint_supabase_jwt(str(uuid.uuid4()), 60)
    except RuntimeError:
        ok = True
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"expected RuntimeError, got {type(exc).__name__}") from exc
    else:
        ok = False
    finally:
        if saved is not None:
            os.environ["SUPABASE_JWT_SECRET"] = saved
    _check(ok, "missing secret did not fail closed")
    print("  unit: missing secret fails closed (RuntimeError)")
    return 1


def test_reads_secret_from_env() -> int:
    # With no override the primitive must read SUPABASE_JWT_SECRET from the env,
    # and the resulting token must verify under that same secret.
    saved = os.environ.get("SUPABASE_JWT_SECRET")
    os.environ["SUPABASE_JWT_SECRET"] = UNIT_SECRET
    try:
        token = mint_supabase_jwt(str(uuid.uuid4()), 60)
        _decode(token, UNIT_SECRET)
    finally:
        if saved is None:
            os.environ.pop("SUPABASE_JWT_SECRET", None)
        else:
            os.environ["SUPABASE_JWT_SECRET"] = saved
    print("  unit: reads SUPABASE_JWT_SECRET from env when no override")
    return 1


def test_token_does_not_embed_secret() -> int:
    # The compact JWT is header.payload.signature; the secret must never appear in
    # the serialized token (it signs, it is not carried).
    token = mint_supabase_jwt(str(uuid.uuid4()), 60, secret=UNIT_SECRET)
    _check(token.count(".") == 2, f"not a compact JWT: {token!r}")
    _check(UNIT_SECRET not in token, "signing secret leaked into the token body")
    print("  unit: token is compact JWT and does not embed the secret")
    return 1


def run_unit_layer() -> int:
    total = 0
    total += test_claims_shape()
    total += test_sub_is_stringified()
    total += test_sub_validation()
    total += test_signature_is_bound_to_secret()
    total += test_expired_token_is_rejected()
    total += test_ttl_validation()
    total += test_missing_secret_fails_closed()
    total += test_reads_secret_from_env()
    total += test_token_does_not_embed_secret()
    return total


# --------------------------------------------------------------------------- #
# Integration layer — real local Supabase; skips cleanly when absent.
# --------------------------------------------------------------------------- #


class Fixture:
    def __init__(self) -> None:
        self.ws = str(uuid.uuid4())            # workspace W
        self.bot = str(uuid.uuid4())           # sub B — a member of W
        self.outsider = str(uuid.uuid4())      # a sub NOT in any membership row
        self.conv = str(uuid.uuid4())          # conversation C in W


async def _table_exists(conn: asyncpg.Connection, table: str) -> bool:
    return (
        await conn.fetchval("select to_regclass($1)", f"public.{table}")
    ) is not None


async def _seed(conn: asyncpg.Connection, fx: Fixture) -> None:
    await conn.execute(
        """
        insert into auth.users
          (id, instance_id, email, encrypted_password, aud, role,
           raw_app_meta_data, raw_user_meta_data,
           created_at, updated_at, email_confirmed_at)
        values
          ($1, $2, $3, '', 'authenticated', 'authenticated',
           '{}'::jsonb, '{}'::jsonb, now(), now(), now())
        """,
        fx.bot, INSTANCE, f"bot-{fx.bot[:8]}@test.local",
    )
    await conn.execute(
        "insert into public.workspaces (id, name) values ($1, 'US068-W')", fx.ws
    )
    await conn.execute(
        """
        insert into public.workspace_membership (workspace_id, user_id, role)
        values ($1, $2, 'member')
        """,
        fx.ws, fx.bot,
    )
    await conn.execute(
        """
        insert into public.conversations (id, workspace_id, status)
        values ($1, $2, 'active')
        """,
        fx.conv, fx.ws,
    )


async def _cleanup(conn: asyncpg.Connection, fx: Fixture) -> None:
    await conn.execute(
        "delete from public.conversations where id = $1::uuid", fx.conv
    )
    await conn.execute("delete from public.workspaces where id = $1::uuid", fx.ws)
    await conn.execute("delete from auth.users where id = $1::uuid", fx.bot)


def _headers(token: str, anon_key: str) -> dict[str, str]:
    return {"apikey": anon_key, "Authorization": f"Bearer {token}"}


def _expired_like(sub: str, secret: str) -> str:
    """A token of the primitive's exact claim shape but already long-expired.

    The primitive refuses `ttl_seconds <= 0` (it must never mint a dead token),
    and PostgREST applies a ~30s clock-skew tolerance on `exp` — so cheaply
    waiting out a real short-TTL minted token is impossible (it would need a >30s
    sleep). This hand-rolls the identical claim shape with `exp` ~10 minutes in
    the past, beyond the tolerance, to prove PostgREST enforces `exp` (PGRST303)
    on tokens of this shape. The primitive's own exp-stamping (a real minted token
    expiring under a strict, zero-leeway verifier) is proven in the unit layer.
    """
    now = int(time.time())
    return pyjwt.encode(
        {
            "sub": str(sub),
            "role": "authenticated",
            "aud": "authenticated",
            "iat": now - 660,
            "exp": now - 600,
        },
        secret,
        algorithm="HS256",
    )


async def _run_integration() -> int | None:
    db_url = _env("PERMISSIONS_TEST_DATABASE_URL") or _env("DATABASE_URL") or LOCAL_DB_URL
    try:
        conn = await asyncpg.connect(db_url)
    except (OSError, asyncpg.PostgresError) as exc:
        print(f"SKIP integration: cannot connect to {db_url} ({exc})")
        return None

    try:
        if not await _table_exists(conn, "conversations"):
            print("SKIP integration: public.conversations absent (US-066 not applied)")
            return None

        supabase_url = _env("SUPABASE_URL", LOCAL_SUPABASE_URL)
        anon_key = _env("SUPABASE_ANON_KEY", LOCAL_ANON_KEY)
        jwt_secret = _env("SUPABASE_JWT_SECRET", LOCAL_JWT_SECRET)
        assert supabase_url and anon_key and jwt_secret  # for the type checker

        fx = Fixture()
        total = 0
        await _seed(conn, fx)
        try:
            async with httpx.AsyncClient(timeout=10.0) as http:
                base = f"{supabase_url}/rest/v1"

                # Mint for B via the PRIMITIVE (the real US-068 surface). PostgREST
                # must resolve auth.uid() to B so the membership RLS returns C.
                bot_token = mint_supabase_jwt(fx.bot, 60, secret=jwt_secret)
                r = await http.get(
                    f"{base}/conversations?id=eq.{fx.conv}&select=id",
                    headers=_headers(bot_token, anon_key),
                )
                _check(
                    r.status_code == 200,
                    f"fresh minted token rejected by PostgREST: {r.status_code} {r.text}",
                )
                _check(
                    [row["id"] for row in r.json()] == [fx.conv],
                    f"auth.uid() did not resolve to B (member): {r.text}",
                )
                total += 1
                print("  integration: minted token resolves auth.uid()==B (reads C)")

                # A token for a DIFFERENT sub reads 0 rows — proves the identity
                # genuinely comes from the token's `sub`, not from being accepted
                # blanket. (Same enforcement path the bot token will hit.)
                outsider_token = mint_supabase_jwt(fx.outsider, 60, secret=jwt_secret)
                r = await http.get(
                    f"{base}/conversations?id=eq.{fx.conv}&select=id",
                    headers=_headers(outsider_token, anon_key),
                )
                _check(
                    r.status_code == 200 and r.json() == [],
                    f"a token for a non-member sub read C: {r.status_code} {r.text}",
                )
                total += 1
                print("  integration: token for a different sub reads 0 rows (sub-bound)")

                # Expired token of this shape -> rejected by PostgREST (PRD step 3),
                # against the real enforcement surface (PGRST303), not just a local
                # verifier. See `_expired_like` for why this is hand-rolled rather
                # than waited out.
                r = await http.get(
                    f"{base}/conversations?id=eq.{fx.conv}&select=id",
                    headers=_headers(_expired_like(fx.bot, jwt_secret), anon_key),
                )
                _check(
                    r.status_code == 401,
                    f"expired token NOT rejected: {r.status_code} {r.text}",
                )
                total += 1
                print("  integration: expired token rejected by PostgREST (401)")
        finally:
            await _cleanup(conn, fx)
        return total
    finally:
        await conn.close()


def _run() -> None:
    unit_total = run_unit_layer()
    print(f"OK: US-068 unit layer — {unit_total} assertions on the minting primitive")

    integ_total = asyncio.run(_run_integration())
    if integ_total is None:
        print(
            "OK: US-068 — unit layer passed; integration layer skipped "
            "(no local Supabase / conversations table)"
        )
    else:
        print(
            f"OK: US-068 — unit + integration passed ({unit_total + integ_total} "
            "assertions); a self-minted token is accepted by PostgREST exactly "
            "like a GoTrue JWT, resolves auth.uid() to sub, and expires on TTL"
        )


if __name__ == "__main__":
    _run()
