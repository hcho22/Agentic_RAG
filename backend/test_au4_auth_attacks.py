"""US-010 (AU4): API-layer auth attack tests — the tenant boundary at the edge.

Proves that every retrieval-bearing endpoint returns **0 rows** when hit with a
bad or wrong-tenant JWT, and that the auth floor rejects forged/missing/expired
tokens with 401. This is the API-edge counterpart to E6 (US-009), which proves
the same boundary one layer down inside SQL. Both are pinned `fail` security
invariants (CONTEXT E8): every assertion is exact (`== 0`), never "few".

Two phases, because the two attack classes exercise two different code paths:

1. **Auth floor — REAL `get_user` (no override).** Forged (wrong-signature),
   missing, and expired tokens are rejected at `get_user` (`main.py:361`), which
   validates the bearer against the real (local) GoTrue. Forged/expired →
   GoTrue non-200 → 401; missing → 401 before GoTrue. Asserted on every
   retrieval endpoint, including `/api/chat`. These 401 *before* any retrieval
   or LLM call, so this phase needs no OpenAI and no seeded content.

2. **Data boundary — `get_user` override (RLS path).** Local GoTrue refuses
   self-minted tokens, so — exactly like `test_share_api.py` — the override
   decodes the JWT locally and forwards it verbatim to PostgREST, where RLS +
   the US-003/US-004 membership clause resolve `auth.uid()` from the token. A
   *valid* token for a member of Workspace A only (even one holding an ACL on
   B's chunks) must retrieve 0 of Workspace B's content; a valid token whose
   `sub` is a user with no membership must retrieve 0 everywhere. A B-member
   positive control confirms the same content/endpoint *does* return rows, so a
   0 is a real zero and not a false pass from an empty corpus.

Run:
    python -m backend.test_au4_auth_attacks

Requires the same env as test_share_api.py (DATABASE_URL + backend/.env's
SUPABASE_URL / SUPABASE_ANON_KEY / OPENAI_API_KEY); skips cleanly when unset.
If the OpenAI embedding call is unavailable (e.g. quota), the vector / hybrid /
rerank / chat-tool data-boundary checks are SKIPPED (their positive control
needs real embeddings to be meaningful) — the auth floor on every endpoint and
the keyword endpoint's full leak+positive proof still run and are logged.
"""

from __future__ import annotations

import asyncio
import json
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

_required = ("SUPABASE_URL", "SUPABASE_ANON_KEY", "OPENAI_API_KEY")
_missing = [k for k in _required if not os.environ.get(k)]
if _missing:
    print(f"SKIP: missing env: {', '.join(_missing)}")
    sys.exit(0)

from fastapi import Header, HTTPException  # noqa: E402

from embeddings import embed_texts, to_pgvector  # noqa: E402
from main import (  # noqa: E402
    AuthedUser,
    _execute_tool_call,
    app,
    get_user,
    openai_client,
)

LOCAL_JWT_SECRET = "super-secret-jwt-token-with-at-least-32-characters-long"
JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET") or LOCAL_JWT_SECRET
INSTANCE = "00000000-0000-0000-0000-000000000000"

# Every endpoint that can return retrieved content. keyword needs no OpenAI;
# the rest embed the query, so their data-boundary checks are gated on real
# embeddings being available.
KEYWORD_ENDPOINT = "/api/search/keyword"
VECTOR_ENDPOINTS = ("/api/search", "/api/search/hybrid", "/api/search/rerank")
SEARCH_ENDPOINTS = (KEYWORD_ENDPOINT,) + VECTOR_ENDPOINTS

# A query that hits B's gold by exact keyword (no embedding needed) and, with
# real embeddings, by vector similarity too.
QUERY = "Project Helios quarterly revenue forecast"
B_CHUNKS = [
    "Project Helios quarterly revenue forecast is forty million dollars for FY2027.",
    "The Helios launch window opens in March and closes in June.",
]


