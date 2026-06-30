"""US-082: the agent-reply endpoint (`POST /widget/conversations/{id}/agent-reply`).

A workspace AGENT (a human on the operator dashboard) replies into a support
conversation under their REAL Supabase JWT, the write is gated by the US-066
workspace-membership RLS, and the reply is fanned to the customer's live backend
SSE (US-081) through the in-process registry — never Supabase Realtime.

Four layers, the same shape as the other support-surface tests
(`test_us080_escalation_latch.py`, `test_us079_deflection_streaming.py`):

  * a REGISTRY unit layer (always runs, no app / no DB — imports
    `conversation_fanout` directly): `publish` delivers ONLY to subscribers of the
    SAME conversation (the US-081 cross-conversation-isolation property US-082
    relies on), returns 0 with no subscriber (the reply is still durable in the
    transcript), the `subscribe` context manager registers then unregisters, a
    full subscriber queue drops without raising, and `message_event` carries only
    customer-safe fields (no workspace topology).

  * a MAIN-HELPER unit layer (needs the app import): `_insert_agent_reply` writes
    UNDER THE AGENT'S JWT (Authorization is the agent bearer, NOT the service role),
    with `role='assistant'` (the schema's only support-side role) and no
    `tool_calls`; `_fetch_conversation_for_agent` returns 404 on 0 rows
    (RLS-hidden); and the US-074 path partition treats the route as the
    AUTHENTICATED exception. Pinned via httpx `MockTransport`, no real network.

  * a HANDLER / FAN-OUT layer (calls the endpoint coroutine on ONE event loop so a
    real `subscribe`/`publish` round-trips): A1's member reply is written and FANS
    OUT to C's customer SSE while a subscriber on another conversation receives
    NOTHING; a cross-workspace A2 (whose membership read 404s) writes 0 rows and
    fans out nothing.

  * an HTTP-surface layer (FastAPI TestClient, every DB helper + the fan-out
    mocked): missing bearer → 401, empty/whitespace content → 400, missing field →
    422, a member reply on an ESCALATED conversation → 200 + exactly one fan-out
    publish, a cross-workspace reply → 404 with 0 writes and 0 fan-out.

The actual cross-workspace zero-leak boundary (the RLS itself) is pinned at the DB
layer by `backend/test_us066_conversations_rls.py`; here the membership read is
mocked, so these layers pin the ENDPOINT WIRING around that boundary (write under
the agent's JWT, 404-on-RLS-hidden, fan-out only on success).

Run:
    python -m backend.test_us082_agent_reply
"""

from __future__ import annotations

import asyncio
import json
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

WORKSPACE = "ws-uuid-1"
CONV = "conv-uuid-1"
OTHER_CONV = "conv-uuid-2"
AGENT_JWT = "agent-real-jwt-token"  # the agent's REAL Supabase session token
REPLY = "Happy to help — I've reset your account; try logging in again now."
CREATED_AT = "2026-06-29T00:00:00+00:00"


def _import_main():
    try:
        import main  # noqa: E402

        return main
    except Exception as e:  # pragma: no cover - environment-dependent
        print(f"SKIP: cannot import backend app ({e})")
        return None


