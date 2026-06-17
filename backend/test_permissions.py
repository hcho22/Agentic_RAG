"""US-038 integration test for backend/permissions.py.

Exercises the grant → list → revoke loop against a real local Supabase via
the same HTTP path the API endpoints will use (US-039). Uses asyncpg only to
seed the test fixture (users / doc / chunks); the operations under test all
go through PostgREST under a user JWT so RLS is exercised end-to-end.

Run:
    python -m backend.test_permissions

Requires:
    - Local Supabase running (default URL http://127.0.0.1:54321).
    - DATABASE_URL or PERMISSIONS_TEST_DATABASE_URL pointing at the local DB.
    - SUPABASE_ANON_KEY in env (matches supabase status); falls back to the
      well-known local default if unset.
    - SUPABASE_JWT_SECRET in env; falls back to the well-known local default.

Skips cleanly when any required input is missing.
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

# Allow `python -m backend.test_permissions` from the repo root.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from permissions import (  # noqa: E402
    grant_doc_to_principal,
    list_doc_shares,
    revoke_doc_from_principal,
    snapshot_doc_acls,
)

LOCAL_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6ImFub24iLCJleHAiOjE5ODM4MTI5OTZ9."
    "CRXP1A7WOeoJeXxjNni43kdQwgnWNReilDMblYTn_I0"
)
LOCAL_JWT_SECRET = "super-secret-jwt-token-with-at-least-32-characters-long"


def _env(key: str, default: str | None = None) -> str | None:
    val = os.environ.get(key)
    return val if val else default


def _mint_user_jwt(user_id: str, email: str, secret: str) -> str:
    """Mint a Supabase-style HS256 JWT for the test user."""
    now = int(time.time())
    payload = {
        "iss": "supabase-demo",
        "sub": user_id,
        "email": email,
        "role": "authenticated",
        "aud": "authenticated",
        "iat": now,
        "exp": now + 3600,
    }
    return pyjwt.encode(payload, secret, algorithm="HS256")


def _user_headers(jwt_token: str, anon_key: str) -> dict[str, str]:
    return {
        "apikey": anon_key,
        "Authorization": f"Bearer {jwt_token}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


async def _seed_fixture(
    conn: asyncpg.Connection,
    n_chunks: int,
) -> tuple[str, str, str]:
    """Insert alice, bob, an alice-owned doc, and N chunks. Returns IDs."""
    alice_id = str(uuid.uuid4())
    bob_id = str(uuid.uuid4())
    doc_id = str(uuid.uuid4())
    instance = "00000000-0000-0000-0000-000000000000"

    await conn.execute(
        """
        insert into auth.users
          (id, instance_id, email, encrypted_password, aud, role,
           raw_app_meta_data, raw_user_meta_data,
           created_at, updated_at, email_confirmed_at)
        values
          ($1, $2, $3, '', 'authenticated', 'authenticated',
           '{}'::jsonb, '{}'::jsonb, now(), now(), now()),
          ($4, $2, $5, '', 'authenticated', 'authenticated',
           '{}'::jsonb, '{}'::jsonb, now(), now(), now())
        """,
        alice_id, instance, f"alice-{alice_id[:8]}@test.local",
        bob_id, f"bob-{bob_id[:8]}@test.local",
    )

    # Workspace-membership mirroring (ADR-0002): alice/bob are created AFTER the
    # Default-Workspace backfill migration ran, so they are not members yet.
    # Without this, the US-005 documents/chunks SELECT RLS (membership AND-ed
    # under owner-OR-ACL) hides alice's own chunks from grant_doc_to_principal —
    # the per-chunk fan-out reads back zero chunks and inserts nothing. Mirrors
    # the fixture in test_share_api.py. (Cascades away on the auth.users delete
    # in _cleanup_fixture via workspace_membership's ON DELETE CASCADE FK.)
    await conn.execute(
        """
        insert into public.workspace_membership (workspace_id, user_id, role)
        values
          ('00000000-0000-0000-0000-0000000000d0', $1, 'member'),
          ('00000000-0000-0000-0000-0000000000d0', $2, 'member')
        on conflict do nothing
        """,
        alice_id, bob_id,
    )

    await conn.execute(
        """
        insert into public.documents
          (id, user_id, filename, storage_path, byte_size, content_type, status)
        values ($1, $2, 'permissions-test.txt', $3, 100, 'text/plain', 'ready')
        """,
        doc_id, alice_id, f"{alice_id}/permissions-test.txt",
    )

    # Embedding can be a constant — these tests exercise ACL plumbing only.
    embedding_literal = "[" + ",".join(["0.01"] * 1536) + "]"
    rows = [
        (str(uuid.uuid4()), doc_id, alice_id, i, f"chunk {i}", embedding_literal)
        for i in range(n_chunks)
    ]
    await conn.executemany(
        """
        insert into public.chunks
          (id, document_id, user_id, chunk_index, content, embedding)
        values ($1, $2, $3, $4, $5, $6::extensions.vector)
        """,
        rows,
    )

    return alice_id, bob_id, doc_id


async def _cleanup_fixture(
    conn: asyncpg.Connection, alice_id: str, bob_id: str
) -> None:
    # Cascades drop the doc, chunks, and chunk_acl rows.
    await conn.execute(
        "delete from auth.users where id = any($1::uuid[])",
        [alice_id, bob_id],
    )


async def _run() -> None:
    db_url = _env("PERMISSIONS_TEST_DATABASE_URL") or _env("DATABASE_URL")
    if not db_url:
        print("SKIP: PERMISSIONS_TEST_DATABASE_URL/DATABASE_URL unset")
        return

    supabase_url = _env("SUPABASE_URL", "http://127.0.0.1:54321")
    anon_key = _env("SUPABASE_ANON_KEY", LOCAL_ANON_KEY)
    jwt_secret = _env("SUPABASE_JWT_SECRET", LOCAL_JWT_SECRET)
    assert supabase_url and anon_key and jwt_secret  # for type checker

    n_chunks = 7  # arbitrary > 1 to make idempotency claims meaningful
    conn = await asyncpg.connect(db_url)
    alice_id = bob_id = doc_id = None
    try:
        alice_id, bob_id, doc_id = await _seed_fixture(conn, n_chunks)
        alice_email = await conn.fetchval(
            "select email from auth.users where id = $1::uuid", alice_id
        )
        bob_email = await conn.fetchval(
            "select email from auth.users where id = $1::uuid", bob_id
        )

        alice_jwt = _mint_user_jwt(alice_id, alice_email, jwt_secret)
        alice_headers = _user_headers(alice_jwt, anon_key)

        async with httpx.AsyncClient(timeout=10.0) as http:
            # 1. First grant: returns N (one row per chunk).
            inserted = await grant_doc_to_principal(
                http, supabase_url, alice_headers, doc_id,
                "user", bob_id, granted_by=alice_id,
            )
            assert inserted == n_chunks, (
                f"first grant should insert {n_chunks} rows, got {inserted}"
            )
            count = await conn.fetchval(
                "select count(*) from public.chunk_acl "
                "where principal_type = 'user' and principal_id = $1::uuid",
                bob_id,
            )
            assert count == n_chunks, (
                f"chunk_acl should have {n_chunks} rows for bob, got {count}"
            )

            # 2. Re-grant: idempotent, zero new rows, count unchanged.
            inserted = await grant_doc_to_principal(
                http, supabase_url, alice_headers, doc_id,
                "user", bob_id, granted_by=alice_id,
            )
            assert inserted == 0, (
                f"second grant should be a no-op, got {inserted} new rows"
            )
            count = await conn.fetchval(
                "select count(*) from public.chunk_acl "
                "where principal_type = 'user' and principal_id = $1::uuid",
                bob_id,
            )
            assert count == n_chunks, (
                f"chunk_acl row count should still be {n_chunks}, got {count}"
            )

            # 3. list_doc_shares: one entry for bob, display_name = bob's email.
            shares = await list_doc_shares(
                http, supabase_url, alice_headers, doc_id
            )
            assert len(shares) == 1, f"expected 1 share, got {len(shares)}"
            share = shares[0]
            assert share.principal_type == "user", share
            assert share.principal_id == bob_id, share
            assert share.display_name == bob_email, (
                f"display_name should be bob's email {bob_email!r}, "
                f"got {share.display_name!r}"
            )

            # 4. snapshot_doc_acls (used by re-ingestion hook): one principal.
            snap = await snapshot_doc_acls(
                http, supabase_url, alice_headers, doc_id
            )
            assert len(snap) == 1, f"snapshot should dedupe to 1 grant, got {len(snap)}"
            assert snap[0].principal_id == bob_id, snap

            # 5. Revoke: removes all N rows for bob.
            removed = await revoke_doc_from_principal(
                http, supabase_url, alice_headers, doc_id,
                "user", bob_id,
            )
            assert removed == n_chunks, (
                f"revoke should remove {n_chunks} rows, got {removed}"
            )
            count = await conn.fetchval(
                "select count(*) from public.chunk_acl "
                "where principal_type = 'user' and principal_id = $1::uuid",
                bob_id,
            )
            assert count == 0, f"chunk_acl rows for bob should be gone, got {count}"

            # 6. list_doc_shares after revoke: empty.
            shares = await list_doc_shares(
                http, supabase_url, alice_headers, doc_id
            )
            assert shares == [], f"shares should be empty after revoke, got {shares}"

        print(
            f"OK: grant→list→revoke loop verified ({n_chunks} chunks, "
            f"idempotency + display-name resolution + snapshot dedup)"
        )
    finally:
        if alice_id and bob_id:
            await _cleanup_fixture(conn, alice_id, bob_id)
        await conn.close()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
