"""US-066: cross-workspace zero-leak RLS test for the `conversations` pair.

Proves the support-conversation surface is gated by the SAME ADR-0002
workspace-membership boundary as the retrieval path — a member of one workspace
can read its conversations and **zero** rows of another workspace's, with the
direct-by-id read RLS-hidden (indistinguishable from not-found). This is the
US-066 "Validation Test" encoded as the leak invariant, in the style of
test_au4_auth_attacks.py / test_permissions.py: every leak assertion is exact
(`== 0`), and a same-workspace positive control proves a 0 is a *real* zero and
not a false pass from an empty/over-restrictive policy.

It also asserts the structural failure indicator directly: NO `conversations`
policy references `wm.role` (membership PRESENCE only — role is administrative
and must never enter a visibility predicate, ADR-0002), and that
`conversation_messages` delegates to its parent conversation's membership.

The user JWT is self-minted with the local project JWT secret and forwarded to
PostgREST verbatim (local GoTrue rejects self-minted tokens), exactly as
test_permissions.py does — PostgREST + RLS still resolve `auth.uid()` from the
token's `sub`, so this exercises the real RLS path end-to-end.

Run:
    python -m backend.test_us066_conversations_rls

Requires a local Supabase running and DATABASE_URL (or
PERMISSIONS_TEST_DATABASE_URL) pointing at its DB; SUPABASE_URL /
SUPABASE_ANON_KEY / SUPABASE_JWT_SECRET fall back to the well-known local
defaults. Skips cleanly when DATABASE_URL is unset. Needs no OpenAI.
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
    }


class Fixture:
    def __init__(self) -> None:
        self.ws1 = str(uuid.uuid4())          # W1
        self.ws2 = str(uuid.uuid4())          # W2
        self.u1 = str(uuid.uuid4())           # member of W1 only
        self.u2 = str(uuid.uuid4())           # member of W2 (positive control)
        self.c1 = str(uuid.uuid4())           # conversation in W1
        self.c2 = str(uuid.uuid4())           # conversation in W2
        self.m1 = str(uuid.uuid4())           # message in C1
        self.m2 = str(uuid.uuid4())           # message in C2


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
        fx.u1, fx.u2, INSTANCE,
        f"u1-{fx.u1[:8]}@test.local", f"u2-{fx.u2[:8]}@test.local",
    )
    await conn.execute(
        "insert into public.workspaces (id, name) values ($1, 'US066-W1'), ($2, 'US066-W2')",
        fx.ws1, fx.ws2,
    )
    # U1 → W1 only; U2 → W2 (so C2 has a legitimate reader: the positive control
    # that makes U1's 0 a real zero, not a structurally blind pass).
    await conn.execute(
        """
        insert into public.workspace_membership (workspace_id, user_id, role)
        values ($1, $2, 'member'), ($3, $4, 'member')
        """,
        fx.ws1, fx.u1, fx.ws2, fx.u2,
    )
    await conn.execute(
        """
        insert into public.conversations (id, workspace_id, status)
        values ($1, $2, 'active'), ($3, $4, 'escalated')
        """,
        fx.c1, fx.ws1, fx.c2, fx.ws2,
    )
    await conn.execute(
        """
        insert into public.conversation_messages (id, conversation_id, role, content)
        values ($1, $2, 'user', 'hello from W1'), ($3, $4, 'user', 'secret from W2')
        """,
        fx.m1, fx.c1, fx.m2, fx.c2,
    )


async def _cleanup(conn: asyncpg.Connection, fx: Fixture) -> None:
    # conversations.workspace_id has no ON DELETE CASCADE from workspaces, so drop
    # conversations first (cascades conversation_messages), then workspaces
    # (cascades membership), then users.
    await conn.execute(
        "delete from public.conversations where id = any($1::uuid[])", [fx.c1, fx.c2]
    )
    await conn.execute(
        "delete from public.workspaces where id = any($1::uuid[])", [fx.ws1, fx.ws2]
    )
    await conn.execute(
        "delete from auth.users where id = any($1::uuid[])", [fx.u1, fx.u2]
    )


async def _get(
    http: httpx.AsyncClient, url: str, headers: dict[str, str], query: str
) -> list[dict]:
    r = await http.get(f"{url}/rest/v1/{query}", headers=headers)
    assert r.status_code == 200, f"{query}: {r.status_code} {r.text}"
    return r.json()


async def _policy_predicates(conn: asyncpg.Connection, table: str) -> list[str]:
    """Return every RLS predicate (USING + WITH CHECK) defined on `table`."""
    rows = await conn.fetch(
        """
        select coalesce(qual, '') as qual, coalesce(with_check, '') as with_check
        from pg_policies
        where schemaname = 'public' and tablename = $1
        """,
        table,
    )
    assert rows, f"expected RLS policies on public.{table}, found none"
    preds: list[str] = []
    for row in rows:
        preds.append(row["qual"])
        preds.append(row["with_check"])
    return preds


async def _assert_schema(conn: asyncpg.Connection) -> int:
    """Structural guards: role-free policies, child delegation, indexes, RLS."""
    checks = 0

    # Failure indicator: NO conversations policy may reference the membership
    # role. The membership clause is presence-only (workspace_id + user_id);
    # 'role' must not appear anywhere in any predicate.
    conv_preds = await _policy_predicates(conn, "conversations")
    for pred in conv_preds:
        assert "role" not in pred.lower(), (
            f"conversations policy references role (ADR-0002 violation): {pred}"
        )
    checks += 1
    print(f"  conversations: {len(conv_preds)} policy predicates, none reference role")

    # conversation_messages must delegate to the parent conversation (mirrors
    # messages -> threads): every predicate joins through `conversations`, and
    # none reference role either.
    msg_preds = await _policy_predicates(conn, "conversation_messages")
    for pred in msg_preds:
        if pred:  # a permissive policy may have an empty with_check
            assert "conversations" in pred, (
                f"conversation_messages policy does not delegate to parent "
                f"conversation: {pred}"
            )
        assert "role" not in pred.lower(), (
            f"conversation_messages policy references role: {pred}"
        )
    checks += 1
    print("  conversation_messages: every policy delegates to parent conversation")

    # RLS actually enabled on both tables (a policy is inert if RLS is off).
    for table in ("conversations", "conversation_messages"):
        enabled = await conn.fetchval(
            "select relrowsecurity from pg_class where oid = $1::regclass",
            f"public.{table}",
        )
        assert enabled, f"RLS not enabled on public.{table}"
    checks += 1
    print("  RLS enabled on both conversations and conversation_messages")

    # Both required indexes exist.
    for idx in (
        "conversations_workspace_id_status_idx",
        "conversation_messages_conversation_id_created_at_idx",
    ):
        exists = await conn.fetchval(
            "select 1 from pg_indexes where schemaname = 'public' and indexname = $1",
            idx,
        )
        assert exists, f"missing required index {idx}"
    checks += 1
    print("  both required indexes present")

    # The leak-proof threads/messages predicate must stay owner-only and never
    # gain a workspace_membership branch (the design constraint US-066 protects).
    thread_preds = await _policy_predicates(conn, "threads")
    for pred in thread_preds:
        assert "workspace_membership" not in pred, (
            f"threads policy was modified to reference workspace_membership: {pred}"
        )
    checks += 1
    print("  threads policies remain owner-only (no workspace_membership branch)")

    return checks


async def _run() -> None:
    db_url = _env("PERMISSIONS_TEST_DATABASE_URL") or _env("DATABASE_URL")
    if not db_url:
        print("SKIP: PERMISSIONS_TEST_DATABASE_URL/DATABASE_URL unset")
        return

    supabase_url = _env("SUPABASE_URL", "http://127.0.0.1:54321")
    anon_key = _env("SUPABASE_ANON_KEY", LOCAL_ANON_KEY)
    jwt_secret = _env("SUPABASE_JWT_SECRET", LOCAL_JWT_SECRET)
    assert supabase_url and anon_key and jwt_secret  # for type checker

    fx = Fixture()
    conn = await asyncpg.connect(db_url)
    total = 0
    try:
        await _seed(conn, fx)
        total += await _assert_schema(conn)

        u1_email = await conn.fetchval(
            "select email from auth.users where id = $1::uuid", fx.u1
        )
        u2_email = await conn.fetchval(
            "select email from auth.users where id = $1::uuid", fx.u2
        )
        u1_headers = _user_headers(_mint_user_jwt(fx.u1, u1_email, jwt_secret), anon_key)
        u2_headers = _user_headers(_mint_user_jwt(fx.u2, u2_email, jwt_secret), anon_key)

        async with httpx.AsyncClient(timeout=10.0) as http:
            # Step 1: U1 lists conversations -> sees C1 (its workspace) and ONLY C1.
            listed = await _get(http, supabase_url, u1_headers, "conversations?select=id")
            ids = {row["id"] for row in listed}
            assert fx.c1 in ids, f"U1 cannot see C1 in its own workspace: {ids}"
            assert fx.c2 not in ids, f"CROSS-WORKSPACE LEAK: U1's list includes C2: {ids}"
            assert ids == {fx.c1}, f"U1's list should be exactly {{C1}}, got {ids}"
            total += 1
            print(f"  U1 list /conversations -> {{C1}} only ({len(ids)} row)")

            # Step 2: U1 reads C2 directly by id -> 0 rows (RLS-hidden, == not-found).
            direct = await _get(
                http, supabase_url, u1_headers, f"conversations?id=eq.{fx.c2}&select=id"
            )
            assert direct == [], f"CROSS-WORKSPACE LEAK: U1 read C2 directly: {direct}"
            total += 1
            print("  U1 direct read of C2 by id -> 0 rows (RLS-hidden)")

            # Child messages delegate: U1 reads C1's message, 0 of C2's.
            m_visible = await _get(
                http, supabase_url, u1_headers,
                f"conversation_messages?conversation_id=eq.{fx.c1}&select=id",
            )
            assert {r["id"] for r in m_visible} == {fx.m1}, (
                f"U1 should see exactly C1's message, got {m_visible}"
            )
            m_leak = await _get(
                http, supabase_url, u1_headers,
                f"conversation_messages?conversation_id=eq.{fx.c2}&select=id",
            )
            assert m_leak == [], f"CROSS-WORKSPACE LEAK: U1 read C2's messages: {m_leak}"
            total += 1
            print("  U1 sees C1's message, 0 of C2's (child delegation holds)")

            # Positive control: U2 (member of W2) CAN read C2 — proves the zeros
            # above are real zeros, not a structurally blind/over-restrictive pass.
            u2_view = await _get(
                http, supabase_url, u2_headers, f"conversations?id=eq.{fx.c2}&select=id"
            )
            assert {r["id"] for r in u2_view} == {fx.c2}, (
                f"positive control failed: W2 member cannot read C2: {u2_view}"
            )
            total += 1
            print("  positive control: U2 (W2 member) reads C2 -> the zeros are real")

        print(
            f"OK: US-066 passed — {total} exact assertions; conversations + "
            "conversation_messages enforce the ADR-0002 membership boundary with "
            "zero cross-workspace leak and no role predicate"
        )
    finally:
        await _cleanup(conn, fx)
        await conn.close()


def main_entry() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main_entry()
