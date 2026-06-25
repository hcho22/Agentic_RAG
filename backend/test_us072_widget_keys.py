"""US-072: widget_keys registry + not-revoked key resolution + admin RLS.

Two layers, in the style of `test_us071_conversation_tokens.py`:

  * a UNIT layer (always runs, no DB / no secrets): the pure key primitives in
    `backend/widget_keys.py` — a public key is non-secret, prefixed/namespaced,
    globally-unique random bytes, and the shape guard accepts only well-formed
    keys. (The public key is NOT a JWT and NOT the opaque customer token — it is
    the opposite, world-readable, granting no access by itself.)

  * an INTEGRATION / SECURITY layer (skips cleanly without a local DB), encoding
    the PRD US-072 "Validation Test" plus the structural failure indicators. The
    boundaries under test are installed by
    `supabase/migrations/20260624130000_widget_keys.sql`:

      Setup: workspace W; admin Ua (role='admin' of W), member Um (role='member');
      active key K and revoked key Kr in W; a live conversation C (token Tc)
      "started under Kr before revocation".

      1. Resolution gates on NOT-REVOKED — the exact backend path (service-role
         REST `widget_keys?public_key=eq&revoked_at=is.null`): K resolves to W
         (positive control), Kr resolves to ZERO rows (revoked → no minting).
      2. Admin RLS: Ua reads both keys / can insert / can revoke; Um (member, NOT
         admin) reads ZERO keys, cannot insert (RLS rejects), cannot revoke; a
         non-member reads ZERO. role='admin' is the gate — and it is the ONLY
         place role legitimately enters a predicate.
      3. Revoking Kr NEVER terminates the live conversation: after Kr is revoked,
         C still exists and its opaque token Tc still resumes (US-071) — the token
         is independent of the key once minted.
      4. Revocation is a one-way DB-enforced latch: once revoked_at is set, even a
         superuser UPDATE cannot clear it to NULL or move it (the BEFORE-UPDATE
         `widget_keys_revoke_guard` trigger rejects it), so Kr stays revoked.

    Failure indicator: a revoked key resolves (mints), a non-admin reads/writes
    keys, revoking a key kills an in-flight conversation, or a revoked key can be
    un-revoked behind the admin policy.

asyncpg connects as the `postgres` superuser for setup/teardown and DB-authoritative
gate checks (RLS is not bypassed for the PostgREST calls, which carry minted JWTs /
the service-role key exactly as the backend does). Skips cleanly when the DB /
migration is absent.

Run:
    python -m backend.test_us072_widget_keys

Requires a local Supabase + DATABASE_URL (or US072_TEST_DATABASE_URL) for the
integration layer; SUPABASE_URL / SUPABASE_ANON_KEY / SUPABASE_JWT_SECRET fall
back to the well-known local defaults. The unit layer needs nothing. No OpenAI.
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

from widget_keys import (  # noqa: E402
    PUBLIC_KEY_PREFIX,
    generate_public_key,
    is_widget_public_key,
)
from conversation_tokens import (  # noqa: E402
    generate_conversation_token,
    hash_conversation_token,
)

LOCAL_DB_URL = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
LOCAL_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6ImFub24iLCJleHAiOjE5ODM4MTI5OTZ9."
    "CRXP1A7WOeoJeXxjNni43kdQwgnWNReilDMblYTn_I0"
)
LOCAL_SERVICE_ROLE_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImV4cCI6MTk4MzgxMjk5Nn0."
    "EGIM96RAZx35lJzdJsyH-qQwv8Hdp7fsn3W0YpN81IU"
)
LOCAL_JWT_SECRET = "super-secret-jwt-token-with-at-least-32-characters-long"
INSTANCE = "00000000-0000-0000-0000-000000000000"


def _env(key: str, default: str | None = None) -> str | None:
    val = os.environ.get(key)
    return val if val else default


def _mint_user_jwt(user_id: str, email: str, secret: str) -> str:
    now = int(time.time())
    return pyjwt.encode(
        {
            "iss": "supabase-demo",
            "sub": user_id,
            "email": email,
            "role": "authenticated",
            "aud": "authenticated",
            "iat": now,
            "exp": now + 3600,
        },
        secret,
        algorithm="HS256",
    )


def _user_headers(jwt_token: str, anon_key: str) -> dict[str, str]:
    return {
        "apikey": anon_key,
        "Authorization": f"Bearer {jwt_token}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _service_headers(service_key: str) -> dict[str, str]:
    return {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
    }


# --------------------------------------------------------------------------- #
# Unit layer — pure primitives, always runs.
# --------------------------------------------------------------------------- #
def _run_unit() -> int:
    checks = 0

    # A public key is non-secret namespaced random bytes: prefixed, unique per
    # call, URL-safe, and NOT JWT-shaped (no dot-delimited segments). It is world-
    # readable by design — the prefix makes its role unmistakable.
    k1 = generate_public_key()
    k2 = generate_public_key()
    assert isinstance(k1, str) and isinstance(k2, str)
    assert k1 != k2, "public keys must be unique per call"
    assert k1.startswith(PUBLIC_KEY_PREFIX), f"missing {PUBLIC_KEY_PREFIX} prefix: {k1}"
    assert len(k1) > len(PUBLIC_KEY_PREFIX) + 20, f"public key too short: {k1}"
    suffix = k1[len(PUBLIC_KEY_PREFIX):]
    assert all(c.isalnum() or c in "-_" for c in suffix), "suffix must be URL-safe"
    checks += 1
    print("  unit: public keys are prefixed, unique, URL-safe random bytes")

    # The shape guard accepts only well-formed keys (cheap fail-fast before any DB
    # round-trip) and rejects blanks / the bare prefix / arbitrary strings.
    assert is_widget_public_key(k1)
    assert not is_widget_public_key("")
    assert not is_widget_public_key(None)
    assert not is_widget_public_key(PUBLIC_KEY_PREFIX), "bare prefix is not a key"
    assert not is_widget_public_key("not-a-widget-key")
    assert not is_widget_public_key("wk_pk")  # prefix not fully present
    checks += 1
    print("  unit: shape guard accepts well-formed keys, rejects blank/prefix/garbage")

    return checks


# --------------------------------------------------------------------------- #
# Integration / security layer — gated on a local DB.
# --------------------------------------------------------------------------- #
class Fixture:
    def __init__(self) -> None:
        self.ws = str(uuid.uuid4())                 # workspace W
        self.ua = str(uuid.uuid4())                 # admin of W (role='admin')
        self.um = str(uuid.uuid4())                 # member of W (role='member')
        self.outsider = str(uuid.uuid4())           # not a member of W (no row)
        self.k_active = str(uuid.uuid4())           # widget_keys id of K
        self.k_revoked = str(uuid.uuid4())          # widget_keys id of Kr
        self.pk_active = generate_public_key()      # public_key of K
        self.pk_revoked = generate_public_key()     # public_key of Kr
        self.conv = str(uuid.uuid4())               # live conversation C in W
        self.tc = generate_conversation_token()     # opaque token bound to C


async def _table_exists(conn: asyncpg.Connection, table: str) -> bool:
    return bool(await conn.fetchval("select to_regclass($1)", f"public.{table}"))


async def _seed(conn: asyncpg.Connection, fx: Fixture) -> None:
    await conn.execute(
        """
        insert into auth.users
          (id, instance_id, email, encrypted_password, aud, role,
           raw_app_meta_data, raw_user_meta_data,
           created_at, updated_at, email_confirmed_at)
        values
          ($1, $3, $4, '', 'authenticated', 'authenticated',
           '{}'::jsonb, '{}'::jsonb, now(), now(), now()),
          ($2, $3, $5, '', 'authenticated', 'authenticated',
           '{}'::jsonb, '{}'::jsonb, now(), now(), now())
        """,
        fx.ua, fx.um, INSTANCE,
        f"ua-{fx.ua[:8]}@test.local", f"um-{fx.um[:8]}@test.local",
    )
    await conn.execute(
        "insert into public.workspaces (id, name) values ($1, 'US072-W')", fx.ws
    )
    # Ua is an ADMIN of W; Um is a plain MEMBER. The admin/member split is what the
    # widget_keys RLS keys off — and the only legitimate use of role in a predicate.
    await conn.execute(
        """
        insert into public.workspace_membership (workspace_id, user_id, role)
        values ($1, $2, 'admin'), ($1, $3, 'member')
        """,
        fx.ws, fx.ua, fx.um,
    )
    # Two keys in W: K active, Kr active-for-now (revoked below, AFTER C is created
    # under it, mirroring "a live conversation started under Kr before revocation").
    await conn.execute(
        """
        insert into public.widget_keys
          (id, workspace_id, public_key, allowed_origins, created_by)
        values ($1, $2, $3, '{https://client.example}', $6),
               ($4, $2, $5, '{https://client.example}', $6)
        """,
        fx.k_active, fx.ws, fx.pk_active, fx.k_revoked, fx.pk_revoked, fx.ua,
    )
    # A live conversation C in W with an opaque token Tc (US-071), "started under
    # Kr". Once minted the token is independent of the key — revoking Kr must not
    # touch C or Tc.
    await conn.execute(
        "insert into public.conversations (id, workspace_id, status) values ($1, $2, 'active')",
        fx.conv, fx.ws,
    )
    await conn.execute(
        """
        insert into public.conversation_tokens (token_hash, conversation_id, expires_at)
        values ($1, $2, now() + interval '24 hours')
        """,
        hash_conversation_token(fx.tc), fx.conv,
    )
    # Revoke Kr (issue-new + revoke-old rotation: the active K is its replacement).
    await conn.execute(
        "update public.widget_keys set revoked_at = now() where id = $1::uuid",
        fx.k_revoked,
    )


async def _cleanup(conn: asyncpg.Connection, fx: Fixture) -> None:
    # conversations has no cascade from workspaces; drop it first (cascades its
    # tokens). widget_keys + membership cascade from workspaces.
    await conn.execute(
        "delete from public.conversations where id = $1::uuid", fx.conv
    )
    # Defensively drop any keys the admin-insert step created in W.
    await conn.execute(
        "delete from public.widget_keys where workspace_id = $1::uuid", fx.ws
    )
    await conn.execute("delete from public.workspaces where id = $1::uuid", fx.ws)
    await conn.execute(
        "delete from auth.users where id = any($1::uuid[])", [fx.ua, fx.um]
    )


async def _policy_rows(conn: asyncpg.Connection, table: str) -> list[asyncpg.Record]:
    return await conn.fetch(
        """
        select cmd, coalesce(qual, '') as qual, coalesce(with_check, '') as with_check
        from pg_policies
        where schemaname = 'public' and tablename = $1
        """,
        table,
    )


async def _assert_schema(conn: asyncpg.Connection) -> int:
    checks = 0

    # RLS enabled on widget_keys (a policy is inert if RLS is off).
    enabled = await conn.fetchval(
        "select relrowsecurity from pg_class where oid = 'public.widget_keys'::regclass"
    )
    assert enabled, "RLS not enabled on public.widget_keys"
    checks += 1
    print("  schema: RLS enabled on widget_keys")

    # The admin-management surface: every policy is gated on role='admin', and the
    # set of commands is exactly {SELECT, INSERT, UPDATE} — NO DELETE policy (a
    # revoked key is retained for audit; deny-by-default delete). This is the ONLY
    # table where role legitimately enters a predicate.
    rows = await _policy_rows(conn, "widget_keys")
    cmds = {r["cmd"] for r in rows}
    assert cmds == {"SELECT", "INSERT", "UPDATE"}, (
        f"widget_keys must have exactly SELECT/INSERT/UPDATE policies, got {cmds}"
    )
    for r in rows:
        pred = (r["qual"] + " " + r["with_check"]).lower()
        assert "role" in pred and "admin" in pred, (
            f"widget_keys {r['cmd']} policy must gate on role='admin', got: {pred}"
        )
    checks += 1
    print(
        "  schema: widget_keys policies = {SELECT,INSERT,UPDATE}, all role='admin'; "
        "no DELETE policy"
    )

    # public_key is globally unique (the resolution lookup key) and the
    # workspace_id listing index exists.
    uniq = await conn.fetchval(
        """
        select 1 from pg_constraint
        where conrelid = 'public.widget_keys'::regclass and contype = 'u'
        """
    )
    assert uniq, "widget_keys.public_key must carry a UNIQUE constraint"
    idx = await conn.fetchval(
        "select 1 from pg_indexes where schemaname='public' "
        "and indexname='widget_keys_workspace_id_idx'"
    )
    assert idx, "missing widget_keys_workspace_id_idx"
    checks += 1
    print("  schema: public_key UNIQUE + workspace_id index present")

    return checks


async def _run_integration() -> int:
    db_url = _env("US072_TEST_DATABASE_URL") or _env("DATABASE_URL") or LOCAL_DB_URL
    try:
        conn = await asyncpg.connect(db_url, timeout=5)
    except (OSError, asyncpg.PostgresError) as e:
        print(f"SKIP integration: cannot connect to local DB ({e})")
        return 0

    try:
        if not await _table_exists(conn, "widget_keys"):
            print("SKIP integration: widget_keys table absent (migration not applied)")
            return 0
        if not await _table_exists(conn, "conversations"):
            print("SKIP integration: conversations table absent (US-066 not applied)")
            return 0

        supabase_url = _env("SUPABASE_URL", "http://127.0.0.1:54321")
        anon_key = _env("SUPABASE_ANON_KEY", LOCAL_ANON_KEY)
        svc_key = _env("SUPABASE_SERVICE_ROLE_KEY", LOCAL_SERVICE_ROLE_KEY)
        jwt_secret = _env("SUPABASE_JWT_SECRET", LOCAL_JWT_SECRET)
        assert supabase_url and anon_key and svc_key and jwt_secret

        fx = Fixture()
        total = 0
        try:
            total += await _assert_schema(conn)
            await _seed(conn, fx)

            ua_email = await conn.fetchval(
                "select email from auth.users where id = $1::uuid", fx.ua
            )
            um_email = await conn.fetchval(
                "select email from auth.users where id = $1::uuid", fx.um
            )
            admin_h = _user_headers(_mint_user_jwt(fx.ua, ua_email, jwt_secret), anon_key)
            member_h = _user_headers(_mint_user_jwt(fx.um, um_email, jwt_secret), anon_key)
            outsider_h = _user_headers(
                _mint_user_jwt(fx.outsider, "out@test.local", jwt_secret), anon_key
            )
            svc_h = _service_headers(svc_key)

            async with httpx.AsyncClient(timeout=10.0) as http:
                # --- Step 1: resolution gates on NOT-REVOKED (the backend path) ---
                # Active K resolves to W; revoked Kr resolves to ZERO rows. This is
                # the exact service-role read the backend's _resolve_widget_key runs.
                rk = await http.get(
                    f"{supabase_url}/rest/v1/widget_keys",
                    params={
                        "public_key": f"eq.{fx.pk_active}",
                        "revoked_at": "is.null",
                        "select": "id,workspace_id",
                    },
                    headers=svc_h,
                )
                assert rk.status_code == 200, f"{rk.status_code} {rk.text}"
                active_rows = rk.json()
                assert len(active_rows) == 1 and active_rows[0]["workspace_id"] == fx.ws, (
                    f"active key K must resolve to W, got {active_rows}"
                )

                rkr = await http.get(
                    f"{supabase_url}/rest/v1/widget_keys",
                    params={
                        "public_key": f"eq.{fx.pk_revoked}",
                        "revoked_at": "is.null",
                        "select": "id,workspace_id",
                    },
                    headers=svc_h,
                )
                assert rkr.status_code == 200, f"{rkr.status_code} {rkr.text}"
                assert rkr.json() == [], (
                    f"REVOKED key Kr must resolve to ZERO rows (no minting), got {rkr.json()}"
                )
                total += 1
                print("  step 1: active K → W; revoked Kr → 0 rows (resolution gate)")

                # --- Step 2: admin RLS ---
                # Ua (admin) sees BOTH keys; Um (member) and the outsider see ZERO.
                admin_list = await http.get(
                    f"{supabase_url}/rest/v1/widget_keys",
                    params={"workspace_id": f"eq.{fx.ws}", "select": "id"},
                    headers=admin_h,
                )
                assert admin_list.status_code == 200, admin_list.text
                admin_ids = {r["id"] for r in admin_list.json()}
                assert admin_ids == {fx.k_active, fx.k_revoked}, (
                    f"admin must see both keys, got {admin_ids}"
                )

                member_list = await http.get(
                    f"{supabase_url}/rest/v1/widget_keys",
                    params={"workspace_id": f"eq.{fx.ws}", "select": "id"},
                    headers=member_h,
                )
                assert member_list.status_code == 200, member_list.text
                assert member_list.json() == [], (
                    f"NON-ADMIN member must read 0 keys, got {member_list.json()}"
                )
                outsider_list = await http.get(
                    f"{supabase_url}/rest/v1/widget_keys",
                    params={"workspace_id": f"eq.{fx.ws}", "select": "id"},
                    headers=outsider_h,
                )
                assert outsider_list.json() == [], "non-member must read 0 keys"
                total += 1
                print("  step 2a: admin reads both keys; member + outsider read 0 (real zero)")

                # A member CANNOT insert a key (RLS WITH CHECK rejects).
                member_insert = await http.post(
                    f"{supabase_url}/rest/v1/widget_keys",
                    headers=member_h,
                    json={
                        "workspace_id": fx.ws,
                        "public_key": generate_public_key(),
                        "created_by": fx.um,
                    },
                )
                assert member_insert.status_code in (401, 403), (
                    f"member insert must be RLS-rejected, got {member_insert.status_code}"
                )
                # A member CANNOT revoke K: RLS USING hides the row, so the PATCH
                # matches and updates 0 rows (returns []), and K stays active.
                member_revoke = await http.patch(
                    f"{supabase_url}/rest/v1/widget_keys",
                    params={"id": f"eq.{fx.k_active}", "revoked_at": "is.null"},
                    headers=member_h,
                    json={"revoked_at": "2026-01-01T00:00:00+00:00"},
                )
                assert member_revoke.json() == [], (
                    f"member revoke must update 0 rows (RLS-hidden), got {member_revoke.json()}"
                )
                still_active = await conn.fetchval(
                    "select revoked_at is null from public.widget_keys where id=$1::uuid",
                    fx.k_active,
                )
                assert still_active, "member revoke must NOT take effect (K still active)"
                total += 1
                print("  step 2b: member cannot insert (RLS 4xx) nor revoke K (0 effect)")

                # An admin CAN insert, then CAN revoke that key (the write path).
                admin_insert = await http.post(
                    f"{supabase_url}/rest/v1/widget_keys",
                    headers=admin_h,
                    json={
                        "workspace_id": fx.ws,
                        "public_key": generate_public_key(),
                        "label": "admin-issued",
                        "created_by": fx.ua,
                    },
                )
                assert admin_insert.status_code in (200, 201), (
                    f"admin insert must succeed, got {admin_insert.status_code}: {admin_insert.text}"
                )
                new_id = admin_insert.json()[0]["id"]
                admin_revoke = await http.patch(
                    f"{supabase_url}/rest/v1/widget_keys",
                    params={"id": f"eq.{new_id}", "revoked_at": "is.null"},
                    headers=admin_h,
                    json={"revoked_at": "2026-06-25T00:00:00+00:00"},
                )
                assert admin_revoke.status_code == 200 and admin_revoke.json(), (
                    f"admin revoke must update the key, got {admin_revoke.status_code}"
                )
                total += 1
                print("  step 2c: admin can insert and revoke (write path works)")

            # --- Step 3: revoking Kr never terminates the live conversation C ---
            # Kr was revoked in _seed AFTER C was created under it. C must survive,
            # and its opaque token Tc must still resume (US-071) — the token is
            # independent of the key once minted.
            conv_alive = await conn.fetchval(
                "select status from public.conversations where id=$1::uuid", fx.conv
            )
            assert conv_alive == "active", (
                f"live conversation C must survive key revocation, status={conv_alive}"
            )
            resumed = await conn.fetch(
                "select id from public.resume_conversation($1)",
                hash_conversation_token(fx.tc),
            )
            assert len(resumed) == 1 and str(resumed[0]["id"]) == fx.conv, (
                f"Tc must still resume C after Kr revoked, got {resumed}"
            )
            total += 1
            print("  step 3: revoking Kr leaves C live and Tc still resumes (key-independent)")

            # --- Step 4: revocation is a one-way DB-enforced latch ---
            # "Never un-revoked" is pinned by the BEFORE-UPDATE trigger, not just
            # avoided by the endpoint. Even the postgres superuser (asyncpg, which
            # bypasses RLS) cannot clear Kr's revoked_at back to NULL nor move it to
            # a different timestamp — the trigger rejects any mutation of an
            # already-set revoked_at, so the audit record "Kr is revoked" is
            # terminal. A direct PostgREST PATCH behind the admin policy is rejected
            # the same way (the trigger fires for every writer).
            for stmt, label in (
                (
                    "update public.widget_keys set revoked_at = null where id = $1::uuid",
                    "clear to NULL (un-revoke)",
                ),
                (
                    "update public.widget_keys set revoked_at = now() + interval '1 day' "
                    "where id = $1::uuid",
                    "move to a different timestamp",
                ),
            ):
                latch_held = False
                try:
                    await conn.execute(stmt, fx.k_revoked)
                except asyncpg.PostgresError:
                    latch_held = True
                assert latch_held, (
                    f"revoke latch must reject {label} on an already-revoked key"
                )
            still_revoked = await conn.fetchval(
                "select revoked_at is not null from public.widget_keys where id=$1::uuid",
                fx.k_revoked,
            )
            assert still_revoked, "Kr must stay revoked after the rejected un-revoke attempts"
            total += 1
            print("  step 4: revoke is a one-way DB latch (un-revoke + re-time rejected; Kr stays revoked)")

        finally:
            await _cleanup(conn, fx)

        print(
            f"OK: US-072 integration passed — {total} exact assertions; resolution "
            "gates on not-revoked (revoked key mints nothing), widget_keys are "
            "admin-only (member/outsider read+write 0), and revoking a key never "
            "kills a live conversation"
        )
        return total
    finally:
        await conn.close()


async def _run() -> None:
    unit = _run_unit()
    print(f"  ({unit} unit checks passed)")
    await _run_integration()


def main_entry() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main_entry()
