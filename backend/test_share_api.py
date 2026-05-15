"""US-039 integration test for the share API endpoints.

Walks the seven PRD validation steps end-to-end via FastAPI's ASGI transport,
so the auth middleware, ownership check, principal resolver, and the
underlying PostgREST/RLS path all run in their natural shapes — only the
JWT-validation HTTP call to gotrue is short-circuited via a dependency
override (the test mints its own HS256 JWTs against the same JWT_SECRET).

Run:
    python -m backend.test_share_api

Requires the same env as test_permissions.py (DATABASE_URL plus the
backend/.env values for SUPABASE_URL / SUPABASE_ANON_KEY / OPENAI_API_KEY).
The override decodes the JWT's `sub` claim to populate AuthedUser, so RLS
behaviour is identical to the production auth path.
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
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / "backend" / ".env")
sys.path.insert(0, str(ROOT / "backend"))

# Required env may be missing on a fresh checkout — exit gracefully.
_required = ("SUPABASE_URL", "SUPABASE_ANON_KEY", "OPENAI_API_KEY")
_missing = [k for k in _required if not os.environ.get(k)]
if _missing:
    print(f"SKIP: missing env: {', '.join(_missing)}")
    sys.exit(0)

from fastapi import Header, HTTPException  # noqa: E402

import main  # noqa: E402
from main import AuthedUser, app, get_user  # noqa: E402

LOCAL_JWT_SECRET = "super-secret-jwt-token-with-at-least-32-characters-long"
JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET") or LOCAL_JWT_SECRET


def _mint_jwt(user_id: str, email: str) -> str:
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
    return pyjwt.encode(payload, JWT_SECRET, algorithm="HS256")


async def _override_get_user(
    authorization: str | None = Header(default=None),
) -> AuthedUser:
    """Test-only dependency override — decodes the user id from the JWT and
    skips the gotrue HTTP roundtrip. The token itself is forwarded verbatim
    to PostgREST in subsequent calls, so RLS still runs as the test user."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    payload = pyjwt.decode(
        token, JWT_SECRET, algorithms=["HS256"], audience="authenticated"
    )
    return AuthedUser(id=payload["sub"], access_token=token)