# --------------------------------------------------------------------------- #
# Registry unit layer — the in-process fan-out core (no app / no DB).
# --------------------------------------------------------------------------- #
def _run_registry_unit() -> int:
    from conversation_fanout import ConversationFanout, message_event

    checks = 0

    async def _scenario() -> None:
        nonlocal checks
        fan = ConversationFanout()

        # Cross-conversation isolation: a publish to C reaches C's subscriber and
        # NEVER another conversation's subscriber (the US-081 security property).
        async with fan.subscribe(CONV) as qc, fan.subscribe(OTHER_CONV) as qother:
            assert fan.subscriber_count(CONV) == 1
            assert fan.conversation_count == 2
            delivered = fan.publish(CONV, {"type": "message", "message": {"id": "m1"}})
            assert delivered == 1, "delivered to exactly the one CONV subscriber"
            evt = qc.get_nowait()
            assert evt["message"]["id"] == "m1"
            assert qother.empty(), (
                "FAILURE INDICATOR: a subscriber on another conversation must NEVER "
                "receive C's event (US-081 cross-conversation isolation)"
            )
        checks += 1

        # The context manager unregisters on exit — no leaked subscriber, no
        # per-conversation state for an idle backend.
        assert fan.subscriber_count(CONV) == 0
        assert fan.conversation_count == 0
        checks += 1

        # A publish with no open SSE delivers to 0 (the reply is still durable in the
        # transcript; the customer recovers it on reconnect).
        assert fan.publish(CONV, {"x": 1}) == 0
        checks += 1

        # Two subscribers on one conversation both receive.
        async with fan.subscribe(CONV) as q1, fan.subscribe(CONV) as q2:
            assert fan.publish(CONV, {"type": "message", "message": {}}) == 2
            assert not q1.empty() and not q2.empty()
        checks += 1

        # A full subscriber queue drops the live push rather than raising/blocking
        # (a stuck SSE cannot stall the agent's write path).
        small = ConversationFanout(max_queue=1)
        async with small.subscribe(CONV) as q:
            assert small.publish(CONV, {"n": 1}) == 1
            assert small.publish(CONV, {"n": 2}) == 0, (
                "a full queue drops the second push (delivered=0), never raises"
            )
            assert q.get_nowait() == {"n": 1} and q.empty()
        checks += 1

    asyncio.run(_scenario())

    # message_event: customer-safe fields only — no workspace topology leaks.
    evt = message_event(
        {
            "id": "m9",
            "conversation_id": CONV,
            "role": "assistant",
            "content": REPLY,
            "created_at": CREATED_AT,
            "workspace_id": WORKSPACE,
            "bot_user_id": "bot-uuid",
        }
    )
    assert evt["type"] == "message"
    assert evt["message"] == {
        "id": "m9",
        "role": "assistant",
        "content": REPLY,
        "created_at": CREATED_AT,
    }, "the envelope exposes exactly id/role/content/created_at"
    assert "workspace_id" not in evt["message"] and "bot_user_id" not in evt["message"], (
        "FAILURE INDICATOR: the fan-out envelope must not leak workspace topology"
    )
    checks += 1

    print("  registry: publish isolates by conversation_id; subscribe lifecycle clean")
    print("  registry: 0-subscriber publish returns 0; full queue drops without raising")
    print("  registry: message_event exposes only customer-safe fields (no topology)")
    return checks


