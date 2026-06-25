"""US-070 security-critical validation, LIVE against Postgres + PostgREST.

This is the PRD US-070 "Validation Test" encoded end-to-end against the REAL
`match_chunks` / `keyword_search` RPCs (with the US-070 `filter_workspace_id`
param applied) through PostgREST under a self-minted bot JWT — the exact path
production takes. It proves the two claims a unit test with a mock transport
cannot:

  1. SHARE-TO-BOT IS THE ONLY KEY. A doc D shared to the bot via `chunk_acl` is
     bot-retrievable; a doc E in the SAME workspace, NOT shared, returns ZERO rows
     for the bot — even though its content matches perfectly. The bot holds no
     content role; it sees only owner-OR-ACL grants resolved from `auth.uid()`,
     exactly like any member (ADR-0008: no new enforcement path). A same-data
     owner read is the positive control that makes the bot's 0 a real zero, not a
     vacuous one.

  2. `filter_workspace_id` IS A NON-SECURITY NARROWING FILTER. The doc owner U is
     a member of BOTH workspaces W and W2 and owns a doc in each. With no filter U
     sees both; with `filter_workspace_id = W` U sees only W's docs — the filter
     SUBTRACTS within what membership already allows, and omitting it never leaks
     across workspaces (the membership clause, not the filter, is the boundary).

It also confirms US-068's premise transitively: a self-minted HS256 token
(`role=authenticated`, `sub = bot_user_id`) is accepted by PostgREST and resolves
`auth.uid()` to the bot — the same minting the production bot token uses.

The bot JWT is self-minted with the local project JWT secret and forwarded to
PostgREST verbatim (local GoTrue rejects self-minted tokens), exactly as
test_permissions.py / test_us066_conversations_rls.py do.

Run:
    python -m backend.test_us070_bot_retrieval_integration

Requires a local Supabase running, the US-070 migrations applied
(20260624150000 / 20260624150100), and DATABASE_URL (or
PERMISSIONS_TEST_DATABASE_URL) pointing at its DB; SUPABASE_URL /
SUPABASE_ANON_KEY / SUPABASE_JWT_SECRET fall back to the well-known local
defaults. Skips cleanly when DATABASE_URL is unset or the migration is absent.
Needs no OpenAI.
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

# A 1536-d unit vector. Every seeded chunk gets this SAME embedding and the query
# IS this vector, so cosine == 1.0 for all of them: retrieval is then gated PURELY
# by owner-OR-ACL / membership / the workspace filter, never by similarity.
EMBEDDING = "[" + ",".join(["1"] + ["0"] * 1535) + "]"


def _env(key: str, default: str | None = None) -> str | None:
    val = os.environ.get(key)
    return val if val else default


def _mint_jwt(user_id: str, email: str, secret: str) -> str:
    """Self-sign a Supabase-compatible role=authenticated token (US-068 shape)."""
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


def _headers(jwt_token: str, anon_key: str) -> dict[str, str]:
    return {
        "apikey": anon_key,
        "Authorization": f"Bearer {jwt_token}",
        "Content-Type": "application/json",
    }


class Fixture:
    def __init__(self) -> None:
        self.ws1 = str(uuid.uuid4())   # W  — doc D (shared to bot) + doc E (not)
        self.ws2 = str(uuid.uuid4())   # W2 — doc F (owner-only, other workspace)
        self.owner = str(uuid.uuid4())  # U — owns D, E, F; member of W and W2
        self.bot = str(uuid.uuid4())    # B — support bot; member of W only
        self.doc_d = str(uuid.uuid4())
        self.doc_e = str(uuid.uuid4())
        self.doc_f = str(uuid.uuid4())
        self.chunk_d = str(uuid.uuid4())
        self.chunk_e = str(uuid.uuid4())
        self.chunk_f = str(uuid.uuid4())


async def _seed(conn: asyncpg.Connection, fx: Fixture) -> None:
    await conn.execute(
        """
        insert into auth.users
          (id, instance_id, email, encrypted_password, aud, role,
           raw_app_meta_data, raw_user_meta_data, created_at, updated_at, email_confirmed_at)
        values
          ($1, $3, $4, '', 'authenticated', 'authenticated', '{}'::jsonb, '{}'::jsonb, now(), now(), now()),
          ($2, $3, $5, '', 'authenticated', 'authenticated', '{}'::jsonb, '{}'::jsonb, now(), now(), now())
        """,
        fx.owner, fx.bot, INSTANCE,
        f"owner-{fx.owner[:8]}@test.local", f"bot-{fx.bot[:8]}@test.local",
    )
    await conn.execute(
        "insert into public.workspaces (id, name) values ($1, 'US070-W'), ($2, 'US070-W2')",
        fx.ws1, fx.ws2,
    )
    # U is a member of BOTH workspaces (so the workspace filter, not membership,
    # is what narrows U). B is a member of W only and role='member' — the US-069
    # bot is an ordinary member (is_bot is a non-security flag, irrelevant here).
    await conn.execute(
        """
        insert into public.workspace_membership (workspace_id, user_id, role)
        values ($1, $2, 'member'), ($3, $2, 'member'), ($1, $4, 'member')
        """,
        fx.ws1, fx.owner, fx.ws2, fx.bot,
    )
    # All three docs owned by U; D and E in W, F in W2.
    for doc_id, ws in ((fx.doc_d, fx.ws1), (fx.doc_e, fx.ws1), (fx.doc_f, fx.ws2)):
        await conn.execute(
            """
            insert into public.documents
              (id, user_id, filename, storage_path, byte_size, status, chunks_count,
               uploaded_at, workspace_id)
            values ($1, $2, $3, 'seed/path', 1, 'ready', 1, now(), $4)
            """,
            doc_id, fx.owner, f"{doc_id[:8]}.txt", ws,
        )
    # One chunk per doc, identical embedding + searchable content_tsv.
    for chunk_id, doc_id, content in (
        (fx.chunk_d, fx.doc_d, "shared answer about returns policy"),
        (fx.chunk_e, fx.doc_e, "secret answer about returns policy"),
        (fx.chunk_f, fx.doc_f, "other workspace answer about returns policy"),
    ):
        # content_tsv is a GENERATED column (derived from content) — never insert
        # it; it auto-populates so keyword_search has a tsvector to match.
        await conn.execute(
            """
            insert into public.chunks
              (id, document_id, user_id, chunk_index, content, embedding)
            values ($1, $2, $3, 0, $4, $5::vector)
            """,
            chunk_id, doc_id, fx.owner, content, EMBEDDING,
        )
    # SHARE-TO-BOT: grant ONLY D's chunk to the bot (principal_type='user').
    # E is deliberately NOT shared; F is in a workspace the bot does not belong to.
    await conn.execute(
        """
        insert into public.chunk_acl (chunk_id, principal_type, principal_id, granted_by)
        values ($1, 'user', $2, $3)
        """,
        fx.chunk_d, fx.bot, fx.owner,
    )


async def _cleanup(conn: asyncpg.Connection, fx: Fixture) -> None:
    await conn.execute(
        "delete from public.chunks where id = any($1::uuid[])",
        [fx.chunk_d, fx.chunk_e, fx.chunk_f],
    )
    await conn.execute(
        "delete from public.documents where id = any($1::uuid[])",
        [fx.doc_d, fx.doc_e, fx.doc_f],
    )
    await conn.execute(
        "delete from public.workspaces where id = any($1::uuid[])", [fx.ws1, fx.ws2]
    )
    await conn.execute(
        "delete from auth.users where id = any($1::uuid[])", [fx.owner, fx.bot]
    )


async def _match_chunks(
    http: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    *,
    workspace_id: str | None,
) -> set[str]:
    """Call match_chunks via PostgREST and return the set of document_ids seen."""
    body: dict[str, object] = {
        "query_embedding": EMBEDDING,
        "match_threshold": 0.5,
        "match_count": 50,
    }
    if workspace_id is not None:
        body["filter_workspace_id"] = workspace_id
    r = await http.post(f"{url}/rest/v1/rpc/match_chunks", headers=headers, json=body)
    assert r.status_code == 200, f"match_chunks: {r.status_code} {r.text}"
    return {row["document_id"] for row in r.json()}


async def _keyword_docs(
    http: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    *,
    workspace_id: str | None,
) -> set[str]:
    body: dict[str, object] = {"query": "returns policy", "match_count": 50}
    if workspace_id is not None:
        body["filter_workspace_id"] = workspace_id
    r = await http.post(f"{url}/rest/v1/rpc/keyword_search", headers=headers, json=body)
    assert r.status_code == 200, f"keyword_search: {r.status_code} {r.text}"
    return {row["document_id"] for row in r.json()}


async def _migration_applied(conn: asyncpg.Connection) -> bool:
    """True iff match_chunks carries the US-070 `filter_workspace_id` (9th) arg."""
    row = await conn.fetchval(
        """
        select 1 from pg_proc
        where proname = 'match_chunks'
          and pg_get_function_identity_arguments(oid) like '%filter_workspace_id uuid%'
        """
    )
    return row is not None


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
    checks = 0
    try:
        if not await _migration_applied(conn):
            print("SKIP: US-070 match_chunks filter_workspace_id migration not applied")
            return
        # Nudge PostgREST to pick up the (possibly just-applied) signature.
        await conn.execute("notify pgrst, 'reload schema'")

        await _seed(conn, fx)

        owner_email = await conn.fetchval("select email from auth.users where id=$1::uuid", fx.owner)
        bot_email = await conn.fetchval("select email from auth.users where id=$1::uuid", fx.bot)
        owner_headers = _headers(_mint_jwt(fx.owner, owner_email, jwt_secret), anon_key)
        bot_headers = _headers(_mint_jwt(fx.bot, bot_email, jwt_secret), anon_key)

        async with httpx.AsyncClient(timeout=10.0) as http:
            # --- Claim 1: share-to-bot is the only key (the PRD validation) ---
            # Positive control: the owner sees D AND E (same data, both owned).
            owner_w = await _match_chunks(http, supabase_url, owner_headers, workspace_id=fx.ws1)
            assert fx.doc_d in owner_w and fx.doc_e in owner_w, (
                f"owner control should see BOTH D and E in W, got {owner_w}"
            )
            checks += 1
            print("  owner sees both D and E in W (positive control — data is retrievable)")

            # The bot, scoped to W: sees D (shared) and ZERO of E (not shared).
            bot_w = await _match_chunks(http, supabase_url, bot_headers, workspace_id=fx.ws1)
            assert fx.doc_d in bot_w, f"bot must retrieve shared doc D, got {bot_w}"
            assert fx.doc_e not in bot_w, f"LEAK: bot retrieved NON-shared doc E: {bot_w}"
            assert fx.doc_f not in bot_w, f"LEAK: bot retrieved other-workspace doc F: {bot_w}"
            assert bot_w == {fx.doc_d}, f"bot should see EXACTLY {{D}}, got {bot_w}"
            checks += 1
            print("  bot sees EXACTLY {D} — shared doc only; E (not shared) -> 0 rows")

            # Even with NO workspace filter the bot still sees only D — proving the
            # grant (not the filter) is what hides E, and that nothing leaks when
            # the non-security filter is omitted.
            bot_nofilter = await _match_chunks(http, supabase_url, bot_headers, workspace_id=None)
            assert bot_nofilter == {fx.doc_d}, (
                f"bot with no workspace filter must STILL see only {{D}}, got {bot_nofilter}"
            )
            checks += 1
            print("  bot with NO workspace filter still sees only {D} (grant is the gate, not the filter)")

            # --- Claim 2: filter_workspace_id is a non-security narrowing filter ---
            owner_all = await _match_chunks(http, supabase_url, owner_headers, workspace_id=None)
            assert {fx.doc_d, fx.doc_e, fx.doc_f} <= owner_all, (
                f"owner with no filter should see D, E AND F (both workspaces), got {owner_all}"
            )
            owner_w2 = await _match_chunks(http, supabase_url, owner_headers, workspace_id=fx.ws2)
            assert owner_w2 & {fx.doc_d, fx.doc_e, fx.doc_f} == {fx.doc_f}, (
                f"owner filtered to W2 should see only F among the seeded docs, got {owner_w2}"
            )
            assert fx.doc_f not in owner_w, "owner filtered to W must NOT include W2's doc F"
            checks += 1
            print("  owner: no-filter -> {D,E,F}; filter=W -> {D,E}; filter=W2 -> {F} (narrowing only)")

            # --- The same filter holds on the keyword leg (hybrid coherence) ---
            bot_kw = await _keyword_docs(http, supabase_url, bot_headers, workspace_id=fx.ws1)
            assert bot_kw == {fx.doc_d}, f"keyword leg: bot should see only {{D}}, got {bot_kw}"
            owner_kw_w2 = await _keyword_docs(http, supabase_url, owner_headers, workspace_id=fx.ws2)
            assert owner_kw_w2 & {fx.doc_d, fx.doc_e, fx.doc_f} == {fx.doc_f}, (
                f"keyword leg: owner filtered to W2 should see only F, got {owner_kw_w2}"
            )
            checks += 1
            print("  keyword_search honours filter_workspace_id identically (both hybrid legs scoped)")

    finally:
        await _cleanup(conn, fx)
        await conn.close()

    print(f"\nPASS: {checks} US-070 live integration checks")


if __name__ == "__main__":
    asyncio.run(_run())
