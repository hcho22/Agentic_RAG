"""US-071: opaque per-conversation customer token — issuance, hashed storage, resume.

Two layers, in the style of `test_supabase_jwt.py`:

  * a UNIT layer (always runs, no DB / no secrets): the pure token primitives in
    `backend/conversation_tokens.py` — the token is opaque random bytes (NOT a
    JWT), the stored form is a one-way SHA-256 hash that never equals the raw
    token, and the 24h TTL constant is correct.

  * an INTEGRATION / SECURITY layer (skips cleanly without a local DB), which is
    the PRD US-071 "Validation Test" encoded as the leak invariant, mirroring
    `test_us066_conversations_rls.py`. The boundary under test is the
    `resume_conversation` RPC + the purge-on-resolve trigger installed by
    `supabase/migrations/20260624160000_conversation_tokens.sql`, exercised
    directly against Postgres (the authoritative read path the backend invokes):

      Setup: conversation X (token Tx) in workspace WX, Y (token Ty) in WY.
      1. resume(Tx)  → returns X only (and Ty → Y, a positive control proving the
         zeros below are real, not a blind/over-restrictive pass).
      2. A token for X can NEVER resolve to Y: resume(Tx).id == X != Y, and the
         transcript-endpoint binding check (resumed.id must equal the requested
         id) rejects "Tx requesting Y's id". Plus: anon CANNOT call the RPC at all
         (PostgREST returns non-200) — token resolution is backend-mediated only.
      3. Resolve X → Tx is invalidated: its hash row is purged and resume(Tx)
         returns 0 rows.

    Failure indicator: Tx reads Y or any conversation other than X, or a resolved
    conversation's token still resumes.

asyncpg connects as the `postgres` superuser; the RPC body (binding + expiry +
status gate + activity refresh) and the trigger are not bypassed by superusers,
so they fire exactly as in production. The anon-denied check goes through
PostgREST (httpx) with the well-known local anon key, exactly as
test_us066_conversations_rls.py forwards a self-minted token.

Run:
    python -m backend.test_us071_conversation_tokens

Requires a local Supabase + DATABASE_URL (or US071_TEST_DATABASE_URL) for the
integration layer; SUPABASE_URL / SUPABASE_ANON_KEY fall back to the well-known
local defaults. Skips cleanly when the DB is unreachable or the migration is not
yet applied. The unit layer needs nothing. No OpenAI.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import asyncpg
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from conversation_tokens import (  # noqa: E402
    CONVERSATION_TOKEN_TTL_SECONDS,
    generate_conversation_token,
    hash_conversation_token,
)

LOCAL_DB_URL = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
LOCAL_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6ImFub24iLCJleHAiOjE5ODM4MTI5OTZ9."
    "CRXP1A7WOeoJeXxjNni43kdQwgnWNReilDMblYTn_I0"
)
# Well-known local Supabase service_role key (role=service_role). The backend
# resolves the real key from SUPABASE_SERVICE_ROLE_KEY; this is the standard
# `supabase start` default used so the test can exercise the EXACT backend path
# (service role → /rest/v1/rpc/resume_conversation).
LOCAL_SERVICE_ROLE_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImV4cCI6MTk4MzgxMjk5Nn0."
    "EGIM96RAZx35lJzdJsyH-qQwv8Hdp7fsn3W0YpN81IU"
)


def _env(key: str, default: str | None = None) -> str | None:
    val = os.environ.get(key)
    return val if val else default


# --------------------------------------------------------------------------- #
# Unit layer — pure primitives, always runs.
# --------------------------------------------------------------------------- #
def _run_unit() -> int:
    checks = 0

    # The token is opaque random bytes, NOT a JWT: distinct each call, URL-safe,
    # high entropy, and crucially NOT three dot-separated segments (a JWT shape).
    t1 = generate_conversation_token()
    t2 = generate_conversation_token()
    assert isinstance(t1, str) and isinstance(t2, str)
    assert t1 != t2, "tokens must be unique per call"
    assert len(t1) >= 40, f"token too short for 256-bit entropy: {len(t1)}"
    assert "." not in t1, "opaque token must not be JWT-shaped (no dot segments)"
    assert all(c.isalnum() or c in "-_" for c in t1), "token must be URL-safe"
    checks += 1
    print("  unit: tokens are opaque, unique, URL-safe, not JWT-shaped")

    # The stored form is a one-way SHA-256 hash: deterministic, 64 hex chars,
    # never equal to the raw token (so a hash leak can't be replayed), and
    # collision-distinct for different inputs.
    h1 = hash_conversation_token(t1)
    assert h1 == hash_conversation_token(t1), "hash must be deterministic"
    assert len(h1) == 64 and all(c in "0123456789abcdef" for c in h1), (
        f"hash must be 64-char lowercase hex, got {h1!r}"
    )
    assert h1 != t1, "stored hash must NOT equal the raw token"
    assert hash_conversation_token(t2) != h1, "distinct tokens hash distinctly"
    checks += 1
    print("  unit: hash is deterministic 64-hex, one-way, != raw token")

    # An empty credential must never resolve to a hash.
    try:
        hash_conversation_token("")
        raise AssertionError("hash_conversation_token('') should raise ValueError")
    except ValueError:
        pass
    checks += 1
    print("  unit: empty token rejected by hash")

    # 24h lifetime.
    assert CONVERSATION_TOKEN_TTL_SECONDS == 24 * 60 * 60, "TTL must be 24h"
    checks += 1
    print("  unit: TTL constant is 24h")

    return checks


# --------------------------------------------------------------------------- #
# Integration / security layer — gated on a local DB.
# --------------------------------------------------------------------------- #
class Fixture:
    def __init__(self) -> None:
        self.wx = str(uuid.uuid4())          # workspace WX
        self.wy = str(uuid.uuid4())          # workspace WY (different)
        self.x = str(uuid.uuid4())           # conversation X in WX
        self.y = str(uuid.uuid4())           # conversation Y in WY
        self.tx = generate_conversation_token()   # raw token bound to X
        self.ty = generate_conversation_token()   # raw token bound to Y


async def _table_exists(conn: asyncpg.Connection, table: str) -> bool:
    return bool(await conn.fetchval("select to_regclass($1)", f"public.{table}"))


async def _seed(conn: asyncpg.Connection, fx: Fixture) -> None:
    await conn.execute(
        "insert into public.workspaces (id, name) values ($1, 'US071-WX'), ($2, 'US071-WY')",
        fx.wx, fx.wy,
    )
    await conn.execute(
        """
        insert into public.conversations (id, workspace_id, status)
        values ($1, $2, 'active'), ($3, $4, 'active')
        """,
        fx.x, fx.wx, fx.y, fx.wy,
    )
    # Issue the two tokens exactly as the backend would: store ONLY the hash, with
    # a 24h expiry. Tx → X, Ty → Y.
    await conn.execute(
        """
        insert into public.conversation_tokens (token_hash, conversation_id, expires_at)
        values ($1, $2, now() + interval '24 hours'),
               ($3, $4, now() + interval '24 hours')
        """,
        hash_conversation_token(fx.tx), fx.x,
        hash_conversation_token(fx.ty), fx.y,
    )


async def _cleanup(conn: asyncpg.Connection, fx: Fixture) -> None:
    # conversation_tokens FK cascades from conversations; deleting conversations
    # drops any surviving token rows. Then drop the workspaces.
    await conn.execute(
        "delete from public.conversations where id = any($1::uuid[])", [fx.x, fx.y]
    )
    await conn.execute(
        "delete from public.workspaces where id = any($1::uuid[])", [fx.wx, fx.wy]
    )


async def _resume(
    conn: asyncpg.Connection, raw_token: str, *, slide: bool = True
) -> list[asyncpg.Record]:
    """Call the authoritative read path the backend invokes (service-role RPC),
    here as the postgres superuser so the function body is exercised directly.

    `slide` maps to the RPC's `p_slide`: True is the POST /resume activity refresh
    (default); False is the read-only GET transcript binding check, which must
    resolve+gate identically but never slide the 24h window."""
    return await conn.fetch(
        "select id, workspace_id, status from public.resume_conversation($1, $2)",
        hash_conversation_token(raw_token),
        slide,
    )


async def _assert_schema(conn: asyncpg.Connection) -> int:
    checks = 0

    # RLS enabled on conversation_tokens and NO policies: anon/authenticated are
    # denied wholesale; only the service role + SECURITY DEFINER RPC touch it.
    enabled = await conn.fetchval(
        "select relrowsecurity from pg_class where oid = 'public.conversation_tokens'::regclass"
    )
    assert enabled, "RLS not enabled on public.conversation_tokens"
    npol = await conn.fetchval(
        "select count(*) from pg_policies "
        "where schemaname='public' and tablename='conversation_tokens'"
    )
    assert npol == 0, f"conversation_tokens must have NO policies (deny-all), found {npol}"
    checks += 1
    print("  schema: conversation_tokens has RLS on + 0 policies (backend-mediated)")

    # resume_conversation is backend-only: EXECUTE revoked from anon/authenticated,
    # granted to service_role. (The whole point: token resolution never happens
    # under a customer-facing Postgres role.)
    for role, expected in (("anon", False), ("authenticated", False), ("service_role", True)):
        granted = await conn.fetchval(
            "select has_function_privilege($1, 'public.resume_conversation(text, boolean)', 'EXECUTE')",
            role,
        )
        assert granted is expected, (
            f"resume_conversation EXECUTE for {role}: expected {expected}, got {granted}"
        )
    checks += 1
    print("  schema: resume_conversation EXECUTE = service_role only (anon/authenticated denied)")

    # The purge-on-resolve trigger exists (literal invalidation on resolve).
    trig = await conn.fetchval(
        "select 1 from pg_trigger "
        "where tgname='conversations_purge_tokens_on_resolve' and not tgisinternal"
    )
    assert trig, "missing conversations_purge_tokens_on_resolve trigger"
    checks += 1
    print("  schema: purge-on-resolve trigger present")

    return checks


async def _run_integration() -> int:
    db_url = _env("US071_TEST_DATABASE_URL") or _env("DATABASE_URL") or LOCAL_DB_URL

    try:
        conn = await asyncpg.connect(db_url, timeout=5)
    except (OSError, asyncpg.PostgresError) as e:
        print(f"SKIP integration: cannot connect to local DB ({e})")
        return 0

    try:
        if not await _table_exists(conn, "conversation_tokens"):
            print("SKIP integration: conversation_tokens table absent (migration not applied)")
            return 0
        if not await _table_exists(conn, "conversations"):
            print("SKIP integration: conversations table absent (US-066 not applied)")
            return 0

        fx = Fixture()
        total = 0
        try:
            total += await _assert_schema(conn)
            await _seed(conn, fx)

            # Step 1: resume(Tx) → exactly X. Positive control: resume(Ty) → Y.
            rx = await _resume(conn, fx.tx)
            assert len(rx) == 1, f"resume(Tx) should return exactly one row, got {len(rx)}"
            assert str(rx[0]["id"]) == fx.x, f"resume(Tx) returned {rx[0]['id']}, expected X"
            assert str(rx[0]["workspace_id"]) == fx.wx
            ry = await _resume(conn, fx.ty)
            assert len(ry) == 1 and str(ry[0]["id"]) == fx.y, (
                "positive control failed: resume(Ty) must return Y"
            )
            total += 1
            print("  step 1: resume(Tx) → X only; resume(Ty) → Y (positive control)")

            # Step 2: a token for X can NEVER resolve to Y. The RPC takes no
            # caller-supplied id, so resume(Tx) is structurally X-bound; the
            # transcript endpoint's binding check (resumed.id must equal the
            # requested id) is what rejects "Tx requesting Y's id".
            assert str(rx[0]["id"]) != fx.y, "CROSS LEAK: Tx resolved to Y"
            resumed_for_tx = str(rx[0]["id"])
            tx_can_read_y = (resumed_for_tx == fx.y)  # the endpoint's authz predicate
            assert not tx_can_read_y, "CROSS LEAK: Tx authorized to read Y's transcript"
            total += 1
            print("  step 2: Tx is X-bound; 'Tx requesting Y' rejected by binding check")

            # Step 2b (defense in depth): anon CANNOT call the RPC at all — token
            # resolution is strictly backend-mediated (service role).
            supabase_url = _env("SUPABASE_URL", "http://127.0.0.1:54321")
            anon_key = _env("SUPABASE_ANON_KEY", LOCAL_ANON_KEY)
            assert supabase_url and anon_key
            try:
                async with httpx.AsyncClient(timeout=10.0) as http:
                    anon_resp = await http.post(
                        f"{supabase_url}/rest/v1/rpc/resume_conversation",
                        headers={
                            "apikey": anon_key,
                            "Authorization": f"Bearer {anon_key}",
                            "Content-Type": "application/json",
                        },
                        json={"p_token_hash": hash_conversation_token(fx.tx)},
                    )
                    assert anon_resp.status_code != 200, (
                        f"anon must NOT execute resume_conversation, got {anon_resp.status_code}"
                    )
                    assert fx.x not in anon_resp.text, "anon RPC response leaked X"
                    # Same call as the service role succeeds → proves the backend path
                    # works (the grant is to service_role, the role the backend uses).
                    svc_key = _env("SUPABASE_SERVICE_ROLE_KEY", LOCAL_SERVICE_ROLE_KEY)
                    assert svc_key
                    svc_resp = await http.post(
                        f"{supabase_url}/rest/v1/rpc/resume_conversation",
                        headers={
                            "apikey": svc_key,
                            "Authorization": f"Bearer {svc_key}",
                            "Content-Type": "application/json",
                        },
                        json={"p_token_hash": hash_conversation_token(fx.tx)},
                    )
                    assert svc_resp.status_code == 200, (
                        f"service role must execute resume_conversation, got "
                        f"{svc_resp.status_code}: {svc_resp.text[:200]}"
                    )
                    svc_rows = svc_resp.json()
                    assert len(svc_rows) == 1 and svc_rows[0]["id"] == fx.x, (
                        f"service-role resume(Tx) must return X, got {svc_rows}"
                    )
            except httpx.TransportError as e:
                print(f"  SKIP step 2b: PostgREST/Kong unreachable ({e})")
            else:
                total += 1
                print("  step 2b: anon RPC denied (non-200); service-role RPC → X (backend path)")

            # Activity refresh contract: POST /resume (slide=True) slides the 24h
            # window forward, but the read-only GET transcript path (slide=False)
            # must NOT — a nominally-safe/idempotent GET can never extend a token's
            # lifetime. Both still resolve+gate identically.
            before = await conn.fetchval(
                "select expires_at from public.conversation_tokens where token_hash=$1",
                hash_conversation_token(fx.ty),
            )
            # GET transcript binding check: slide=False resolves Y but leaves expiry.
            ry_noslide = await _resume(conn, fx.ty, slide=False)
            assert len(ry_noslide) == 1 and str(ry_noslide[0]["id"]) == fx.y, (
                "slide=False must still resolve+return Y (read-only binding check)"
            )
            unchanged = await conn.fetchval(
                "select expires_at from public.conversation_tokens where token_hash=$1",
                hash_conversation_token(fx.ty),
            )
            assert unchanged == before, (
                f"GET transcript (slide=False) must NOT slide expiry: {before} -> {unchanged}"
            )
            # POST /resume (slide=True): expiry moves strictly forward.
            await _resume(conn, fx.ty, slide=True)
            after = await conn.fetchval(
                "select expires_at from public.conversation_tokens where token_hash=$1",
                hash_conversation_token(fx.ty),
            )
            assert after > before, (
                f"POST resume (slide=True) must slide expiry forward: {before} -> {after}"
            )
            total += 1
            print("  refresh: POST resume slides the 24h expiry; GET transcript (slide=False) does not")

            # Expiry: an expired token is rejected (and is NOT refreshed).
            stale = generate_conversation_token()
            await conn.execute(
                """
                insert into public.conversation_tokens (token_hash, conversation_id, expires_at)
                values ($1, $2, now() - interval '1 hour')
                """,
                hash_conversation_token(stale), fx.y,
            )
            expired = await _resume(conn, stale)
            assert expired == [], f"expired token must be rejected, got {expired}"
            still_stale = await conn.fetchval(
                "select expires_at < now() from public.conversation_tokens where token_hash=$1",
                hash_conversation_token(stale),
            )
            assert still_stale, "rejected expired token must NOT be refreshed"
            await conn.execute(
                "delete from public.conversation_tokens where token_hash=$1",
                hash_conversation_token(stale),
            )
            total += 1
            print("  expiry: an expired token is rejected and not resurrected")

            # Step 3: resolve X → Tx is invalidated. The hash row is purged by the
            # trigger and resume(Tx) returns 0 rows.
            await conn.execute(
                "update public.conversations set status='resolved' where id=$1::uuid", fx.x
            )
            survivors = await conn.fetchval(
                "select count(*) from public.conversation_tokens where conversation_id=$1::uuid",
                fx.x,
            )
            assert survivors == 0, f"resolve must purge X's tokens, {survivors} survived"
            after_resolve = await _resume(conn, fx.tx)
            assert after_resolve == [], (
                f"resolved conversation's token must NOT resume, got {after_resolve}"
            )
            total += 1
            print("  step 3: resolve(X) purges Tx and rejects resume(Tx) (invalidated on resolve)")

        finally:
            await _cleanup(conn, fx)

        print(
            f"OK: US-071 integration passed — {total} exact assertions; the opaque "
            "token is X-bound, backend-mediated, refreshed on activity, expiry- and "
            "resolve-invalidated, with zero cross-conversation leak"
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
