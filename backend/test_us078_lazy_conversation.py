"""US-078: lazy conversation creation on the FIRST customer message.

Two layers, the same shape as the other support-surface tests
(`test_us074_widget_cors.py`, `test_us076_widget_rate_limit.py`):

  * a UNIT layer (skips cleanly if the app can't import): the PURE insert-payload
    builders the endpoint writes through, which pin the AC-critical column values
    WITHOUT a DB — a widget conversation is born `status='active'`,
    `channel='widget'`, `bot_user_id` set (US-069), `escalated_at` NEVER planted
    by the caller (US-067 trigger owns it); a customer message carries no
    `tool_calls` (deterministic pipeline, not the agentic tool loop — US-079 AC).

  * an INTEGRATION / SECURITY layer (skips cleanly when the app can't import),
    encoding the PRD US-078 "Validation Test" end-to-end through the REAL
    endpoints via a FastAPI TestClient with every DB-touching helper mocked (no
    Supabase round-trip, no OpenAI):

      Setup: a fresh widget key K, no stored token.
      1. Resolve K (open) 5x without sending a message -> count conversation rows.
      2. Send the FIRST message                        -> count rows.
      3. Reload with the stored token, send a 2nd msg  -> count rows.

      Expected: step 1 -> 0 rows; step 2 -> 1 row; step 3 -> still 1 row (the
      stored token RESUMES, it does not duplicate). The "rows created" counter is
      the number of `_create_widget_conversation` calls.

    Failure indicator (the bug a test MUST catch): a conversation row created on
    OPEN (key resolution), or a reload-with-token DUPLICATING the conversation
    instead of resuming it. Plus security/contract guards: the raw opaque token is
    returned ONCE in the `X-Conversation-Token` RESPONSE HEADER and NEVER appears
    in the SSE body (US-071); the first message wires `bot_user_id` from the
    workspace bot and is born active/widget; an empty message is a 400; a
    malformed first-message key and an invalid resume token are refused without
    creating a row.

Run:
    python -m backend.test_us078_lazy_conversation

Both layers need only an importable backend (they mock the DB + limiter; no
Supabase, no OpenAI).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

# The app reads SUPABASE_* / a provider key at import time. Supply local defaults
# so the import succeeds without a real deployment; only set what is missing so a
# real environment is never clobbered.
os.environ.setdefault("SUPABASE_URL", "http://127.0.0.1:54321")
os.environ.setdefault("SUPABASE_ANON_KEY", "anon-test-key")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

LISTED = "https://client.example"  # a buyer's page — registered on the key
WORKSPACE = "ws-uuid-1"
BOT = "bot-uuid-1"


def _import_main():
    try:
        import main  # noqa: E402

        return main
    except Exception as e:  # pragma: no cover - environment-dependent
        print(f"SKIP: cannot import backend app ({e})")
        return None


# --------------------------------------------------------------------------- #
# Unit layer — the PURE insert-payload builders. No DB, no network.
# --------------------------------------------------------------------------- #
def _run_unit(main) -> int:
    checks = 0

    # A widget conversation is born ACTIVE on the WIDGET channel with the bot set.
    conv = main._widget_conversation_insert_payload(
        workspace_id=WORKSPACE, bot_user_id=BOT
    )
    assert conv["status"] == "active", "a widget conversation is born status='active' (US-067 latch start)"
    assert conv["channel"] == "widget", "channel must be 'widget'"
    assert conv["workspace_id"] == WORKSPACE, "workspace_id must be the resolved key's workspace"
    assert conv["bot_user_id"] == BOT, "bot_user_id must be set from the per-workspace bot (US-069)"
    # escalated_at is owned ENTIRELY by the US-067 status trigger; the caller must
    # never plant it (a row born 'active' gets a null latch).
    assert "escalated_at" not in conv, "the caller must NEVER set escalated_at (US-067 trigger owns it)"
    checks += 1
    print("  unit: conversation payload is active/widget, bot set, escalated_at not planted")

    # bot_user_id may be None (provisioning unavailable) — the column is nullable
    # and the row is still created; the key remains absent-of-meaning, not omitted.
    conv_nobot = main._widget_conversation_insert_payload(
        workspace_id=WORKSPACE, bot_user_id=None
    )
    assert conv_nobot["bot_user_id"] is None, "a botless workspace still creates the row (nullable column)"
    checks += 1
    print("  unit: conversation payload tolerates a null bot_user_id (still creates the row)")

    # A customer message carries role/content and NO tool_calls (deterministic
    # pipeline, not the agentic tool loop — US-079 AC).
    msg = main._conversation_message_insert_payload(
        conversation_id="c1", role="user", content="hello"
    )
    assert msg["conversation_id"] == "c1" and msg["role"] == "user" and msg["content"] == "hello"
    assert "tool_calls" not in msg, "conversation_messages.tool_calls stays null (no agentic tool loop)"
    checks += 1
    print("  unit: message payload carries role/content and NEVER a tool_calls field")

    return checks


# --------------------------------------------------------------------------- #
# Integration / security layer — the PRD validation test through the real
# endpoints via TestClient with every DB-touching helper mocked.
# --------------------------------------------------------------------------- #
def _run_integration(main) -> int:
    from fastapi.testclient import TestClient  # noqa: E402
    from widget_keys import generate_public_key  # noqa: E402

    pk = generate_public_key()

    # Shared mock state: the single conversation the first message creates, plus
    # counters proving WHEN a row is (not) created and WHICH helpers ran.
    state = {
        "rows_created": 0,
        "resume_calls": 0,
        "persisted": [],          # list of (conversation_id, role, content)
        "created_kwargs": None,   # kwargs _create_widget_conversation saw
        "raw_token": "tok-raw-secret-value",
        "conversation": None,     # the created row, replayed by resume
    }

    async def fake_resolve(http, public_key):
        # US-072 not-revoked gate: K resolves to its workspace with the listed origin.
        return {"id": "k", "workspace_id": WORKSPACE, "allowed_origins": [LISTED]}

    async def fake_ensure_bot(http, workspace_id):
        assert workspace_id == WORKSPACE
        return BOT

    async def fake_create(http, *, workspace_id, bot_user_id):
        state["rows_created"] += 1
        state["created_kwargs"] = {"workspace_id": workspace_id, "bot_user_id": bot_user_id}
        row = {
            "id": "conv-1",
            "workspace_id": workspace_id,
            "status": "active",
            "created_at": "2026-06-28T00:00:00+00:00",
        }
        state["conversation"] = row
        return row

    async def fake_issue_token(http, conversation_id):
        assert conversation_id == "conv-1"
        return state["raw_token"]

    async def fake_persist(http, *, conversation_id, role, content):
        state["persisted"].append((conversation_id, role, content))
        return {"id": f"m{len(state['persisted'])}", "role": role, "content": content}

    async def fake_resume(http, raw_token, *, slide=True):
        state["resume_calls"] += 1
        # A valid stored token resolves to the ONE conversation it is bound to.
        if raw_token == state["raw_token"] and state["conversation"] is not None:
            return state["conversation"]
        return None

    async def fake_load_bot(http, conversation_id):
        # US-079 resume path reads the conversation's bot_user_id under the service
        # role (the resume RPC view omits it). Mocked here so the row-count test
        # never touches the dummy DB.
        return BOT

    async def fake_run_turn(http, *, conversation_id, workspace_id, bot_user_id, message):
        # US-079 deflection turn. Stubbed so this row-count test runs no real
        # retrieval/LLM; US-079's own test exercises the streaming/persistence path.
        return "stubbed bot reply"

    async def fake_load_origins():
        # US-074 widget CORS snapshot: the buyer's origin is the one registered
        # origin. Mocked so the CORS layer admits LISTED (and the run stays quiet
        # instead of fail-closing against the dummy DB).
        return frozenset({LISTED}), False

    originals = {
        "_resolve_widget_key": main._resolve_widget_key,
        "_ensure_workspace_bot": main._ensure_workspace_bot,
        "_create_widget_conversation": main._create_widget_conversation,
        "_issue_conversation_token": main._issue_conversation_token,
        "_persist_conversation_message": main._persist_conversation_message,
        "_resume_conversation_by_token": main._resume_conversation_by_token,
        "_load_conversation_bot_user_id": main._load_conversation_bot_user_id,
        "_run_widget_bot_turn": main._run_widget_bot_turn,
        "_load_active_widget_origins": main._load_active_widget_origins,
        "_RATE_LIMITER": main._RATE_LIMITER,
    }
    main._resolve_widget_key = fake_resolve            # type: ignore[assignment]
    main._ensure_workspace_bot = fake_ensure_bot       # type: ignore[assignment]
    main._create_widget_conversation = fake_create     # type: ignore[assignment]
    main._issue_conversation_token = fake_issue_token  # type: ignore[assignment]
    main._persist_conversation_message = fake_persist  # type: ignore[assignment]
    main._resume_conversation_by_token = fake_resume   # type: ignore[assignment]
    main._load_conversation_bot_user_id = fake_load_bot  # type: ignore[assignment]
    main._run_widget_bot_turn = fake_run_turn          # type: ignore[assignment]
    main._load_active_widget_origins = fake_load_origins  # type: ignore[assignment]
    main._WIDGET_ORIGIN_SNAPSHOT.invalidate()
    main._RATE_LIMITER = None  # row-count test: rate limiting is no-op here  # type: ignore[assignment]

    total = 0
    try:
        client = TestClient(main.app)

        # Step 1: resolve K (open) 5x WITHOUT a message -> ZERO conversation rows.
        for i in range(5):
            r = client.post(
                "/widget/keys/resolve",
                json={"public_key": pk},
                headers={"Origin": LISTED},
            )
            assert r.status_code == 200 and r.json().get("active") is True, (
                f"open {i + 1} must resolve active, got {r.status_code} {r.text}"
            )
        assert state["rows_created"] == 0, (
            "FAILURE INDICATOR: a conversation row was created on OPEN — open must "
            "do ONLY key resolution (no row, no token, no SSE)"
        )
        total += 1
        print("  step 1: 5 opens (key resolution) created 0 conversation rows")

        # Step 2: the FIRST message creates exactly ONE row + issues the token.
        r = client.post(
            "/widget/conversations/messages",
            json={"public_key": pk, "message": "how do I reset my password?"},
            headers={"Origin": LISTED},
        )
        assert r.status_code == 200, f"first message must succeed, got {r.status_code} {r.text}"
        assert state["rows_created"] == 1, "the first message must create exactly ONE conversation row"
        # The conversation was born active/widget with the bot wired from US-069.
        assert state["created_kwargs"] == {"workspace_id": WORKSPACE, "bot_user_id": BOT}, (
            "the conversation must be created for the resolved workspace with bot_user_id set"
        )
        # The customer message was persisted as a user turn.
        assert ("conv-1", "user", "how do I reset my password?") in state["persisted"], (
            "the customer's first message must be persisted (role='user')"
        )
        total += 1
        print("  step 2: first message created exactly 1 row, bot wired, message persisted")

        # Token contract (US-071): returned ONCE in the RESPONSE HEADER, NEVER in
        # the SSE body / an event.
        assert r.headers.get(main._CONVERSATION_TOKEN_HEADER) == state["raw_token"], (
            "the raw token must be returned in the X-Conversation-Token response header"
        )
        assert state["raw_token"] not in r.text, (
            "SECURITY: the raw token must NEVER appear in the SSE body / an event (US-071)"
        )
        # The SSE announced the conversation and closed with done.
        assert "event: conversation" in r.text and "event: done" in r.text, (
            "the request-scoped SSE must announce the conversation then done"
        )
        assert "conv-1" in r.text, "the conversation id (non-secret) is announced in the SSE body"
        total += 1
        print("  step 2b: token only in the response HEADER (never the SSE body); SSE announces conversation+done")

        # Step 3: reload with the stored token, send a SECOND message -> STILL 1
        # row (resumed, not duplicated).
        r = client.post(
            "/widget/conversations/messages",
            json={"message": "it still does not work"},
            headers={"Origin": LISTED, main._CONVERSATION_TOKEN_HEADER: state["raw_token"]},
        )
        assert r.status_code == 200, f"the resumed message must succeed, got {r.status_code} {r.text}"
        assert state["resume_calls"] == 1, "a token-bearing message must RESUME (resolve the token)"
        assert state["rows_created"] == 1, (
            "FAILURE INDICATOR: a reload-with-token DUPLICATED the conversation — it "
            "must resume the existing row (still 1)"
        )
        assert ("conv-1", "user", "it still does not work") in state["persisted"], (
            "the resumed turn's message must be persisted to the SAME conversation"
        )
        # No new token is minted on resume (the stored one is stable).
        assert main._CONVERSATION_TOKEN_HEADER not in r.headers, (
            "a resume must NOT mint/return a new token (the stored token is stable)"
        )
        total += 1
        print("  step 3: reload-with-token resumed the SAME row (still 1), no new token minted")

        # Guard: an empty message is a 400 and creates nothing.
        before = state["rows_created"]
        r = client.post(
            "/widget/conversations/messages",
            json={"public_key": pk, "message": "   "},
            headers={"Origin": LISTED},
        )
        assert r.status_code == 400, f"an empty message must be 400, got {r.status_code}"
        assert state["rows_created"] == before, "an empty message must create no row"
        total += 1
        print("  guard: an empty message -> 400, no row created")

        # Guard: a malformed first-message key is the same opaque 404, no row.
        before = state["rows_created"]
        r = client.post(
            "/widget/conversations/messages",
            json={"public_key": "not-a-widget-key", "message": "hi"},
            headers={"Origin": LISTED},
        )
        assert r.status_code == 404, f"a malformed key must be 404, got {r.status_code}"
        assert state["rows_created"] == before, "a malformed key must create no row"
        total += 1
        print("  guard: a malformed first-message key -> 404 opaque, no row created")

        # Guard: an invalid/expired resume token is a 401, no row.
        before = state["rows_created"]
        r = client.post(
            "/widget/conversations/messages",
            json={"message": "hi"},
            headers={"Origin": LISTED, main._CONVERSATION_TOKEN_HEADER: "wrong-token"},
        )
        assert r.status_code == 401, f"an invalid resume token must be 401, got {r.status_code}"
        assert state["rows_created"] == before, "an invalid resume token must create no row"
        total += 1
        print("  guard: an invalid resume token -> 401, no row created")

        print(
            f"OK: US-078 integration passed — {total} endpoint assertions; opens create "
            "0 rows, the first message creates exactly 1 (active/widget, bot wired, token "
            "in the header only), and a reload-with-token resumes the SAME row (no duplicate)"
        )
        return total
    finally:
        for name, value in originals.items():
            setattr(main, name, value)
        # Drop the mocked origin snapshot so a later test sees a fresh load.
        main._WIDGET_ORIGIN_SNAPSHOT.invalidate()


def _run() -> None:
    main = _import_main()
    if main is None:
        return
    unit = _run_unit(main)
    print(f"  ({unit} unit checks passed)")
    _run_integration(main)


def main_entry() -> None:
    _run()


if __name__ == "__main__":
    main_entry()
