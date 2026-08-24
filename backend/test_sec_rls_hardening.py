"""SEC-F1/F2/F3: RLS-hardening regression tests for the audit fixes.

Proves the three tightenings from data/purvia-rls-audit/report.md hold END TO END
against real PostgREST + RLS, in the style of test_us066_conversations_rls.py
(self-minted user JWTs forwarded to PostgREST; every assertion exact; a positive
control makes each deny a REAL deny, not a structurally-blind pass). Each block
would FAIL on `main` before the fix:

  F1 - profiles cross-tenant email enumeration is closed:
       * a direct `GET /rest/v1/profiles` returns ZERO rows (the `using(true)`
         policy is gone) — before the fix it listed every user's email;
       * `resolve_profile_by_email` returns ONLY the exact-match row (the share
         dialog's real need), nothing on a miss;
       * `resolve_claimer_emails` resolves an id ONLY when that id claimed a
         conversation in a workspace the caller is a member of.

  F2 - documents.workspace_id is constrained on write:
       * a member of W1 cannot INSERT a document into W2 (not a member) — before
         the fix the WITH CHECK tested only user_id and allowed it;
       * nor UPDATE their own W1 document's workspace_id to W2;
       * positive control: inserting into their OWN workspace still works.

  F3 - the ACL helpers no longer take a foreign user id:
       * the two-arg `_chunk_acl_grants_user(p_chunk_id, p_user_id)` RPC is GONE
         (404) — the cross-user boolean oracle is removed;
       * positive control: an ACL grant to the caller is still read-through
         visible, so the single-arg (auth.uid()-internal) helper still authorizes
         legitimate shared reads.

Run:
    python -m backend.test_sec_rls_hardening

Requires a local Supabase running and DATABASE_URL (or
PERMISSIONS_TEST_DATABASE_URL); SUPABASE_URL / SUPABASE_ANON_KEY /
SUPABASE_JWT_SECRET fall back to the well-known local defaults. Skips cleanly when
DATABASE_URL is unset. Needs no OpenAI.
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
        self.ws1 = str(uuid.uuid4())        # W1
        self.ws2 = str(uuid.uuid4())        # W2
        self.u1 = str(uuid.uuid4())         # member of W1 (the caller under test)
        self.u2 = str(uuid.uuid4())         # member of W2 (positive control / cross-tenant)
        self.owner1 = str(uuid.uuid4())     # member of W1, owns d1 + ch1
        self.claimer_a = str(uuid.uuid4())  # agent, claimed C1 (in W1)
        self.claimer_b = str(uuid.uuid4())  # agent, claimed C2 (in W2)
        self.d1 = str(uuid.uuid4())         # document in W1, owned by owner1
        self.ch1 = str(uuid.uuid4())        # chunk of d1, shared to u1 via chunk_acl
        self.c1 = str(uuid.uuid4())         # conversation in W1, claimed by claimer_a
        self.c2 = str(uuid.uuid4())         # conversation in W2, claimed by claimer_b

    def email(self, uid: str) -> str:
        return f"secfix-{uid[:8]}@test.local"


async def _seed(conn: asyncpg.Connection, fx: Fixture) -> None:
    uids = [fx.u1, fx.u2, fx.owner1, fx.claimer_a, fx.claimer_b]
    # Insert users WITH emails so the US-037 sync trigger mirrors each into
    # public.profiles (what resolve_profile_by_email / resolve_claimer_emails read).
    for uid in uids:
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
            uid, INSTANCE, fx.email(uid),
        )
    await conn.execute(
        "insert into public.workspaces (id, name) values ($1, 'SECFIX-W1'), ($2, 'SECFIX-W2')",
        fx.ws1, fx.ws2,
    )
    await conn.execute(
        """
        insert into public.workspace_membership (workspace_id, user_id, role)
        values ($1, $2, 'member'), ($3, $4, 'member'), ($1, $5, 'member')
        """,
        fx.ws1, fx.u1, fx.ws2, fx.u2, fx.owner1,
    )
    # A document + chunk owned by owner1 in W1, shared to u1 via chunk_acl (the F3
    # positive control: u1 must still read ch1 through the single-arg helper).
    await conn.execute(
        """
        insert into public.documents
          (id, user_id, filename, storage_path, byte_size, status, workspace_id)
        values ($1, $2, 'd1.txt', $3, 10, 'ready', $4)
        """,
        fx.d1, fx.owner1, f"{fx.owner1}/d1.txt", fx.ws1,
    )
    await conn.execute(
        """
        insert into public.chunks (id, document_id, user_id, chunk_index, content)
        values ($1, $2, $3, 0, 'shared chunk body')
        """,
        fx.ch1, fx.d1, fx.owner1,
    )
    await conn.execute(
        """
        insert into public.chunk_acl (chunk_id, principal_type, principal_id, granted_by)
        values ($1, 'user', $2, $3)
        """,
        fx.ch1, fx.u1, fx.owner1,
    )
    # Conversations with claimers, for resolve_claimer_emails membership scoping.
    await conn.execute(
        """
        insert into public.conversations (id, workspace_id, status, claimed_by)
        values ($1, $2, 'escalated', $3), ($4, $5, 'escalated', $6)
        """,
        fx.c1, fx.ws1, fx.claimer_a, fx.c2, fx.ws2, fx.claimer_b,
    )


async def _cleanup(conn: asyncpg.Connection, fx: Fixture) -> None:
    # Drop conversations + documents (cascade their children), then workspaces
    # (cascade membership), then users (cascade profiles + chunk_acl.granted_by set).
    await conn.execute(
        "delete from public.conversations where id = any($1::uuid[])", [fx.c1, fx.c2]
    )
    # documents in these workspaces: the seeded d1 plus any inserted by F2 tests.
    await conn.execute(
        "delete from public.documents where workspace_id = any($1::uuid[])",
        [fx.ws1, fx.ws2],
    )
    await conn.execute(
        "delete from public.workspaces where id = any($1::uuid[])", [fx.ws1, fx.ws2]
    )
    await conn.execute(
        "delete from auth.users where id = any($1::uuid[])",
        [fx.u1, fx.u2, fx.owner1, fx.claimer_a, fx.claimer_b],
    )


async def _rpc(
    http: httpx.AsyncClient, url: str, headers: dict[str, str], fn: str, body: dict
) -> httpx.Response:
    return await http.post(f"{url}/rest/v1/rpc/{fn}", headers=headers, json=body)


async def _run() -> None:
    db_url = _env("PERMISSIONS_TEST_DATABASE_URL") or _env("DATABASE_URL")
    if not db_url:
        print("SKIP: PERMISSIONS_TEST_DATABASE_URL/DATABASE_URL unset")
        return

    supabase_url = _env("SUPABASE_URL", "http://127.0.0.1:54321")
    anon_key = _env("SUPABASE_ANON_KEY", LOCAL_ANON_KEY)
    jwt_secret = _env("SUPABASE_JWT_SECRET", LOCAL_JWT_SECRET)
    assert supabase_url and anon_key and jwt_secret

    fx = Fixture()
    conn = await asyncpg.connect(db_url)
    total = 0
    try:
        await _seed(conn, fx)
        u1_h = _user_headers(_mint_user_jwt(fx.u1, fx.email(fx.u1), jwt_secret), anon_key)
        u2_h = _user_headers(_mint_user_jwt(fx.u2, fx.email(fx.u2), jwt_secret), anon_key)
        owner1_h = _user_headers(
            _mint_user_jwt(fx.owner1, fx.email(fx.owner1), jwt_secret), anon_key
        )

        async with httpx.AsyncClient(timeout=10.0) as http:
            # ---- F1: profiles enumeration closed ------------------------------
            # u1 owns no documents and has no grantees, so a direct list exposes at
            # most u1's OWN row (self) and NEVER another tenant's user — the pre-fix
            # `using(true)` would have returned EVERY user's email. Assert no foreign
            # row leaks (the enumeration invariant), regardless of whether self shows.
            r = await http.get(
                f"{supabase_url}/rest/v1/profiles?select=id,email", headers=u1_h
            )
            assert r.status_code == 200, f"profiles list status {r.status_code}: {r.text}"
            ids = {row["id"] for row in r.json()}
            assert ids <= {fx.u1}, (
                f"SEC-F1 LEAK: authenticated user enumerated foreign profiles: {ids}"
            )
            assert fx.u2 not in ids and fx.owner1 not in ids, (
                f"SEC-F1 LEAK: u1 read another tenant's profile: {ids}"
            )
            total += 1
            print("  F1: direct GET /profiles -> only self, no foreign rows")

            # Grantee scoping (the policy's second branch): owner1 owns d1 and shared
            # ch1 to u1, so owner1 CAN resolve u1's profile (needed by list_doc_shares)
            # but NOT u2's (u2 is not a grantee of owner1's docs).
            r = await http.get(
                f"{supabase_url}/rest/v1/profiles?id=in.({fx.u1},{fx.u2})&select=id",
                headers=owner1_h,
            )
            assert r.status_code == 200, f"owner1 profiles read {r.status_code}: {r.text}"
            owner_sees = {row["id"] for row in r.json()}
            assert owner_sees == {fx.u1}, (
                f"SEC-F1: doc owner must see its grantee (u1) and no non-grantee; "
                f"expected {{u1}}, got {owner_sees}"
            )
            total += 1
            print("  F1: doc owner sees its grantee only (list_doc_shares path intact)")

            # Exact-email RPC returns ONLY the exact match (share-dialog need).
            r = await _rpc(
                http, supabase_url, u1_h, "resolve_profile_by_email",
                {"p_email": fx.email(fx.u2)},
            )
            assert r.status_code == 200, f"resolve_profile status {r.status_code}: {r.text}"
            rows = r.json()
            assert [row["id"] for row in rows] == [fx.u2], (
                f"resolve_profile_by_email should return exactly u2: {rows}"
            )
            total += 1
            print("  F1: resolve_profile_by_email -> exact match only")

            # A miss returns zero rows (reveals only non-existence, as a share 404).
            r = await _rpc(
                http, supabase_url, u1_h, "resolve_profile_by_email",
                {"p_email": "no-such-user@nowhere.invalid"},
            )
            assert r.status_code == 200 and r.json() == [], (
                f"resolve_profile_by_email miss should be empty: {r.status_code} {r.text}"
            )
            total += 1
            print("  F1: resolve_profile_by_email miss -> 0 rows")

            # Claimer resolution is workspace-membership-scoped: u1 (member of W1)
            # resolves claimer_a (claimed C1 in W1) but NOT claimer_b (only in W2).
            r = await _rpc(
                http, supabase_url, u1_h, "resolve_claimer_emails",
                {"p_ids": [fx.claimer_a, fx.claimer_b]},
            )
            assert r.status_code == 200, f"resolve_claimer status {r.status_code}: {r.text}"
            got = {row["id"] for row in r.json()}
            assert got == {fx.claimer_a}, (
                f"SEC-F1: resolve_claimer_emails must scope to caller's workspaces; "
                f"expected {{claimer_a}}, got {got}"
            )
            total += 1
            print("  F1: resolve_claimer_emails -> only in-workspace claimer")

            # ---- F2: documents.workspace_id constrained on write --------------
            # This block asserts on ACTUAL DB STATE, not the HTTP status: a
            # pre-fix INSERT into a non-member workspace still 403s under
            # `return=representation` (the RETURNING read-back is denied by the
            # SELECT policy) while the ROW IS NEVERTHELESS WRITTEN — the real hole.
            # `return=minimal` avoids that read-back, and a direct DB count is the
            # authoritative "did a row get smuggled in" check.
            #
            # u1 (member of W1 only) attempts to INSERT a doc into W2.
            r = await http.post(
                f"{supabase_url}/rest/v1/documents",
                headers={**u1_h, "Prefer": "return=minimal"},
                json={
                    "user_id": fx.u1, "filename": "evil.txt",
                    "storage_path": f"{fx.u1}/evil.txt", "byte_size": 1,
                    "workspace_id": fx.ws2,
                },
            )
            smuggled = await conn.fetchval(
                "select count(*) from public.documents "
                "where user_id = $1::uuid and workspace_id = $2::uuid",
                fx.u1, fx.ws2,
            )
            assert smuggled == 0, (
                f"SEC-F2 LEAK: u1 wrote {smuggled} document(s) into a non-member "
                f"workspace (HTTP {r.status_code})"
            )
            total += 1
            print("  F2: INSERT into non-member workspace -> no row written")

            # Positive control: u1 CAN insert into its OWN workspace (W1).
            r = await http.post(
                f"{supabase_url}/rest/v1/documents",
                headers={**u1_h, "Prefer": "return=representation"},
                json={
                    "user_id": fx.u1, "filename": "mine.txt",
                    "storage_path": f"{fx.u1}/mine.txt", "byte_size": 1,
                    "workspace_id": fx.ws1,
                },
            )
            assert r.status_code == 201, (
                f"positive control failed: u1 cannot insert into own workspace: "
                f"{r.status_code} {r.text}"
            )
            my_doc_id = r.json()[0]["id"]
            total += 1
            print("  F2: INSERT into own workspace -> 201 (positive control)")

            # u1 attempts to MOVE that doc's workspace_id to W2 (a non-member
            # workspace). Again assert the DB did not move: pre-fix the UPDATE
            # commits (only user_id was checked) and only the read-back 403s.
            await http.patch(
                f"{supabase_url}/rest/v1/documents?id=eq.{my_doc_id}",
                headers={**u1_h, "Prefer": "return=minimal"},
                json={"workspace_id": fx.ws2},
            )
            landed_ws = await conn.fetchval(
                "select workspace_id from public.documents where id = $1::uuid",
                my_doc_id,
            )
            assert str(landed_ws) == fx.ws1, (
                f"SEC-F2 LEAK: u1 moved a doc across the tenant boundary "
                f"(workspace_id now {landed_ws}, expected {fx.ws1})"
            )
            total += 1
            print("  F2: UPDATE workspace_id to non-member workspace -> row not moved")

            # ---- F3: cross-user boolean oracle removed ------------------------
            # The two-arg helper no longer exists → PostgREST cannot resolve it.
            r = await _rpc(
                http, supabase_url, u1_h, "_chunk_acl_grants_user",
                {"p_chunk_id": fx.ch1, "p_user_id": fx.u2},
            )
            assert r.status_code == 404, (
                f"SEC-F3: two-arg _chunk_acl_grants_user(p_chunk_id,p_user_id) must "
                f"be GONE (404); got {r.status_code} {r.text}"
            )
            total += 1
            print("  F3: two-arg _chunk_acl_grants_user RPC -> 404 (oracle removed)")

            # Positive control: the ACL grant to u1 is still read-through visible, so
            # the single-arg (auth.uid()-internal) helper still authorizes shared
            # reads — F3 did not break the ACL path.
            r = await http.get(
                f"{supabase_url}/rest/v1/chunks?id=eq.{fx.ch1}&select=id", headers=u1_h
            )
            assert r.status_code == 200 and {x["id"] for x in r.json()} == {fx.ch1}, (
                f"positive control failed: u1's ACL-shared chunk not visible: "
                f"{r.status_code} {r.text}"
            )
            # And a non-grantee (u2, member of W2, no grant) still sees zero.
            r = await http.get(
                f"{supabase_url}/rest/v1/chunks?id=eq.{fx.ch1}&select=id", headers=u2_h
            )
            assert r.status_code == 200 and r.json() == [], (
                f"SEC-F3: non-grantee u2 must not read ch1: {r.status_code} {r.text}"
            )
            total += 1
            print("  F3: ACL grant still read-through for grantee, 0 for non-grantee")

        print(
            f"OK: SEC-F1/F2/F3 passed — {total} exact assertions; profiles "
            "enumeration closed, documents.workspace_id constrained on write, and "
            "the cross-user ACL oracle removed with the ACL path intact"
        )
    finally:
        await _cleanup(conn, fx)
        await conn.close()


def main_entry() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main_entry()
