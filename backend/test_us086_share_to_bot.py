"""US-086: share-to-bot is a SEPARATE, explicitly-confirmed publish action.

Two layers, the same shape as the other support-surface tests
(`test_us080_escalation_latch.py`, `test_us082_agent_reply.py`):

  * a UNIT layer (always runs, no DB / no LLM / no app network, via an httpx
    `MockTransport`):
      - `support_bot.is_bot_user` returns True only for a user with an `is_bot`
        membership row, False for a plain user, and False for a non-UUID id
        WITHOUT touching the network (a bot id is always an auth.users UUID).
      - `support_bot.find_workspace_bot` resolves the workspace's `is_bot` row to
        the bot id, and None when no bot has been provisioned.
      - `main._reject_if_bot_principal` raises 403 for a bot `user` principal, is a
        no-op for a group principal (never queries), and a no-op when the service
        role key is unset (no bot can exist without it).

  * an HTTP-SURFACE layer (skips cleanly when the app can't import), encoding the
    PRD US-086 "Validation Test" end-to-end through the REAL share + publish-to-bot
    endpoints via a FastAPI TestClient, every DB-touching helper mocked (no
    Supabase, no OpenAI):
      - STEP 1: typing the bot's email into the normal share box → 403 and NO
        chunk_acl grant (the failure indicator: a silent share to the widget). A
        real teammate email is the positive control: it resolves + grants.
      - STEP 2: the explicit `POST /publish-to-bot` grants the bot as a `user`
        principal through the same chunk_acl mechanism — only after being invoked
        as its own distinct action.
      - The bot is NEVER surfaced as a quiet grantee row: `GET /shares` filters it
        out (positive control: the human grant is still listed), and its published
        state is reported only by `GET /publish-to-bot`.
      - Publishing into a workspace with no bot (support not enabled) is a clean
        409, never a silent provision; `DELETE /publish-to-bot` unpublishes.

Run:
    python -m backend.test_us086_share_to_bot
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

# The app reads SUPABASE_* / a provider key at import time. Supply local defaults
# so the import succeeds without a real deployment; only set what is missing.
os.environ.setdefault("SUPABASE_URL", "http://127.0.0.1:54321")
os.environ.setdefault("SUPABASE_ANON_KEY", "anon-test-key")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

# Valid UUIDs — is_bot_user short-circuits a non-UUID before any network call, so
# the principals under test must be well-formed to exercise the query path.
WORKSPACE = "33333333-3333-3333-3333-333333333333"
BOT = "11111111-1111-1111-1111-111111111111"
HUMAN = "22222222-2222-2222-2222-222222222222"
DOC = "doc-1"
BOT_EMAIL = "support-bot-abc123@bots.support.internal"
HUMAN_EMAIL = "teammate@example.com"
CREATED_AT = "2026-06-29T00:00:00+00:00"


def _import_main():
    try:
        import main  # noqa: E402

        return main
    except Exception as e:  # pragma: no cover - environment-dependent
        print(f"SKIP: cannot import backend app ({e})")
        return None


# --------------------------------------------------------------------------- #
# Unit layer — the bot-identity lookups + the normal-grant-box refusal, via
# MockTransport. No DB, no app HTTP.
# --------------------------------------------------------------------------- #
def _run_unit(main) -> int:
    import httpx

    import support_bot

    checks = 0

    def _boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected network call to {request.url}")

    def _membership_handler(request: httpx.Request) -> httpx.Response:
        # workspace_membership?...&is_bot=eq.true — one row for the bot, none else.
        url = str(request.url)
        if f"eq.{BOT}" in url or ("workspace_id" in url and "is_bot=eq.true" in url):
            return httpx.Response(200, json=[{"user_id": BOT}])
        return httpx.Response(200, json=[])

    def _empty_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    async def _call_is_bot(uid: object, handler) -> bool:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await support_bot.is_bot_user(
                uid, http=http, supabase_url="http://x", service_role_key="k"
            )

    # ---- is_bot_user: non-UUID short-circuits to False with NO network. ----
    assert asyncio.run(_call_is_bot("not-a-uuid", _boom)) is False, (
        "a non-UUID id cannot be a bot and must never hit the network"
    )
    assert asyncio.run(_call_is_bot(None, _boom)) is False
    checks += 1

    # ---- is_bot_user: a matching is_bot row → True; a plain user → False. ----
    captured: dict = {}

    def _capture_membership(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return _membership_handler(request)

    assert asyncio.run(_call_is_bot(BOT, _capture_membership)) is True
    assert "is_bot=eq.true" in captured["url"], (
        "the lookup must filter on the is_bot flag (the single source of truth)"
    )
    assert f"user_id=eq.{BOT}" in captured["url"]
    assert asyncio.run(_call_is_bot(HUMAN, _empty_handler)) is False
    checks += 1

    # ---- find_workspace_bot: resolves the workspace's bot id, None when absent. ----
    async def _call_find(handler) -> str | None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await support_bot.find_workspace_bot(
                WORKSPACE, http=http, supabase_url="http://x", service_role_key="k"
            )

    assert asyncio.run(_call_find(_membership_handler)) == BOT
    assert asyncio.run(_call_find(_empty_handler)) is None, (
        "no is_bot row → None (support not enabled), never a provision"
    )
    checks += 1

    # ---- _reject_if_bot_principal: 403 for a bot, no-op for a group / unconfigured. ----
    orig_key = main.SUPABASE_SERVICE_ROLE_KEY
    main.SUPABASE_SERVICE_ROLE_KEY = "service-test-key"  # type: ignore[assignment]
    try:
        # a group principal never queries (no-op) — _boom would fire if it did.
        async def _group() -> None:
            async with httpx.AsyncClient(transport=httpx.MockTransport(_boom)) as http:
                await main._reject_if_bot_principal(http, "group", HUMAN)

        asyncio.run(_group())

        # a bot user principal → HTTPException(403).
        from fastapi import HTTPException

        async def _bot() -> None:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(_membership_handler)
            ) as http:
                await main._reject_if_bot_principal(http, "user", BOT)

        raised_status = None
        try:
            asyncio.run(_bot())
        except HTTPException as e:
            raised_status = e.status_code
        assert raised_status == 403, (
            "the bot must be refused (403) in the normal share box, never granted"
        )

        # a plain user principal → no-op.
        async def _human() -> None:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(_empty_handler)
            ) as http:
                await main._reject_if_bot_principal(http, "user", HUMAN)

        asyncio.run(_human())
        checks += 1

        # unconfigured (no service role key) → no-op even for a would-be bot id.
        main.SUPABASE_SERVICE_ROLE_KEY = ""  # type: ignore[assignment]

        async def _unconfigured() -> None:
            async with httpx.AsyncClient(transport=httpx.MockTransport(_boom)) as http:
                await main._reject_if_bot_principal(http, "user", BOT)

        asyncio.run(_unconfigured())
        checks += 1
    finally:
        main.SUPABASE_SERVICE_ROLE_KEY = orig_key  # type: ignore[assignment]

    print(f"  ({checks} unit checks passed)")
    return checks


# --------------------------------------------------------------------------- #
# HTTP-surface layer — the real ASGI stack via TestClient (every DB helper +
# the bot-identity lookups mocked). Encodes the PRD US-086 Validation Test.
# --------------------------------------------------------------------------- #
def _run_http_surface(main) -> int:
    from fastapi.testclient import TestClient

    import support_bot
    from permissions import ShareSummary

    user = main.AuthedUser(id="owner-1", access_token="jwt-owner")

    state: dict = {
        "doc": {
            "id": DOC,
            "user_id": "owner-1",
            "status": "ready",
            "workspace_id": WORKSPACE,
        },
        "support_enabled": True,
        "grant_calls": [],
        "revoke_calls": [],
        "shares": [],
    }

    async def fake_assert_owner(http, u, doc_id):  # noqa: ANN001
        return dict(state["doc"], id=doc_id)

    async def fake_resolve_principal(http, headers, identifier, ws):  # noqa: ANN001
        if identifier == BOT_EMAIL:
            return ("user", BOT, BOT_EMAIL)
        if identifier == HUMAN_EMAIL:
            return ("user", HUMAN, HUMAN_EMAIL)
        return None

    async def fake_is_bot_user(user_id, **kw):  # noqa: ANN001
        return str(user_id) == BOT

    async def fake_find_workspace_bot(ws, **kw):  # noqa: ANN001
        return BOT if state["support_enabled"] else None

    async def fake_grant(http, url, headers, doc_id, ptype, pid, granted_by):  # noqa: ANN001
        state["grant_calls"].append((ptype, pid))
        return 1

    async def fake_revoke(http, url, headers, doc_id, ptype, pid):  # noqa: ANN001
        state["revoke_calls"].append((ptype, pid))
        return 1

    async def fake_list_shares(http, url, headers, doc_id):  # noqa: ANN001
        return list(state["shares"])

    async def fake_principal_has_grant(http, url, headers, doc_id, ptype, pid):  # noqa: ANN001
        return any(
            s.principal_type == ptype and s.principal_id == pid
            for s in state["shares"]
        )

    orig = {
        "assert": main._assert_doc_owner,
        "resolve": main._resolve_principal,
        "grant": main.grant_doc_to_principal,
        "revoke": main.revoke_doc_from_principal,
        "list": main.list_doc_shares,
        "hasgrant": main.principal_has_doc_grant,
        "isbot": support_bot.is_bot_user,
        "findbot": support_bot.find_workspace_bot,
        "key": main.SUPABASE_SERVICE_ROLE_KEY,
    }
    main._assert_doc_owner = fake_assert_owner  # type: ignore[assignment]
    main._resolve_principal = fake_resolve_principal  # type: ignore[assignment]
    main.grant_doc_to_principal = fake_grant  # type: ignore[assignment]
    main.revoke_doc_from_principal = fake_revoke  # type: ignore[assignment]
    main.list_doc_shares = fake_list_shares  # type: ignore[assignment]
    main.principal_has_doc_grant = fake_principal_has_grant  # type: ignore[assignment]
    support_bot.is_bot_user = fake_is_bot_user  # type: ignore[assignment]
    support_bot.find_workspace_bot = fake_find_workspace_bot  # type: ignore[assignment]
    main.SUPABASE_SERVICE_ROLE_KEY = "service-test-key"  # type: ignore[assignment]
    main.app.dependency_overrides[main.get_user] = lambda: user

    total = 0
    try:
        client = TestClient(main.app)

        # ===== STEP 1: the bot email in the NORMAL box is refused, no grant. =====
        state["grant_calls"].clear()
        r = client.post(
            f"/api/documents/{DOC}/share",
            json={"principal_email_or_name": BOT_EMAIL},
        )
        assert r.status_code == 403, f"bot email must be refused (got {r.status_code} {r.text})"
        assert state["grant_calls"] == [], (
            "FAILURE INDICATOR: typing the bot email in the normal box must NOT "
            "silently share to the public widget"
        )
        total += 1
        print("  step1: bot email in the normal share box → 403, zero chunk_acl grants")

        # positive control: a real teammate resolves + grants normally.
        r = client.post(
            f"/api/documents/{DOC}/share",
            json={"principal_email_or_name": HUMAN_EMAIL},
        )
        assert r.status_code == 200, f"a normal grant must still succeed ({r.text})"
        assert ("user", HUMAN) in state["grant_calls"], "the human grant went through"
        total += 1
        print("  step1: a real teammate still grants normally (positive control)")

        # ===== STEP 2: the explicit publish-to-bot action grants the bot. =====
        state["grant_calls"].clear()
        r = client.post(f"/api/documents/{DOC}/publish-to-bot")
        assert r.status_code == 200, f"publish must succeed ({r.text})"
        assert r.json() == {"published": True, "bot_provisioned": True}
        assert state["grant_calls"] == [("user", BOT)], (
            "publish grants the bot as a `user` principal via the same chunk_acl path"
        )
        total += 1
        print("  step2: explicit POST /publish-to-bot grants the bot (one user-principal grant)")

        # ===== The bot is NEVER a quiet grantee row: GET /shares filters it. =====
        state["shares"] = [
            ShareSummary(
                principal_type="user",
                principal_id=BOT,
                display_name=BOT_EMAIL,
                granted_at=CREATED_AT,
            ),
            ShareSummary(
                principal_type="user",
                principal_id=HUMAN,
                display_name=HUMAN_EMAIL,
                granted_at=CREATED_AT,
            ),
        ]
        r = client.get(f"/api/documents/{DOC}/shares")
        assert r.status_code == 200
        listed = [s["principal_id"] for s in r.json()["shares"]]
        assert BOT not in listed, (
            "FAILURE INDICATOR: the bot must NOT appear as a normal grantee row"
        )
        assert HUMAN in listed, "human grants are still listed"
        total += 1
        print("  shares: the bot is filtered from the normal list; the human grant remains")

        # ===== A transient bot-lookup blip must NOT break the core share read. =====
        async def _boom(http, ws):  # noqa: ANN001
            raise RuntimeError("transient PostgREST error resolving the bot")

        orig_resolve = main._resolve_workspace_bot_id
        main._resolve_workspace_bot_id = _boom  # type: ignore[assignment]
        try:
            r = client.get(f"/api/documents/{DOC}/shares")
        finally:
            main._resolve_workspace_bot_id = orig_resolve  # type: ignore[assignment]
        assert r.status_code == 200, (
            "a bot-lookup error must fail SOFT (unfiltered list), never 500 the read"
        )
        listed = [s["principal_id"] for s in r.json()["shares"]]
        assert HUMAN in listed and BOT in listed, (
            "fail-soft leaves the list UNFILTERED (a momentary bot row is acceptable)"
        )
        total += 1
        print("  shares: a transient bot-lookup error fails soft (200, unfiltered) not 500")

        # ===== Published state is reported by GET /publish-to-bot only. =====
        r = client.get(f"/api/documents/{DOC}/publish-to-bot")
        assert r.status_code == 200
        assert r.json() == {"published": True, "bot_provisioned": True}
        total += 1
        print("  status: GET /publish-to-bot reports published=true")

        # ===== DELETE unpublishes (revokes the bot's grants). =====
        state["revoke_calls"].clear()
        r = client.delete(f"/api/documents/{DOC}/publish-to-bot")
        assert r.status_code == 204
        assert state["revoke_calls"] == [("user", BOT)], "unpublish revokes the bot's grants"
        total += 1
        print("  unpublish: DELETE /publish-to-bot revokes the bot's chunk_acl grants (204)")

        # ===== A bot-less workspace: publish is a clean 409, never a silent provision. =====
        state["support_enabled"] = False
        state["grant_calls"].clear()
        r = client.post(f"/api/documents/{DOC}/publish-to-bot")
        assert r.status_code == 409, f"publishing without a bot must 409 (got {r.status_code})"
        assert state["grant_calls"] == [], "no grant when support is not enabled"
        r = client.get(f"/api/documents/{DOC}/publish-to-bot")
        assert r.json() == {"published": False, "bot_provisioned": False}
        total += 1
        print("  no-support: publish → 409, status reports bot_provisioned=false (no silent provision)")

        # ===== publish requires a ready document. =====
        state["support_enabled"] = True
        state["doc"] = dict(state["doc"], status="processing")
        r = client.post(f"/api/documents/{DOC}/publish-to-bot")
        assert r.status_code == 409, "an un-ingested doc cannot be published"
        total += 1
        print("  guard: publishing an un-ingested doc → 409")
    finally:
        main._assert_doc_owner = orig["assert"]  # type: ignore[assignment]
        main._resolve_principal = orig["resolve"]  # type: ignore[assignment]
        main.grant_doc_to_principal = orig["grant"]  # type: ignore[assignment]
        main.revoke_doc_from_principal = orig["revoke"]  # type: ignore[assignment]
        main.list_doc_shares = orig["list"]  # type: ignore[assignment]
        main.principal_has_doc_grant = orig["hasgrant"]  # type: ignore[assignment]
        support_bot.is_bot_user = orig["isbot"]  # type: ignore[assignment]
        support_bot.find_workspace_bot = orig["findbot"]  # type: ignore[assignment]
        main.SUPABASE_SERVICE_ROLE_KEY = orig["key"]  # type: ignore[assignment]
        main.app.dependency_overrides.pop(main.get_user, None)

    print(f"  ({total} http-surface checks passed)")
    return total


def _run() -> None:
    main = _import_main()
    print("US-086: share-to-bot as a separate, explicitly-confirmed publish action")
    print("- unit layer:")
    # The unit layer only needs the two modules, not the full app import; but the
    # app import is what everything shares, so gate both on it for a clean skip.
    if main is None:
        return
    _run_unit(main)
    print("- http-surface layer:")
    _run_http_surface(main)
    print("US-086: OK")


def main_entry() -> None:
    _run()


if __name__ == "__main__":
    main_entry()