def _mint_jwt(
    sub: str, email: str, *, exp_delta: int = 3600, secret: str = JWT_SECRET
) -> str:
    now = int(time.time())
    return pyjwt.encode(
        {
            "iss": "supabase-demo",
            "sub": sub,
            "email": email,
            "role": "authenticated",
            "aud": "authenticated",
            "iat": now,
            "exp": now + exp_delta,
        },
        secret,
        algorithm="HS256",
    )


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _override_get_user(
    authorization: str | None = Header(default=None),
) -> AuthedUser:
    """Test-only stand-in for get_user (local GoTrue rejects self-minted tokens).

    Decodes the user id from the JWT and forwards the token verbatim, so RLS in
    PostgREST still runs as the token's `sub` — the membership clause resolves
    the workspace boundary from `auth.uid()`, identically to production.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    payload = pyjwt.decode(
        token, JWT_SECRET, algorithms=["HS256"], audience="authenticated"
    )
    return AuthedUser(id=payload["sub"], access_token=token)


class Fixture:
    def __init__(self) -> None:
        self.ws_a = str(uuid.uuid4())
        self.ws_b = str(uuid.uuid4())
        self.v_id = str(uuid.uuid4())          # member of A only (+ ACL on B)
        self.b_owner_id = str(uuid.uuid4())    # member of B, owns B's doc
        self.mallory_id = str(uuid.uuid4())    # member of nothing
        self.doc_id = str(uuid.uuid4())
        self.embeddings_real = False


async def _seed(conn: asyncpg.Connection, fx: Fixture) -> None:
    await conn.execute(
        """
        insert into auth.users
          (id, instance_id, email, encrypted_password, aud, role,
           raw_app_meta_data, raw_user_meta_data,
           created_at, updated_at, email_confirmed_at)
        values
          ($1, $4, $5, '', 'authenticated', 'authenticated',
           '{}'::jsonb, '{}'::jsonb, now(), now(), now()),
          ($2, $4, $6, '', 'authenticated', 'authenticated',
           '{}'::jsonb, '{}'::jsonb, now(), now(), now()),
          ($3, $4, $7, '', 'authenticated', 'authenticated',
           '{}'::jsonb, '{}'::jsonb, now(), now(), now())
        """,
        fx.v_id, fx.b_owner_id, fx.mallory_id, INSTANCE,
        f"v-{fx.v_id[:8]}@test.local",
        f"bowner-{fx.b_owner_id[:8]}@test.local",
        f"mallory-{fx.mallory_id[:8]}@test.local",
    )
    await conn.execute(
        "insert into public.workspaces (id, name) values ($1, 'AU4-A'), ($2, 'AU4-B')",
        fx.ws_a, fx.ws_b,
    )
    # V → A only; B_OWNER → B; MALLORY → nothing.
    await conn.execute(
        """
        insert into public.workspace_membership (workspace_id, user_id, role)
        values ($1, $2, 'member'), ($3, $4, 'member')
        """,
        fx.ws_a, fx.v_id, fx.ws_b, fx.b_owner_id,
    )
    # B's document lives in Workspace B, owned by B_OWNER.
    await conn.execute(
        """
        insert into public.documents
          (id, user_id, workspace_id, filename, storage_path, byte_size,
           content_type, status)
        values ($1, $2, $3, 'au4-secret.txt', $4, 100, 'text/plain', 'ready')
        """,
        fx.doc_id, fx.b_owner_id, fx.ws_b, f"{fx.b_owner_id}/au4-secret.txt",
    )

    # Real embeddings make the vector endpoints meaningful; fall back to a dummy
    # vector (keyword + auth-floor stay valid) if OpenAI is unavailable.
    try:
        vectors = await embed_texts(openai_client, B_CHUNKS)
        pg_vectors = [to_pgvector(v) for v in vectors]
        fx.embeddings_real = True
    except Exception as e:  # noqa: BLE001 — degrade, don't fail the whole suite
        print(f"  (note: embeddings unavailable, vector checks will skip: {e})")
        dummy = "[" + ",".join(["0.01"] * 1536) + "]"
        pg_vectors = [dummy for _ in B_CHUNKS]

    chunk_ids = [str(uuid.uuid4()) for _ in B_CHUNKS]
    await conn.executemany(
        """
        insert into public.chunks
          (id, document_id, user_id, chunk_index, content, embedding)
        values ($1, $2, $3, $4, $5, $6::extensions.vector)
        """,
        [
            (chunk_ids[i], fx.doc_id, fx.b_owner_id, i, B_CHUNKS[i], pg_vectors[i])
            for i in range(len(B_CHUNKS))
        ],
    )
    # V holds a user-ACL on B's chunks — so the ONLY thing blocking V from B's
    # content is that V is not a member of Workspace B (isolates the boundary,
    # exactly like E6).
    await conn.executemany(
        """
        insert into public.chunk_acl (chunk_id, principal_type, principal_id, granted_by)
        values ($1, 'user', $2, $3)
        """,
        [(cid, fx.v_id, fx.b_owner_id) for cid in chunk_ids],
    )


async def _cleanup(conn: asyncpg.Connection, fx: Fixture) -> None:
    # Doc first (cascades chunks → chunk_acl, clearing the granted_by FK), then
    # workspaces (cascades membership), then users.
    await conn.execute("delete from public.documents where id = $1::uuid", fx.doc_id)
    await conn.execute(
        "delete from public.workspace_membership where workspace_id = any($1::uuid[])",
        [fx.ws_a, fx.ws_b],
    )
    await conn.execute(
        "delete from public.workspaces where id = any($1::uuid[])", [fx.ws_a, fx.ws_b]
    )
    await conn.execute(
        "delete from auth.users where id = any($1::uuid[])",
        [fx.v_id, fx.b_owner_id, fx.mallory_id],
    )


# ---------------------------------------------------------------------------
# Phase 1: auth floor (real get_user / GoTrue)
# ---------------------------------------------------------------------------


async def _auth_floor_phase(client: httpx.AsyncClient, fx: Fixture) -> int:
    """Forged / missing / expired tokens → 401 on every retrieval endpoint."""
    real_user = fx.b_owner_id  # a real, confirmed user
    email = f"bowner-{real_user[:8]}@test.local"
    forged = _mint_jwt(real_user, email, secret="wrong-secret-" + "x" * 32)
    expired = _mint_jwt(real_user, email, exp_delta=-3600)

    bad_tokens = {"forged": forged, "expired": expired}
    checks = 0

    async def _post(path: str, headers: dict[str, str]) -> httpx.Response:
        if path == "/api/chat":
            body = {"thread_id": str(uuid.uuid4()), "message": "hello"}
        else:
            body = {"query": QUERY}
        return await client.post(path, headers=headers, json=body)

    for path in SEARCH_ENDPOINTS + ("/api/chat",):
        # missing bearer
        r = await _post(path, {})
        assert r.status_code == 401, f"{path} missing-token: {r.status_code} {r.text}"
        checks += 1
        # forged + expired
        for label, tok in bad_tokens.items():
            r = await _post(path, _bearer(tok))
            assert r.status_code == 401, (
                f"{path} {label}-token: expected 401, got {r.status_code} {r.text}"
            )
            checks += 1
    print(f"  auth floor: {checks} checks passed (forged/missing/expired → 401)")
    return checks


# ---------------------------------------------------------------------------
# Phase 2: data boundary (get_user override → RLS)
# ---------------------------------------------------------------------------


async def _endpoint_count(
    client: httpx.AsyncClient, path: str, token: str
) -> int:
    r = await client.post(path, headers=_bearer(token), json={"query": QUERY, "top_k": 10})
    assert r.status_code == 200, f"{path}: {r.status_code} {r.text}"
    return len(r.json()["results"])


async def _chat_tool_count(http: httpx.AsyncClient, token: str, sub: str) -> int:
    """Drive the chat retrieval path at its data-access boundary.

    Calls the exact function the /api/chat completions loop invokes for the
    search_documents tool, with the attacker's user — deterministic, and tests
    the same RLS boundary the agent would hit without driving the LLM.
    """
    out = await _execute_tool_call(
        http, AuthedUser(id=sub, access_token=token), "search_documents",
        json.dumps({"query": QUERY, "top_k": 10}),
    )
    payload = json.loads(out)
    assert "error" not in payload, f"chat tool errored: {payload}"
    return int(payload["count"])


async def _data_boundary_phase(
    client: httpx.AsyncClient, http: httpx.AsyncClient, fx: Fixture
) -> int:
    b_owner_jwt = _mint_jwt(fx.b_owner_id, f"bowner-{fx.b_owner_id[:8]}@test.local")
    v_jwt = _mint_jwt(fx.v_id, f"v-{fx.v_id[:8]}@test.local")
    mallory_jwt = _mint_jwt(fx.mallory_id, f"mallory-{fx.mallory_id[:8]}@test.local")
    checks = 0

    # --- keyword endpoint: meaningful WITHOUT OpenAI -----------------------
    # Positive control: B_OWNER (member of B + owner) sees B's content.
    pos = await _endpoint_count(client, KEYWORD_ENDPOINT, b_owner_jwt)
    assert pos >= 1, (
        f"positive control failed: B-member retrieved {pos} rows of B's own "
        "content — the eval is structurally blind, so any 0 below is a false pass"
    )
    checks += 1
    # Cross-workspace: V is a member of A only (with an ACL on B) → 0.
    leak = await _endpoint_count(client, KEYWORD_ENDPOINT, v_jwt)
    assert leak == 0, f"CROSS-WORKSPACE LEAK on {KEYWORD_ENDPOINT}: V got {leak} rows of B"
    checks += 1
    # Tampered-sub / no-membership: MALLORY → 0.
    leak = await _endpoint_count(client, KEYWORD_ENDPOINT, mallory_jwt)
    assert leak == 0, f"NO-MEMBERSHIP LEAK on {KEYWORD_ENDPOINT}: MALLORY got {leak} rows"
    checks += 1
    print(f"  keyword data boundary: B-member={pos} rows, V=0, MALLORY=0")

    # --- vector / hybrid / rerank + chat tool: need real embeddings --------
    if not fx.embeddings_real:
        print(
            "  SKIP vector/hybrid/rerank/chat-tool data boundary: no real "
            "embeddings (auth floor on these endpoints already passed above)"
        )
        return checks

    for path in VECTOR_ENDPOINTS:
        pos = await _endpoint_count(client, path, b_owner_jwt)
        assert pos >= 1, f"positive control failed on {path}: B-member got {pos} rows"
        v_leak = await _endpoint_count(client, path, v_jwt)
        assert v_leak == 0, f"CROSS-WORKSPACE LEAK on {path}: V got {v_leak} rows of B"
        m_leak = await _endpoint_count(client, path, mallory_jwt)
        assert m_leak == 0, f"NO-MEMBERSHIP LEAK on {path}: MALLORY got {m_leak} rows"
        checks += 3
        print(f"  {path} data boundary: B-member={pos} rows, V=0, MALLORY=0")

    # chat retrieval path (search_documents tool)
    pos = await _chat_tool_count(http, b_owner_jwt, fx.b_owner_id)
    assert pos >= 1, f"positive control failed on chat tool: B-member got {pos} rows"
    v_leak = await _chat_tool_count(http, v_jwt, fx.v_id)
    assert v_leak == 0, f"CROSS-WORKSPACE LEAK on chat tool: V got {v_leak} rows of B"
    m_leak = await _chat_tool_count(http, mallory_jwt, fx.mallory_id)
    assert m_leak == 0, f"NO-MEMBERSHIP LEAK on chat tool: MALLORY got {m_leak} rows"
    checks += 3
    print(f"  /api/chat search_documents tool: B-member={pos} rows, V=0, MALLORY=0")
    return checks


async def _run() -> None:
    db_url = (
        os.environ.get("PERMISSIONS_TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
    )
    if not db_url:
        print("SKIP: PERMISSIONS_TEST_DATABASE_URL/DATABASE_URL unset")
        return

    fx = Fixture()
    conn = await asyncpg.connect(db_url)
    total = 0
    try:
        await _seed(conn, fx)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            # Phase 1 runs against the REAL get_user (GoTrue validation).
            total += await _auth_floor_phase(client, fx)

            # Phase 2 needs the override so a valid token reaches RLS.
            app.dependency_overrides[get_user] = _override_get_user
            try:
                async with httpx.AsyncClient(timeout=60.0) as http:
                    total += await _data_boundary_phase(client, http, fx)
            finally:
                app.dependency_overrides.pop(get_user, None)

        print(f"OK: AU4 passed — {total} exact assertions across the auth floor "
              "and the cross-workspace / no-membership data boundary")
    finally:
        await _cleanup(conn, fx)
        await conn.close()


def main_entry() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main_entry()