# --------------------------------------------------------------------------- #
# Main-helper unit layer — the agent-JWT write + RLS-hidden 404, via MockTransport.
# --------------------------------------------------------------------------- #
def _run_main_unit(main) -> int:
    import httpx
    from fastapi import HTTPException

    checks = 0
    user = main.AuthedUser(id="agent-1", access_token=AGENT_JWT)

    # ---- _insert_agent_reply: POST under the AGENT'S JWT, role='assistant', no tool_calls.
    captured: dict = {}

    def _insert_handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            201,
            json=[
                {
                    "id": "m1",
                    "conversation_id": CONV,
                    "role": "assistant",
                    "content": REPLY,
                    "created_at": CREATED_AT,
                }
            ],
        )

    async def _do_insert() -> dict:
        transport = httpx.MockTransport(_insert_handler)
        async with httpx.AsyncClient(transport=transport) as http:
            return await main._insert_agent_reply(
                http, user, conversation_id=CONV, content=REPLY
            )

    row = asyncio.run(_do_insert())
    assert captured["method"] == "POST"
    assert "/rest/v1/conversation_messages" in captured["url"]
    assert captured["auth"] == f"Bearer {AGENT_JWT}", (
        "FAILURE INDICATOR: the reply must be written UNDER THE AGENT'S OWN JWT "
        "(the membership RLS is the authorization), never the service role"
    )
    assert captured["json"]["role"] == "assistant", (
        "role='assistant' — the only support-side role the US-066 CHECK allows "
        "(there is no separate 'agent' role)"
    )
    assert captured["json"]["conversation_id"] == CONV
    assert captured["json"]["content"] == REPLY
    assert "tool_calls" not in captured["json"], (
        "a human reply is not the agentic tool loop — tool_calls stays null/absent"
    )
    assert row["id"] == "m1"
    checks += 1
    print("  main: _insert_agent_reply writes under the agent's JWT, role='assistant', no tool_calls")

    # ---- _fetch_conversation_for_agent: 0 rows (RLS-hidden) → 404; a row → returned.
    def _empty_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    async def _do_fetch_empty() -> dict:
        transport = httpx.MockTransport(_empty_handler)
        async with httpx.AsyncClient(transport=transport) as http:
            return await main._fetch_conversation_for_agent(http, user, CONV)

    try:
        asyncio.run(_do_fetch_empty())
        raise AssertionError("a non-member / absent conversation must 404")
    except HTTPException as e:
        assert e.status_code == 404, (
            "FAILURE INDICATOR: an RLS-hidden (cross-workspace) conversation must "
            "be indistinguishable from not-found (404), leaking nothing"
        )
    checks += 1

    fetch_auth: dict = {}

    def _row_handler(request: httpx.Request) -> httpx.Response:
        fetch_auth["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=[{"id": CONV, "status": "escalated"}])

    async def _do_fetch_row() -> dict:
        transport = httpx.MockTransport(_row_handler)
        async with httpx.AsyncClient(transport=transport) as http:
            return await main._fetch_conversation_for_agent(http, user, CONV)

    conv = asyncio.run(_do_fetch_row())
    assert conv["status"] == "escalated"
    assert fetch_auth["auth"] == f"Bearer {AGENT_JWT}", (
        "the membership read also runs under the agent's JWT (RLS is the gate)"
    )
    checks += 1
    print("  main: _fetch_conversation_for_agent 404s on RLS-hidden rows, reads under the agent's JWT")

    # ---- US-074 partition: the agent-reply route is the AUTHENTICATED exception.
    assert not main._is_widget_public_path(
        f"/widget/conversations/{CONV}/agent-reply"
    ), (
        "FAILURE INDICATOR: the agent-reply route must fall to the AUTHENTICATED "
        "CORS posture (US-074) so the dashboard's app origin is not blocked"
    )
    assert main._is_widget_public_path("/widget/conversations/messages"), (
        "(control) the anonymous-customer message route stays public-widget"
    )
    checks += 1
    print("  main: the agent-reply route is the US-074 authenticated exception (not public-widget)")
    return checks


# --------------------------------------------------------------------------- #
# Handler / fan-out layer — the endpoint coroutine on one loop, real subscribe/publish.
# --------------------------------------------------------------------------- #
def _run_handler_fanout(main) -> int:
    from fastapi import HTTPException

    checks = 0
    user = main.AuthedUser(id="agent-1", access_token=AGENT_JWT)

    state = {"insert_calls": 0, "fetch_404": False, "status": "escalated"}

    async def fake_fetch(http, u, conversation_id):
        if state["fetch_404"]:
            raise HTTPException(status_code=404, detail="conversation not found")
        return {"id": conversation_id, "status": state["status"]}

    async def fake_insert(http, u, *, conversation_id, content):
        state["insert_calls"] += 1
        return {
            "id": "m-1",
            "conversation_id": conversation_id,
            "role": "assistant",
            "content": content,
            "created_at": CREATED_AT,
        }

    orig_fetch = main._fetch_conversation_for_agent
    orig_insert = main._insert_agent_reply
    main._fetch_conversation_for_agent = fake_fetch  # type: ignore[assignment]
    main._insert_agent_reply = fake_insert  # type: ignore[assignment]
    try:

        async def _scenario() -> None:
            nonlocal checks
            fan = main._CONVERSATION_FANOUT

            # ===== A1 member: reply written + fanned to C's SSE; another
            # conversation's subscriber receives NOTHING. =====
            state["fetch_404"] = False
            state["insert_calls"] = 0
            async with fan.subscribe(CONV) as qc, fan.subscribe(OTHER_CONV) as qother:
                resp = await main.widget_agent_reply(
                    CONV, main.AgentReplyRequest(content=REPLY), user=user
                )
                assert resp["message"]["content"] == REPLY
                assert resp["message"]["role"] == "assistant"
                assert state["insert_calls"] == 1
                evt = qc.get_nowait()
                assert evt["type"] == "message" and evt["message"]["content"] == REPLY, (
                    "FAILURE INDICATOR: A1's reply must fan out to C's customer SSE"
                )
                assert qother.empty(), (
                    "FAILURE INDICATOR: the reply must NEVER reach another "
                    "conversation's SSE (US-081 isolation)"
                )
            checks += 1

            # ===== A2 cross-workspace: membership read 404s → no write, no fan-out. =====
            state["fetch_404"] = True
            state["insert_calls"] = 0
            async with fan.subscribe(CONV) as qc:
                try:
                    await main.widget_agent_reply(
                        CONV, main.AgentReplyRequest(content=REPLY), user=user
                    )
                    raise AssertionError("a cross-workspace agent must be rejected")
                except HTTPException as e:
                    assert e.status_code == 404
                assert state["insert_calls"] == 0, (
                    "FAILURE INDICATOR: a rejected cross-workspace reply writes 0 rows"
                )
                assert qc.empty(), (
                    "FAILURE INDICATOR: a rejected reply must not fan out to the customer"
                )
            checks += 1

        asyncio.run(_scenario())
    finally:
        main._fetch_conversation_for_agent = orig_fetch  # type: ignore[assignment]
        main._insert_agent_reply = orig_insert  # type: ignore[assignment]

    print("  handler: A1's member reply writes + fans out to C only (not other conversations)")
    print("  handler: A2's cross-workspace reply is rejected — 0 rows, 0 fan-out")
    return checks


# --------------------------------------------------------------------------- #
# HTTP-surface layer — the real ASGI stack via TestClient (DB + fan-out mocked).
# --------------------------------------------------------------------------- #
def _run_http_surface(main) -> int:
    from fastapi import HTTPException
    from fastapi.testclient import TestClient

    user = main.AuthedUser(id="agent-1", access_token=AGENT_JWT)

    class _RecordingFanout:
        """A fan-out stand-in so the TestClient layer asserts publish TRIGGERING
        without crossing event loops (the real registry's queues live on the app's
        loop; a recording fake sidesteps that — real delivery is covered above)."""

        def __init__(self) -> None:
            self.published: list[tuple[str, dict]] = []

        def publish(self, conversation_id: str, event: dict) -> int:
            self.published.append((conversation_id, event))
            return 1

    state = {"insert_calls": 0, "fetch_404": False, "status": "active"}

    async def fake_fetch(http, u, conversation_id):
        if state["fetch_404"]:
            raise HTTPException(status_code=404, detail="conversation not found")
        return {"id": conversation_id, "status": state["status"]}

    async def fake_insert(http, u, *, conversation_id, content):
        state["insert_calls"] += 1
        return {
            "id": "m-1",
            "conversation_id": conversation_id,
            "role": "assistant",
            "content": content,
            "created_at": CREATED_AT,
        }

    rec = _RecordingFanout()
    orig_fetch = main._fetch_conversation_for_agent
    orig_insert = main._insert_agent_reply
    orig_fanout = main._CONVERSATION_FANOUT
    main._fetch_conversation_for_agent = fake_fetch  # type: ignore[assignment]
    main._insert_agent_reply = fake_insert  # type: ignore[assignment]
    main._CONVERSATION_FANOUT = rec  # type: ignore[assignment]

    total = 0
    try:
        client = TestClient(main.app)

        # ---- missing bearer → 401 (the REAL get_user runs; no network: it rejects on
        #      the absent header before any auth call). ----
        r = client.post(
            f"/widget/conversations/{CONV}/agent-reply", json={"content": REPLY}
        )
        assert r.status_code == 401, f"missing bearer must 401 (got {r.status_code})"
        total += 1
        print("  http: missing bearer → 401 (authed by the agent's real JWT)")

        # Authenticate as A1 for the rest.
        main.app.dependency_overrides[main.get_user] = lambda: user
        try:
            # ---- whitespace content → 400 (no write, no fan-out). ----
            state["fetch_404"] = False
            state["insert_calls"] = 0
            rec.published.clear()
            r = client.post(
                f"/widget/conversations/{CONV}/agent-reply", json={"content": "   "}
            )
            assert r.status_code == 400, f"whitespace content must 400 (got {r.status_code})"
            assert state["insert_calls"] == 0 and not rec.published
            total += 1

            # ---- missing content field → 422 (pydantic). ----
            r = client.post(f"/widget/conversations/{CONV}/agent-reply", json={})
            assert r.status_code == 422, f"missing content must 422 (got {r.status_code})"
            total += 1
            print("  http: empty content → 400, missing field → 422 (no write/fan-out)")

            # ---- A1 member reply on an ESCALATED conversation → 200 + ONE fan-out. ----
            state["status"] = "escalated"
            state["insert_calls"] = 0
            rec.published.clear()
            r = client.post(
                f"/widget/conversations/{CONV}/agent-reply", json={"content": REPLY}
            )
            assert r.status_code == 200, f"member reply must 200 (got {r.status_code}: {r.text})"
            body = r.json()["message"]
            assert body["role"] == "assistant"
            assert body["content"] == REPLY
            assert body["conversation_id"] == CONV
            assert state["insert_calls"] == 1, "the reply is written exactly once"
            assert len(rec.published) == 1 and rec.published[0][0] == CONV, (
                "FAILURE INDICATOR: a member reply must fan out (publish) for C"
            )
            assert rec.published[0][1]["type"] == "message", (
                "the published envelope is a message event"
            )
            total += 1
            print(
                "  http: a member reply on an ESCALATED conversation → 200 + exactly "
                "one fan-out (the agent is the only post-latch message source, AC3)"
            )

            # ---- A2 cross-workspace (membership read 404s) → 404, 0 writes, 0 fan-out. ----
            state["fetch_404"] = True
            state["insert_calls"] = 0
            rec.published.clear()
            r = client.post(
                f"/widget/conversations/{CONV}/agent-reply", json={"content": REPLY}
            )
            assert r.status_code == 404, f"cross-workspace reply must 404 (got {r.status_code})"
            assert state["insert_calls"] == 0, (
                "FAILURE INDICATOR: a cross-workspace reply writes 0 rows"
            )
            assert not rec.published, (
                "FAILURE INDICATOR: a cross-workspace reply must not fan out"
            )
            total += 1
            print("  http: a cross-workspace reply → 404, 0 rows written, 0 fan-out")
        finally:
            main.app.dependency_overrides.pop(main.get_user, None)

        print(
            f"OK: US-082 HTTP surface passed — {total} scenarios; a member writes "
            "(under their JWT) + fans out, a cross-workspace agent is rejected with "
            "0 rows, replies are permitted on escalated conversations"
        )
        return total
    finally:
        main._fetch_conversation_for_agent = orig_fetch  # type: ignore[assignment]
        main._insert_agent_reply = orig_insert  # type: ignore[assignment]
        main._CONVERSATION_FANOUT = orig_fanout  # type: ignore[assignment]


def _run() -> None:
    reg = _run_registry_unit()
    print(f"  ({reg} registry unit checks passed)")
    main = _import_main()
    if main is None:
        return
    mu = _run_main_unit(main)
    print(f"  ({mu} main-helper unit checks passed)")
    hf = _run_handler_fanout(main)
    print(f"  ({hf} handler/fan-out checks passed)")
    _run_http_surface(main)


def main_entry() -> None:
    _run()


if __name__ == "__main__":
    main_entry()