async def _seed_fixture(
    conn: asyncpg.Connection, n_chunks: int
) -> tuple[str, str, str, str, str, str]:
    """Seed alice, bob, alice's doc + N chunks, and the 'engineering' group.

    Returns (alice_id, bob_id, doc_id, alice_email, bob_email, group_id).
    """
    alice_id = str(uuid.uuid4())
    bob_id = str(uuid.uuid4())
    doc_id = str(uuid.uuid4())
    group_id = str(uuid.uuid4())
    instance = "00000000-0000-0000-0000-000000000000"
    alice_email = f"alice-{alice_id[:8]}@test.local"
    bob_email = f"bob-{bob_id[:8]}@test.local"
    group_name = f"engineering-{group_id[:8]}"

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
        alice_id, instance, alice_email, bob_id, bob_email,
    )

    await conn.execute(
        """
        insert into public.documents
          (id, user_id, filename, storage_path, byte_size, content_type, status)
        values ($1, $2, 'share-api-test.txt', $3, 100, 'text/plain', 'ready')
        """,
        doc_id, alice_id, f"{alice_id}/share-api-test.txt",
    )

    embedding = "[" + ",".join(["0.01"] * 1536) + "]"
    rows = [
        (str(uuid.uuid4()), doc_id, alice_id, i, f"chunk {i}", embedding)
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

    await conn.execute(
        "insert into public.principals (id, name, kind) values ($1, $2, 'group')",
        group_id, group_name,
    )

    return alice_id, bob_id, doc_id, alice_email, bob_email, group_id, group_name


async def _cleanup(
    conn: asyncpg.Connection, ids: tuple[str, str, str, str]
) -> None:
    alice_id, bob_id, group_id, doc_id = ids
    # Delete the doc first — cascades to chunks → chunk_acl, which clears the
    # granted_by FK references back to auth.users (the FK has no cascade).
    await conn.execute(
        "delete from public.documents where id = $1::uuid", doc_id
    )
    await conn.execute(
        "delete from public.principals where id = $1::uuid", group_id
    )
    await conn.execute(
        "delete from auth.users where id = any($1::uuid[])", [alice_id, bob_id]
    )


def _bearer(jwt_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {jwt_token}"}


async def _run() -> None:
    db_url = (
        os.environ.get("PERMISSIONS_TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
    )
    if not db_url:
        print("SKIP: PERMISSIONS_TEST_DATABASE_URL/DATABASE_URL unset")
        return

    app.dependency_overrides[get_user] = _override_get_user

    conn = await asyncpg.connect(db_url)
    seeded: tuple[str, str, str, str] | None = None
    try:
        (
            alice_id, bob_id, doc_id, alice_email, bob_email, group_id, group_name,
        ) = await _seed_fixture(conn, n_chunks=3)
        seeded = (alice_id, bob_id, group_id, doc_id)

        alice_jwt = _mint_jwt(alice_id, alice_email)
        bob_jwt = _mint_jwt(bob_id, bob_email)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            # Step 1: alice grants bob → 200, bob's UUID returned.
            r = await client.post(
                f"/api/documents/{doc_id}/share",
                headers=_bearer(alice_jwt),
                json={"principal_email_or_name": bob_email},
            )
            assert r.status_code == 200, (r.status_code, r.text)
            body = r.json()
            assert body["principal_id"] == bob_id, body
            assert body["principal_type"] == "user", body
            assert body["display_name"] == bob_email, body
            assert body["granted_at"], body

            # Step 2: alice grants bob again → 200 (idempotent).
            r = await client.post(
                f"/api/documents/{doc_id}/share",
                headers=_bearer(alice_jwt),
                json={"principal_email_or_name": bob_email},
            )
            assert r.status_code == 200, (r.status_code, r.text)
            assert r.json()["principal_id"] == bob_id

            # Step 3: alice grants the engineering group → 200.
            r = await client.post(
                f"/api/documents/{doc_id}/share",
                headers=_bearer(alice_jwt),
                json={"principal_email_or_name": group_name},
            )
            assert r.status_code == 200, (r.status_code, r.text)
            assert r.json()["principal_id"] == group_id
            assert r.json()["principal_type"] == "group"

            # Step 4: alice grants a nonexistent identifier → 404.
            r = await client.post(
                f"/api/documents/{doc_id}/share",
                headers=_bearer(alice_jwt),
                json={"principal_email_or_name": "nonexistent@nowhere.com"},
            )
            assert r.status_code == 404, (r.status_code, r.text)

            # Step 5: bob (not the owner) tries to share alice's doc → 403.
            r = await client.post(
                f"/api/documents/{doc_id}/share",
                headers=_bearer(bob_jwt),
                json={"principal_email_or_name": bob_email},
            )
            assert r.status_code == 403, (r.status_code, r.text)

            # Step 6: alice GET /shares — bob and group both present.
            r = await client.get(
                f"/api/documents/{doc_id}/shares",
                headers=_bearer(alice_jwt),
            )
            assert r.status_code == 200, (r.status_code, r.text)
            shares = r.json()["shares"]
            principals = {(s["principal_type"], s["principal_id"]) for s in shares}
            assert ("user", bob_id) in principals, shares
            assert ("group", group_id) in principals, shares

            # Step 7: alice DELETE bob's share → 204; GET shows only group.
            r = await client.delete(
                f"/api/documents/{doc_id}/share/user/{bob_id}",
                headers=_bearer(alice_jwt),
            )
            assert r.status_code == 204, (r.status_code, r.text)

            r = await client.get(
                f"/api/documents/{doc_id}/shares",
                headers=_bearer(alice_jwt),
            )
            assert r.status_code == 200
            shares = r.json()["shares"]
            principals = {(s["principal_type"], s["principal_id"]) for s in shares}
            assert ("user", bob_id) not in principals, shares
            assert ("group", group_id) in principals, shares

            # Bonus: deleting an already-revoked share → 404.
            r = await client.delete(
                f"/api/documents/{doc_id}/share/user/{bob_id}",
                headers=_bearer(alice_jwt),
            )
            assert r.status_code == 404, (r.status_code, r.text)

            # Bonus: bob hits GET /shares on alice's doc → 403.
            r = await client.get(
                f"/api/documents/{doc_id}/shares",
                headers=_bearer(bob_jwt),
            )
            assert r.status_code == 403, (r.status_code, r.text)

            # Bonus: 409 when the doc isn't ready. Flip status, retry, restore.
            await conn.execute(
                "update public.documents set status='processing' where id=$1::uuid",
                doc_id,
            )
            r = await client.post(
                f"/api/documents/{doc_id}/share",
                headers=_bearer(alice_jwt),
                json={"principal_email_or_name": bob_email},
            )
            assert r.status_code == 409, (r.status_code, r.text)
            await conn.execute(
                "update public.documents set status='ready' where id=$1::uuid",
                doc_id,
            )

            # Bonus: missing-JWT → 401 from the auth middleware.
            r = await client.get(f"/api/documents/{doc_id}/shares")
            assert r.status_code == 401, (r.status_code, r.text)

        print(
            "OK: 7 PRD validation steps + 5 edge cases passed "
            "(grant/list/revoke + 403/404/409/401)"
        )
    finally:
        app.dependency_overrides.pop(get_user, None)
        if seeded is not None:
            await _cleanup(conn, seeded)
        await conn.close()


def main_entry() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main_entry()
