"""US-081: the customer SSE channel + multi-instance LISTEN/NOTIFY fan-out.

The anonymous customer holds a long-lived BACKEND SSE
(`GET /widget/conversations/{id}/events`), authorized by the US-071 OPAQUE token
(NOT a Supabase JWT), over which async AGENT replies (US-082) are pushed — so the
customer is structurally OFF the Supabase Realtime/JWT surface (ADR-0008). Delivery
is the in-process `ConversationFanout` registry (US-082) plus, for a multi-instance
deployment, a Postgres LISTEN/NOTIFY bridge that publishes INTO that same registry.

Layers (the shape of the other support-surface tests, e.g. `test_us082_agent_reply`):

  * a BRIDGE unit layer (always runs, no DB/app — imports `conversation_bridge`):
    the cross-instance envelope round-trips and rejects malformed frames; the LISTEN
    callback REPLAYS a remote event into the local registry but IGNORES a frame this
    instance emitted (the dedup that prevents a double push on the originating
    instance) and any malformed/foreign frame.

  * an ENDPOINT/STREAM layer (needs the app import; no real network — the token
    resolve + the per-session limit are mocked): the binding (a token for X cannot
    open Y → 401), the validation test (X's stream receives ONLY X's reply, NEVER
    Y's — the cross-conversation isolation), and close-on-invalidation (a resolved
    token re-validation closes the stream with an `event: close`).

  * an HTTP-surface layer (FastAPI TestClient): a missing token → 401 and a
    token bound to a different conversation → 401, both before any stream opens; and
    the US-074 partition treats the customer events route as the PUBLIC-widget
    (anonymous, opaque-token) surface — never the JWT-authenticated exception.

Run:
    python -m backend.test_us081_customer_sse
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

# The app reads SUPABASE_* / a provider key at import time. Supply local defaults so
# the import succeeds without a real deployment; only set what is missing.
os.environ.setdefault("SUPABASE_URL", "http://127.0.0.1:54321")
os.environ.setdefault("SUPABASE_ANON_KEY", "anon-test-key")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

CONV_X = "conv-uuid-x"
CONV_Y = "conv-uuid-y"
TOKEN_X = "raw-opaque-token-x"
CREATED_AT = "2026-06-29T00:00:00+00:00"
REPLY_X = "Hi — an agent here. I've reset your password; try again now."
REPLY_Y = "A different conversation's reply that must NEVER reach X."


def _import_main():
    try:
        import main  # noqa: E402

        return main
    except Exception as e:  # pragma: no cover - environment-dependent
        print(f"SKIP: cannot import backend app ({e})")
        return None


def _decode(frame: bytes) -> tuple[str, dict]:
    """Parse one SSE frame (bytes) into (event_name, data_dict)."""
    text = frame.decode("utf-8")
    event = "message"
    data_lines: list[str] = []
    for line in text.split("\n"):
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())
    data = json.loads("\n".join(data_lines)) if data_lines else {}
    return event, data


def _is_keepalive(frame: bytes) -> bool:
    return frame.strip() == b": keepalive"


# --------------------------------------------------------------------------- #
# Bridge unit layer — the cross-instance envelope + dedup (no DB / no app).
# --------------------------------------------------------------------------- #
def _run_bridge_unit() -> int:
    from conversation_bridge import (
        ConversationBridge,
        build_notify_payload,
        decode_notify_payload,
    )
    from conversation_fanout import ConversationFanout, message_event

    checks = 0

    # ---- payload round-trips; malformed frames decode to None (never raise). ----
    event = message_event(
        {"id": "m1", "role": "assistant", "content": REPLY_X, "created_at": CREATED_AT}
    )
    payload = build_notify_payload(
        instance_id="inst-A", conversation_id=CONV_X, event=event
    )
    decoded = decode_notify_payload(payload)
    assert decoded == {
        "origin": "inst-A",
        "conversation_id": CONV_X,
        "event": event,
    }, "the envelope carries origin + conversation_id + the full event"
    assert decode_notify_payload("not json") is None
    assert decode_notify_payload("[1,2,3]") is None, "a non-object payload is rejected"
    checks += 1
    print("  bridge: notify payload round-trips; malformed/foreign frames decode to None")

    # ---- the LISTEN callback replays a REMOTE event into the local registry. ----
    async def _scenario() -> None:
        nonlocal checks
        fan = ConversationFanout()
        bridge = ConversationBridge(dsn="postgres://unused", fanout=fan, instance_id="inst-B")

        async with fan.subscribe(CONV_X) as qx, fan.subscribe(CONV_Y) as qy:
            # A notification from ANOTHER instance (inst-A) is replayed locally — and
            # ONLY to that conversation's subscriber.
            remote = build_notify_payload(
                instance_id="inst-A", conversation_id=CONV_X, event=event
            )
            bridge._on_notification(None, 0, bridge._channel, remote)
            assert not qx.empty(), (
                "a remote instance's reply must be replayed into the local registry"
            )
            got = qx.get_nowait()
            assert got["message"]["content"] == REPLY_X
            assert qy.empty(), (
                "FAILURE INDICATOR: a replayed remote event must reach ONLY its own "
                "conversation's subscriber (US-081 cross-conversation isolation)"
            )
            checks += 1

            # A notification this instance EMITTED (origin == our id) is IGNORED — the
            # local publish() at the call site already delivered it; replaying would
            # double-push.
            own = build_notify_payload(
                instance_id="inst-B", conversation_id=CONV_X, event=event
            )
            bridge._on_notification(None, 0, bridge._channel, own)
            assert qx.empty(), (
                "FAILURE INDICATOR: a notification from our OWN instance must be "
                "ignored on the LISTEN side (no double delivery on the originator)"
            )
            checks += 1

            # A malformed / wrong-shaped frame is dropped, never raised.
            bridge._on_notification(None, 0, bridge._channel, "garbage")
            bridge._on_notification(
                None, 0, bridge._channel,
                json.dumps({"origin": "inst-A", "conversation_id": CONV_X}),  # no event
            )
            assert qx.empty(), "a malformed/foreign frame delivers nothing and never raises"
            checks += 1

    asyncio.run(_scenario())
    print("  bridge: LISTEN replays a REMOTE event locally; IGNORES our own (dedup) + malformed")
    return checks


# --------------------------------------------------------------------------- #
# Helpers for the endpoint layer — a minimal Request + token-resolve stub.
# --------------------------------------------------------------------------- #
class _FakeRequest:
    """The slice of `starlette.Request` the events endpoint touches."""

    def __init__(self) -> None:
        self.headers = {"x-forwarded-for": "203.0.113.7"}
        self.client = None
        self._disconnected = False

    async def is_disconnected(self) -> bool:
        return self._disconnected


async def _anext_frame(body_iter, timeout: float = 3.0) -> bytes:
    return await asyncio.wait_for(body_iter.__anext__(), timeout=timeout)


async def _read_until(body_iter, event_name: str, timeout: float = 3.0) -> dict:
    """Read frames (skipping keepalives) until one with `event_name`; return its data."""
    while True:
        frame = await _anext_frame(body_iter, timeout=timeout)
        if _is_keepalive(frame):
            continue
        ev, data = _decode(frame)
        if ev == event_name:
            return data
        # Any other named event before the one we want is unexpected here.
        raise AssertionError(f"expected event '{event_name}', got '{ev}': {data}")


# --------------------------------------------------------------------------- #
# Endpoint / stream layer — binding 401, the validation test, close-on-resolve.
# --------------------------------------------------------------------------- #
def _run_endpoint_stream(main) -> int:
    from fastapi import HTTPException

    checks = 0

    # Neutralize the per-session limiter (it is None in tests anyway) and isolate the
    # token resolve. Restored in `finally`.
    orig_resume = main._resume_conversation_by_token
    orig_keepalive = main.WIDGET_SSE_KEEPALIVE_SECONDS
    orig_revalidate = main.WIDGET_SSE_REVALIDATE_SECONDS

    try:
        # ===== Binding: a token that resolves to ANOTHER conversation → 401. =====
        async def resume_other(http, raw, *, slide=True):
            return {"id": CONV_Y, "status": "active", "created_at": CREATED_AT}

        main._resume_conversation_by_token = resume_other  # type: ignore[assignment]

        async def _binding() -> None:
            try:
                await main.widget_conversation_events(
                    CONV_X, _FakeRequest(), x_conversation_token=TOKEN_X
                )
                raise AssertionError("a token bound to Y must not open X's stream")
            except HTTPException as e:
                assert e.status_code == 401, (
                    "FAILURE INDICATOR: a token for X opening Y (or vice versa) must "
                    "401 — the cross-conversation binding the validation test pins"
                )

        asyncio.run(_binding())
        checks += 1
        print("  endpoint: a token bound to another conversation → 401 (binding)")

        # A missing token → 401 before any resolve.
        async def _missing() -> None:
            try:
                await main.widget_conversation_events(
                    CONV_X, _FakeRequest(), x_conversation_token=None
                )
                raise AssertionError("missing token must 401")
            except HTTPException as e:
                assert e.status_code == 401

        asyncio.run(_missing())
        checks += 1

        # ===== Validation test: X's stream receives ONLY X's reply, never Y's. =====
        # Keepalive HIGH so no heartbeat frames intrude; the published item wakes the
        # stream immediately, making the frame sequence deterministic.
        main.WIDGET_SSE_KEEPALIVE_SECONDS = 30  # type: ignore[assignment]

        async def resume_x(http, raw, *, slide=True):
            assert raw == TOKEN_X
            assert slide is False, "the SSE binds with slide=False (a read is not activity)"
            return {"id": CONV_X, "status": "active", "created_at": CREATED_AT}

        main._resume_conversation_by_token = resume_x  # type: ignore[assignment]

        async def _isolation() -> None:
            nonlocal checks
            resp = await main.widget_conversation_events(
                CONV_X, _FakeRequest(), x_conversation_token=TOKEN_X
            )
            body = resp.body_iterator
            try:
                # First frame: `ready` (subscription is now registered).
                ev, data = _decode(await _anext_frame(body))
                assert ev == "ready" and data["conversation_id"] == CONV_X
                fan = main._CONVERSATION_FANOUT
                # Publish to ANOTHER conversation (Y) and to X. Y must never appear.
                fan.publish(CONV_Y, main.message_event(
                    {"id": "my", "role": "assistant", "content": REPLY_Y,
                     "created_at": CREATED_AT}))
                fan.publish(CONV_X, main.message_event(
                    {"id": "mx", "role": "assistant", "content": REPLY_X,
                     "created_at": CREATED_AT}))
                msg = await _read_until(body, "message")
                assert msg["content"] == REPLY_X and msg["id"] == "mx", (
                    "X's stream must deliver X's reply"
                )
                assert msg["content"] != REPLY_Y
                checks += 1
                # No further frame is available (Y never leaks; keepalive is far off).
                try:
                    leaked = await _anext_frame(body, timeout=0.4)
                    raise AssertionError(
                        f"FAILURE INDICATOR: X's stream must NOT receive another "
                        f"conversation's reply; leaked: {leaked!r}"
                    )
                except asyncio.TimeoutError:
                    pass
                checks += 1
            finally:
                await body.aclose()

        asyncio.run(_isolation())
        print(
            "  endpoint: X's SSE delivers ONLY X's reply; a Y reply NEVER reaches it "
            "(US-081 cross-conversation isolation — the validation test)"
        )

        # ===== Close-on-invalidation: a resolved token closes the stream. =====
        main.WIDGET_SSE_KEEPALIVE_SECONDS = 0.05  # type: ignore[assignment]
        main.WIDGET_SSE_REVALIDATE_SECONDS = 0  # revalidate on the first heartbeat

        calls = {"n": 0}

        async def resume_then_resolve(http, raw, *, slide=True):
            calls["n"] += 1
            if calls["n"] == 1:
                # The initial binding succeeds…
                return {"id": CONV_X, "status": "active", "created_at": CREATED_AT}
            # …then the conversation is resolved → its token is purged → None.
            return None

        main._resume_conversation_by_token = resume_then_resolve  # type: ignore[assignment]

        async def _close_on_resolve() -> None:
            nonlocal checks
            resp = await main.widget_conversation_events(
                CONV_X, _FakeRequest(), x_conversation_token=TOKEN_X
            )
            body = resp.body_iterator
            try:
                ev, _ = _decode(await _anext_frame(body))
                assert ev == "ready"
                data = await _read_until(body, "close", timeout=3.0)
                assert data.get("reason") == "resolved", (
                    "FAILURE INDICATOR: a resolved/expired token must close the SSE "
                    "(the connection closes when the token is invalidated)"
                )
                # After `close` the stream ends.
                try:
                    await _anext_frame(body, timeout=1.0)
                    raise AssertionError("the stream must end after `close`")
                except StopAsyncIteration:
                    pass
                checks += 1
            finally:
                await body.aclose()

        asyncio.run(_close_on_resolve())
        print("  endpoint: a resolved token re-validation closes the SSE with `event: close`")
    finally:
        main._resume_conversation_by_token = orig_resume  # type: ignore[assignment]
        main.WIDGET_SSE_KEEPALIVE_SECONDS = orig_keepalive  # type: ignore[assignment]
        main.WIDGET_SSE_REVALIDATE_SECONDS = orig_revalidate  # type: ignore[assignment]

    return checks


# --------------------------------------------------------------------------- #
# HTTP-surface layer — the real ASGI stack for the pre-stream 401s + partition.
# --------------------------------------------------------------------------- #
def _run_http_surface(main) -> int:
    from fastapi.testclient import TestClient

    total = 0
    orig_resume = main._resume_conversation_by_token
    try:
        client = TestClient(main.app)

        # ---- missing token → 401 (before any resolve / stream). ----
        r = client.get(f"/widget/conversations/{CONV_X}/events")
        assert r.status_code == 401, f"missing token must 401 (got {r.status_code})"
        total += 1

        # ---- a token bound to a DIFFERENT conversation → 401 (binding). ----
        async def resume_other(http, raw, *, slide=True):
            return {"id": CONV_Y, "status": "active", "created_at": CREATED_AT}

        main._resume_conversation_by_token = resume_other  # type: ignore[assignment]
        r = client.get(
            f"/widget/conversations/{CONV_X}/events",
            headers={"X-Conversation-Token": TOKEN_X},
        )
        assert r.status_code == 401, f"cross-binding must 401 (got {r.status_code})"
        total += 1
        print("  http: missing token → 401; a token bound to another conversation → 401")

        # ---- US-074 partition: the customer events route is the PUBLIC-widget
        #      (anonymous, opaque-token) surface — NOT the JWT-authenticated exception. ----
        assert main._is_widget_public_path(
            f"/widget/conversations/{CONV_X}/events"
        ), (
            "FAILURE INDICATOR: the customer SSE must ride the anonymous public-widget "
            "surface (opaque token), keeping the customer OFF the Supabase JWT surface"
        )
        assert not main._is_widget_public_path(
            f"/widget/conversations/{CONV_X}/agent-reply"
        ), "(control) the agent leg stays the authenticated exception"
        total += 1
        print("  http: the customer events route is the anonymous public-widget surface (US-074)")
        return total
    finally:
        main._resume_conversation_by_token = orig_resume  # type: ignore[assignment]


def _run() -> None:
    bu = _run_bridge_unit()
    print(f"  ({bu} bridge unit checks passed)")
    main = _import_main()
    if main is None:
        return
    es = _run_endpoint_stream(main)
    print(f"  ({es} endpoint/stream checks passed)")
    hs = _run_http_surface(main)
    print(f"  ({hs} http-surface checks passed)")
    print(
        "OK: US-081 — the customer SSE is token-authed + binding-checked, delivers "
        "ONLY its own conversation's replies, closes on token invalidation, and the "
        "LISTEN/NOTIFY bridge replays REMOTE events while deduping its own"
    )


def main_entry() -> None:
    _run()


if __name__ == "__main__":
    main_entry()
