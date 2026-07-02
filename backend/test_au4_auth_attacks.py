"""US-010 (AU4): API-layer auth attack tests — the tenant boundary at the edge.

Proves that every retrieval-bearing endpoint returns **0 rows** when hit with a
bad or wrong-tenant JWT, and that the auth floor rejects forged/missing/expired
tokens with 401. This is the API-edge counterpart to E6 (US-009), which proves
the same boundary one layer down inside SQL. Both are pinned `fail` security
invariants (CONTEXT E8): every assertion is exact (`== 0`), never "few".

US-093 extends this suite with a THIRD phase — the widget/conversation attack
surface (Epic E / ADR-0008 + ADR-0004). The public support widget and its opaque
customer token add three new trust boundaries beside the retrieval boundary above;
Phase 3 pins each as a deterministic `== 0 / rejected` invariant at the API edge:

  1. A cross-workspace real JWT retrieves **0** conversations / 0
     `conversation_messages` from another workspace (US-066 membership RLS), read
     over the SAME PostgREST surface the operator dashboard uses.
  2. A per-conversation opaque token reads **only its own** conversation — the
     transcript endpoint returns its bound conversation but rejects a request for
     any OTHER conversation with 401 / 0 rows (US-071/US-081 binding check).
  3. A **revoked** or **originless** widget key mints **no session** — the
     first-message endpoint refuses (opaque 404), creating no conversation row and
     issuing no token (US-072 not-revoked gate + US-073 fail-closed origin gate).
  4. The opaque **customer token retrieves 0 chunks** — it is NOT a Supabase JWT,
     so PostgREST rejects it at `match_chunks`; the customer is structurally off
     retrieval (only a server-minted Supabase JWT, the bot's US-068 mechanism, is a
     retrieval principal). A valid minted JWT is the positive control that makes
     the customer's zero a real, auth-based zero.

Phase 3 skips cleanly when the Epic-E migrations (conversations / conversation_
tokens / widget_keys) are not applied, so a knowledge-assistant-only deployment
still runs the retrieval phases below unchanged.

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

# US-093 widget/conversation phase (Epic E): the transcript + first-message
# endpoints read/write UNDER THE SERVICE ROLE, and main.py reads
# SUPABASE_SERVICE_ROLE_KEY at import time. Default it to the well-known local
# service_role key BEFORE importing main so those endpoints have a working path —
# this suite is local-Supabase-only by construction (the data-boundary phase
# forwards self-minted JWTs that only the local GoTrue secret validates), so a
# local default is consistent; a real env value is respected via setdefault.
# Absent a local Supabase / the Epic-E migrations, the widget phase skips cleanly.
LOCAL_SERVICE_ROLE_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImV4cCI6MTk4MzgxMjk5Nn0."
    "EGIM96RAZx35lJzdJsyH-qQwv8Hdp7fsn3W0YpN81IU"
)
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", LOCAL_SERVICE_ROLE_KEY)

from fastapi import Header, HTTPException  # noqa: E402

from conversation_tokens import (  # noqa: E402
    generate_conversation_token,
    hash_conversation_token,
)
from embeddings import embed_texts, to_pgvector  # noqa: E402
from main import (  # noqa: E402
    _CONVERSATION_TOKEN_HEADER,
    AuthedUser,
    _execute_tool_call,
    app,
    get_user,
    openai_client,
)
from widget_keys import generate_public_key  # noqa: E402

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

        # --- US-093 widget/conversation attack fixtures (Epic E) ---------------
        # Reuse the retrieval workspaces: W1 = ws_a, W2 = ws_b. Conversation X in
        # W1, Y in W2; X carries an opaque customer token Tx (US-071). The widget
        # keys live in a THIRD workspace W3 (no conversations) so "no session
        # minted" is a clean count of exactly zero.
        self.c_x = str(uuid.uuid4())              # conversation X in W1 (ws_a)
        self.c_y = str(uuid.uuid4())              # conversation Y in W2 (ws_b)
        self.mx = str(uuid.uuid4())               # customer message in X
        self.my = str(uuid.uuid4())               # customer message in Y
        self.tx = generate_conversation_token()   # opaque token bound to X
        self.ws_widget = str(uuid.uuid4())        # W3: holds the widget keys
        self.pk_active = generate_public_key()    # K  — active, origin-listed
        self.pk_revoked = generate_public_key()   # Kr — revoked
        self.pk_originless = generate_public_key()  # Ko — empty allowlist (inactive)
        self.widget_seeded = False


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


async def _widget_tables_present(conn: asyncpg.Connection) -> bool:
    """True iff every Epic-E table the widget phase needs is present."""
    for table in (
        "conversations",
        "conversation_messages",
        "conversation_tokens",
        "widget_keys",
    ):
        if not await conn.fetchval("select to_regclass($1)", f"public.{table}"):
            return False
    return True


async def _seed_widget(conn: asyncpg.Connection, fx: Fixture) -> None:
    """Seed the US-093 widget/conversation fixtures (Epic E), reusing ws_a/ws_b.

    Sets `widget_seeded` FIRST so a partial seed is still torn down by _cleanup
    (every delete is id-scoped and idempotent)."""
    fx.widget_seeded = True

    # Conversations X (W1=ws_a) and Y (W2=ws_b), each with one customer message.
    # Born 'active' so the US-067 trigger leaves escalated_at null (irrelevant here;
    # keeps the rows honest). No caller-supplied escalated_at.
    await conn.execute(
        """
        insert into public.conversations (id, workspace_id, status)
        values ($1, $2, 'active'), ($3, $4, 'active')
        """,
        fx.c_x, fx.ws_a, fx.c_y, fx.ws_b,
    )
    await conn.execute(
        """
        insert into public.conversation_messages (id, conversation_id, role, content)
        values ($1, $2, 'user', 'hello from W1'), ($3, $4, 'user', 'secret from W2')
        """,
        fx.mx, fx.c_x, fx.my, fx.c_y,
    )
    # Issue Tx exactly as the backend would (US-071): store ONLY the hash, 24h TTL.
    await conn.execute(
        """
        insert into public.conversation_tokens (token_hash, conversation_id, expires_at)
        values ($1, $2, now() + interval '24 hours')
        """,
        hash_conversation_token(fx.tx), fx.c_x,
    )

    # W3 holds three keys: K active (origin-listed), Kr revoked, Ko originless
    # (empty allowlist → US-073 fail-closed inactive). created_by is a real user so
    # the FK holds; the workspace has no bot / conversation, so "no session minted"
    # is a clean zero-count below.
    await conn.execute(
        "insert into public.workspaces (id, name) values ($1, 'AU4-widget')",
        fx.ws_widget,
    )
    await conn.execute(
        """
        insert into public.widget_keys
          (workspace_id, public_key, allowed_origins, created_by)
        values
          ($1, $2, '{https://client.example}', $5),
          ($1, $3, '{https://client.example}', $5),
          ($1, $4, '{}', $5)
        """,
        fx.ws_widget, fx.pk_active, fx.pk_revoked, fx.pk_originless, fx.b_owner_id,
    )
    # Revoke Kr (rotation: K is its replacement). Revocation is a one-way latch — Kr
    # can never resolve again, so it mints nothing.
    await conn.execute(
        "update public.widget_keys set revoked_at = now() where public_key = $1",
        fx.pk_revoked,
    )


async def _cleanup(conn: asyncpg.Connection, fx: Fixture) -> None:
    # US-093: drop the Epic-E fixtures FIRST — conversations have no ON DELETE
    # CASCADE from workspaces, so X/Y (in ws_a/ws_b) must go before those
    # workspaces are deleted below. Deleting X/Y cascades their messages + tokens;
    # deleting W3 cascades its widget_keys.
    if fx.widget_seeded:
        await conn.execute(
            "delete from public.conversations where id = any($1::uuid[])",
            [fx.c_x, fx.c_y],
        )
        await conn.execute(
            "delete from public.workspaces where id = $1::uuid", fx.ws_widget
        )

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


# ---------------------------------------------------------------------------
# Phase 3: widget / conversation attack surface (US-093)
# ---------------------------------------------------------------------------

_DUMMY_VECTOR = "[" + ",".join(["0.01"] * 1536) + "]"


def _pgrst_headers(bearer: str, anon_key: str) -> dict[str, str]:
    """PostgREST headers with `apikey` (routing) + a bearer (the principal)."""
    return {
        "apikey": anon_key,
        "Authorization": f"Bearer {bearer}",
        "Content-Type": "application/json",
    }


async def _pgrst_get(
    http: httpx.AsyncClient, supabase_url: str, headers: dict[str, str], query: str
) -> list[dict]:
    r = await http.get(f"{supabase_url}/rest/v1/{query}", headers=headers)
    assert r.status_code == 200, f"{query}: {r.status_code} {r.text}"
    return r.json()


async def _widget_conversation_phase(
    client: httpx.AsyncClient,
    http: httpx.AsyncClient,
    conn: asyncpg.Connection,
    fx: Fixture,
    supabase_url: str,
    anon_key: str,
) -> int:
    """US-093: the four widget/conversation attack invariants, at the API edge.

    Every negative is exact (`== 0` / `rejected`); every positive control proves
    the corresponding zero/refusal is real, not a structurally blind pass.
    """
    checks = 0
    v_h = _pgrst_headers(
        _mint_jwt(fx.v_id, f"v-{fx.v_id[:8]}@test.local"), anon_key
    )  # W1 member
    b_h = _pgrst_headers(
        _mint_jwt(fx.b_owner_id, f"bowner-{fx.b_owner_id[:8]}@test.local"), anon_key
    )  # W2 member
    m_h = _pgrst_headers(
        _mint_jwt(fx.mallory_id, f"mallory-{fx.mallory_id[:8]}@test.local"), anon_key
    )  # no membership

    # --- Case 1: cross-workspace real JWT → 0 conversations / 0 messages -------
    # This is the surface the operator dashboard (/support/queue, US-087) reads:
    # conversations + conversation_messages over PostgREST under the agent's own
    # JWT, gated by the US-066 membership RLS.
    #
    # The validation-test step: a W2-member lists conversations → sees Y, ZERO of
    # W1's X. (Positive control: b_owner CAN see its own Y, so X's absence is a
    # real zero.)
    b_convs = {r["id"] for r in await _pgrst_get(http, supabase_url, b_h, "conversations?select=id")}
    assert fx.c_y in b_convs, (
        f"positive control failed: W2 member cannot read its own conversation Y: {b_convs}"
    )
    assert fx.c_x not in b_convs, (
        f"CROSS-WORKSPACE LEAK: W2 member's conversation list includes W1's X: {b_convs}"
    )
    # Symmetric: a W1-member sees X, ZERO of Y.
    v_convs = {r["id"] for r in await _pgrst_get(http, supabase_url, v_h, "conversations?select=id")}
    assert fx.c_x in v_convs and fx.c_y not in v_convs, (
        f"CROSS-WORKSPACE LEAK (W1→W2): W1 member list = {v_convs}"
    )
    # No-membership Mallory sees ZERO conversations at all.
    m_convs = await _pgrst_get(http, supabase_url, m_h, "conversations?select=id")
    assert m_convs == [], f"NO-MEMBERSHIP LEAK: Mallory read conversations: {m_convs}"
    checks += 1
    print("  case 1a: cross-workspace JWT → 0 conversations (W2↮W1, Mallory=0)")

    # conversation_messages delegate to the parent conversation: a W2 member reads
    # ZERO of W1's X's messages (positive control: its own Y's message is visible).
    bx_msgs = await _pgrst_get(
        http, supabase_url, b_h, f"conversation_messages?conversation_id=eq.{fx.c_x}&select=id"
    )
    assert bx_msgs == [], f"CROSS-WORKSPACE LEAK: W2 member read W1 X's messages: {bx_msgs}"
    by_msgs = await _pgrst_get(
        http, supabase_url, b_h, f"conversation_messages?conversation_id=eq.{fx.c_y}&select=id"
    )
    assert {r["id"] for r in by_msgs} == {fx.my}, (
        f"positive control failed: W2 member cannot read its own Y's message: {by_msgs}"
    )
    checks += 1
    print("  case 1b: cross-workspace JWT → 0 conversation_messages (child delegation)")

    # --- Case 2: opaque token reads ONLY its own conversation -----------------
    # The transcript endpoint (US-071) binds the token to its ONE conversation and
    # rejects a request for any other id. Driven through the REAL endpoint so the
    # endpoint-level binding check — not just the RPC — is under test.
    tok_h = {_CONVERSATION_TOKEN_HEADER: fx.tx}
    rx = await client.get(f"/widget/conversations/{fx.c_x}/transcript", headers=tok_h)
    assert rx.status_code == 200, (
        f"positive control failed: Tx cannot read its OWN conversation X: "
        f"{rx.status_code} {rx.text}"
    )
    x_msgs = rx.json().get("messages", [])
    assert any(msg["id"] == fx.mx for msg in x_msgs), (
        f"Tx transcript of X must contain X's message, got {x_msgs}"
    )
    # Tx requesting Y (a DIFFERENT conversation) → 401, ZERO rows (binding rejected).
    ry = await client.get(f"/widget/conversations/{fx.c_y}/transcript", headers=tok_h)
    assert ry.status_code == 401, (
        f"CROSS-CONVERSATION LEAK: Tx read Y's transcript: {ry.status_code} {ry.text}"
    )
    assert "secret from W2" not in ry.text, "CROSS-CONVERSATION LEAK: Tx response contained Y's content"
    checks += 1
    print("  case 2: opaque token Tx → its OWN conversation only (Tx→Y rejected 401)")

    # --- Case 3: revoked / originless key mints NO session --------------------
    # Positive control: the ACTIVE key K with a listed Origin resolves active —
    # proving the fixture's keys work, so the refusals below are real, not a broken
    # key. (Resolution creates no conversation; it only validates the key.)
    origin_h = {"Origin": "https://client.example"}
    rk = await client.post(
        "/widget/keys/resolve", json={"public_key": fx.pk_active}, headers=origin_h
    )
    assert rk.status_code == 200 and rk.json().get("active") is True, (
        f"positive control failed: active key K + listed origin must resolve active: "
        f"{rk.status_code} {rk.text}"
    )

    # A revoked key (Kr) and an originless key (Ko) as a FIRST-message key must each
    # refuse (opaque 404) BEFORE creating any conversation or issuing any token.
    before = await conn.fetchval(
        "select count(*) from public.conversations where workspace_id = $1::uuid",
        fx.ws_widget,
    )
    assert before == 0, f"precondition: W3 must start with 0 conversations, has {before}"
    for label, pk in (("revoked", fx.pk_revoked), ("originless", fx.pk_originless)):
        r = await client.post(
            "/widget/conversations/messages",
            json={"public_key": pk, "message": "hello"},
            headers=origin_h,
        )
        assert r.status_code == 404, (
            f"{label} key must mint NOTHING (opaque 404), got {r.status_code} {r.text}"
        )
        issued = {k.lower() for k in r.headers} & {_CONVERSATION_TOKEN_HEADER.lower()}
        assert not issued, f"{label} key issued a conversation token: {r.headers}"
    after = await conn.fetchval(
        "select count(*) from public.conversations where workspace_id = $1::uuid",
        fx.ws_widget,
    )
    assert after == 0, (
        f"SESSION MINTED from a revoked/originless key: W3 conversations {before}→{after}"
    )
    checks += 1
    print("  case 3: revoked + originless key → 404, 0 conversations, 0 tokens (K resolves active)")

    # --- Case 4: opaque customer token retrieves 0 chunks ---------------------
    # The customer token is NOT a Supabase JWT, so PostgREST rejects it at the auth
    # layer before match_chunks runs → 0 chunks. Build a query embedding matched to
    # the seeded chunk mode so the positive control genuinely retrieves.
    if fx.embeddings_real:
        try:
            q_vec = to_pgvector((await embed_texts(openai_client, [QUERY]))[0])
            strong_positive = True
        except Exception:  # noqa: BLE001 — degrade the positive control, not the invariant
            q_vec, strong_positive = _DUMMY_VECTOR, False
    else:
        # Dummy query == the dummy chunk vectors ⇒ cosine similarity 1.0 ⇒ the owner
        # positive control still retrieves ≥1 row without real embeddings.
        q_vec, strong_positive = _DUMMY_VECTOR, True
    body = {"query_embedding": q_vec, "match_threshold": 0.0, "match_count": 10}

    # NEGATIVE (the invariant): the opaque token as a bearer → rejected → 0 chunks.
    cust_h = _pgrst_headers(fx.tx, anon_key)  # fx.tx is the opaque token, NOT a JWT
    r_cust = await http.post(
        f"{supabase_url}/rest/v1/rpc/match_chunks", headers=cust_h, json=body
    )
    assert r_cust.status_code != 200, (
        f"CUSTOMER TOKEN ACCEPTED BY match_chunks (must be rejected — not a JWT / not "
        f"a retrieval principal): got {r_cust.status_code} {r_cust.text[:200]}"
    )
    assert "Helios" not in r_cust.text, "customer-token match_chunks response leaked chunk content"
    checks += 1
    print("  case 4a: opaque customer token → match_chunks REJECTED (0 chunks)")

    # POSITIVE control: a valid server-minted Supabase JWT (the SAME self-signed
    # HS256 mechanism the bot uses, US-068) for a real member/owner IS accepted and
    # retrieves — so the customer's zero is a real, auth-based zero, not a broken RPC.
    r_ok = await http.post(
        f"{supabase_url}/rest/v1/rpc/match_chunks",
        headers=_pgrst_headers(
            _mint_jwt(fx.b_owner_id, f"bowner-{fx.b_owner_id[:8]}@test.local"), anon_key
        ),
        json=body,
    )
    assert r_ok.status_code == 200, (
        f"positive control failed: a valid minted Supabase JWT must be accepted by "
        f"match_chunks: {r_ok.status_code} {r_ok.text[:200]}"
    )
    if strong_positive:
        assert len(r_ok.json()) >= 1, (
            "positive control failed: the minted-JWT owner principal must retrieve ≥1 "
            "chunk (so the customer-token zero is a real zero)"
        )
    checks += 1
    print(
        f"  case 4b: valid minted Supabase JWT ACCEPTED "
        f"({'≥1 chunk' if strong_positive else '200'}) — customer's 0 is a real zero"
    )

    return checks


async def _run() -> None:
    db_url = (
        os.environ.get("PERMISSIONS_TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
    )
    if not db_url:
        print("SKIP: PERMISSIONS_TEST_DATABASE_URL/DATABASE_URL unset")
        return

    supabase_url = os.environ["SUPABASE_URL"].rstrip("/")
    anon_key = os.environ["SUPABASE_ANON_KEY"]

    fx = Fixture()
    conn = await asyncpg.connect(db_url)
    total = 0
    widget_ran = False
    try:
        await _seed(conn, fx)
        # US-093 Phase 3 needs the Epic-E tables; seed its fixtures only when they
        # exist (a knowledge-assistant-only DB runs the retrieval phases unchanged).
        widget_present = await _widget_tables_present(conn)
        if widget_present:
            await _seed_widget(conn, fx)

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

            # Phase 3: widget/conversation attack surface (US-093). No get_user
            # override — these endpoints are opaque-token / service-role / PostgREST-
            # authed, not Supabase-session-authed.
            if widget_present:
                async with httpx.AsyncClient(timeout=30.0) as http:
                    total += await _widget_conversation_phase(
                        client, http, conn, fx, supabase_url, anon_key
                    )
                widget_ran = True
            else:
                print(
                    "  SKIP Phase 3 (widget/conversation): Epic-E tables absent "
                    "(conversations / widget_keys migrations not applied)"
                )

        tail = (
            " and the widget/conversation boundary (US-093)" if widget_ran else ""
        )
        print(
            f"OK: AU4 passed — {total} exact assertions across the auth floor, the "
            f"cross-workspace / no-membership data boundary" + tail
        )
    finally:
        await _cleanup(conn, fx)
        await conn.close()


def main_entry() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main_entry()
