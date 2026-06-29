"""US-079: the deterministic deflection turn streams over the request-scoped SSE.

Two layers, the same shape as the other support-surface tests
(`test_us078_lazy_conversation.py`, `test_us077_circuit_breaker.py`):

  * a UNIT layer (always runs, no DB / no LLM): the pure `_split_for_streaming`
    chunker (re-concatenates byte-exact; emits >1 delta for a real answer) and the
    fail-CLOSED short-circuit of `_run_widget_bot_turn` (a botless workspace
    escalates to the generic deferral without touching the pipeline), plus the
    `tool_calls`-stays-null contract on the assistant message payload (AC4).

  * an INTEGRATION layer (skips cleanly when the app can't import), encoding the
    PRD US-079 "Validation Test" end-to-end through the REAL
    `POST /widget/conversations/messages` endpoint via a FastAPI TestClient, with
    every DB-touching helper mocked AND the ADR-0003 pipeline mocked at
    `support_bot.run_bot_deflection_turn` (no Supabase, no OpenAI) — so the REAL
    `_run_widget_bot_turn` + streaming generator are exercised:

      - ANSWERED branch: a grounded answer streams as `delta` events that
        re-concatenate to the answer, then `done`; the customer's message AND the
        bot answer are persisted to `conversation_messages` (user + assistant).
      - ESCALATE branch: the pipeline's generic deferral streams — never a
        confident answer.
      - BREAKER TRIP (US-077 at its live call site): the per-workspace breaker
        trips, so `run_bot_deflection_turn` is NEVER called (zero retrieval/LLM)
        and the breaker deferral streams.
      - BOTLESS workspace: no provisioned bot ⇒ escalate, pipeline never called.
      - RESUME: a token-bearing turn on an existing conversation still gets a bot
        answer (bot_user_id read under the service role).

    Failure indicator (the bug a test MUST catch): the answer fails to stream over
    the request SSE, a confident answer streams on the escalate branch, or the
    breaker trip still runs the paid pipeline.

Run:
    python -m backend.test_us079_deflection_streaming
"""

from __future__ import annotations

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

LISTED = "https://client.example"  # a buyer's page — registered on the key
WORKSPACE = "ws-uuid-1"
BOT = "bot-uuid-1"
ANSWER = "Reset your password from Settings, then Security, then Reset password."


def _import_main():
    try:
        import main  # noqa: E402

        return main
    except Exception as e:  # pragma: no cover - environment-dependent
        print(f"SKIP: cannot import backend app ({e})")
        return None


def _collect_deltas(sse_text: str) -> str:
    """Concatenate the `text` of every `delta` event in a raw SSE body."""
    texts: list[str] = []
    event: str | None = None
    for line in sse_text.split("\n"):
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:") and event == "delta":
            payload = json.loads(line[len("data:"):].strip())
            texts.append(payload.get("text", ""))
    return "".join(texts)


# --------------------------------------------------------------------------- #
# Unit layer — the pure chunker + the fail-closed short-circuit. No DB, no LLM.
# --------------------------------------------------------------------------- #
def _run_unit(main) -> int:
    import asyncio

    from escalation import GENERIC_DEFERRAL

    checks = 0

    # _split_for_streaming re-concatenates BYTE-EXACT for assorted shapes, so what
    # streams equals what was approved (and persisted).
    for text in [
        "",
        "ok",
        ANSWER,
        "line one\nline two\twith tabs   and  runs",
        "   leading and trailing   ",
        "a" * 200,
    ]:
        chunks = main._split_for_streaming(text)
        assert "".join(chunks) == text, f"chunks must re-concatenate to the original: {text!r}"
        if text == "":
            assert chunks == [], "an empty message yields zero delta chunks"
    checks += 1
    print("  unit: _split_for_streaming re-concatenates byte-exact (incl. empty, whitespace, long)")

    # A real answer streams as MORE THAN ONE delta (the validation test expects
    # `delta` events, plural, then done).
    assert len(main._split_for_streaming(ANSWER)) > 1, "a real answer must stream as multiple delta events"
    checks += 1
    print("  unit: a real answer splits into multiple delta events")

    # Fail-closed: a botless workspace escalates to the generic deferral WITHOUT
    # invoking the pipeline (no http, no mint, no LLM). http is never touched on
    # this path, so passing None proves the short-circuit happens first.
    reply = asyncio.run(
        main._run_widget_bot_turn(
            None,  # type: ignore[arg-type]
            conversation_id="c1",
            workspace_id=WORKSPACE,
            bot_user_id=None,
            message="hello",
        )
    )
    assert reply == GENERIC_DEFERRAL, "a botless workspace must escalate (generic deferral), not crash"
    checks += 1
    print("  unit: _run_widget_bot_turn escalates a botless workspace without touching the pipeline")

    # AC4: the assistant turn carries NO tool_calls (deterministic pipeline, not the
    # agentic tool loop).
    msg = main._conversation_message_insert_payload(
        conversation_id="c1", role="assistant", content=ANSWER
    )
    assert "tool_calls" not in msg, "conversation_messages.tool_calls stays null for the bot answer (AC4)"
    checks += 1
    print("  unit: the bot answer's message payload carries NO tool_calls field (AC4)")

    return checks


# --------------------------------------------------------------------------- #
# Integration layer — the validation test through the real endpoint, with the DB
# helpers + the ADR-0003 pipeline mocked.
# --------------------------------------------------------------------------- #
def _run_integration(main) -> int:
    from fastapi.testclient import TestClient  # noqa: E402

    from circuit_breaker import GENERIC_BREAKER_DEFERRAL  # noqa: E402
    from escalation import (  # noqa: E402
        GENERIC_DEFERRAL,
        DeflectionResult,
        FaithfulnessDecision,
        RetrievalGateDecision,
    )
    from rate_limiting import RateLimitDecision, RateLimiter  # noqa: E402
    from widget_keys import generate_public_key  # noqa: E402
    import support_bot  # noqa: E402

    pk = generate_public_key()

    def _answered() -> DeflectionResult:
        return DeflectionResult(
            action="answered",
            customer_message=ANSWER,
            retrieval=RetrievalGateDecision(
                strong=True, top1_cosine=0.92, n_cleared=4, reason="strong"
            ),
            faithfulness=FaithfulnessDecision(
                faithful=True, supported=True, score=0.95, reason="faithful"
            ),
            reason="answered",
        )

    def _escalated() -> DeflectionResult:
        return DeflectionResult(
            action="escalated",
            customer_message=GENERIC_DEFERRAL,
            retrieval=RetrievalGateDecision(
                strong=False, top1_cosine=0.1, n_cleared=0, reason="weak: top1"
            ),
            faithfulness=None,
            reason="retrieval_weak",
        )

    state = {
        "persisted": [],         # list of (conversation_id, role, content)
        "turn_calls": 0,         # how many times the ADR-0003 pipeline ran
        "turn_kwargs": None,     # kwargs the pipeline saw (workspace/bot/message)
        "next_result": None,     # the DeflectionResult the mocked pipeline returns
        "bot_for_workspace": BOT,
        "conversation": {
            "id": "conv-1",
            "workspace_id": WORKSPACE,
            "status": "active",
            "created_at": "2026-06-29T00:00:00+00:00",
        },
    }

    async def fake_resolve(http, public_key):
        return {"id": "k", "workspace_id": WORKSPACE, "allowed_origins": [LISTED]}

    async def fake_ensure_bot(http, workspace_id):
        return state["bot_for_workspace"]

    async def fake_create(http, *, workspace_id, bot_user_id):
        row = {
            "id": "conv-1",
            "workspace_id": workspace_id,
            "bot_user_id": bot_user_id,
            "status": "active",
            "created_at": "2026-06-29T00:00:00+00:00",
        }
        state["conversation"] = row
        return row

    async def fake_issue_token(http, conversation_id):
        return "tok-raw-secret-value"

    async def fake_persist(http, *, conversation_id, role, content):
        state["persisted"].append((conversation_id, role, content))
        return {"id": f"m{len(state['persisted'])}", "role": role, "content": content}

    async def fake_resume(http, raw_token, *, slide=True):
        if raw_token == "tok-raw-secret-value":
            return state["conversation"]
        return None

    async def fake_load_bot(http, conversation_id):
        return state["bot_for_workspace"]

    async def fake_run_bot_deflection_turn(**kwargs):
        # The ADR-0003 pipeline (US-070). Count calls (the breaker-trip case must
        # leave this at ZERO) and capture the boundary inputs.
        state["turn_calls"] += 1
        state["turn_kwargs"] = kwargs
        return state["next_result"]

    async def fake_load_origins():
        return frozenset({LISTED}), False

    class _SelectiveLimiter(RateLimiter):
        """Allows US-076's `key:`/`ip:` windows but TRIPS the US-077 `ws:` breaker.

        A globally-tripped limiter would 429 at the US-076 per-session charge and
        never reach the breaker, so this limiter discriminates by bucket namespace.
        """

        name = "selective"

        def __init__(self) -> None:
            self.hits: list[str] = []

        async def hit(self, key, *, limit, window_seconds, cost=1) -> RateLimitDecision:
            self.hits.append(key)
            tripped = key.startswith("ws:")
            return RateLimitDecision(
                allowed=not tripped,
                count=(limit + 1 if tripped else cost),
                limit=limit,
                window_seconds=window_seconds,
            )

        async def count(self, key, *, window_seconds) -> int:
            return 0

    originals = {
        "_resolve_widget_key": main._resolve_widget_key,
        "_ensure_workspace_bot": main._ensure_workspace_bot,
        "_create_widget_conversation": main._create_widget_conversation,
        "_issue_conversation_token": main._issue_conversation_token,
        "_persist_conversation_message": main._persist_conversation_message,
        "_resume_conversation_by_token": main._resume_conversation_by_token,
        "_load_conversation_bot_user_id": main._load_conversation_bot_user_id,
        "_load_active_widget_origins": main._load_active_widget_origins,
        "_RATE_LIMITER": main._RATE_LIMITER,
    }
    orig_turn = support_bot.run_bot_deflection_turn

    main._resolve_widget_key = fake_resolve              # type: ignore[assignment]
    main._ensure_workspace_bot = fake_ensure_bot         # type: ignore[assignment]
    main._create_widget_conversation = fake_create       # type: ignore[assignment]
    main._issue_conversation_token = fake_issue_token    # type: ignore[assignment]
    main._persist_conversation_message = fake_persist    # type: ignore[assignment]
    main._resume_conversation_by_token = fake_resume     # type: ignore[assignment]
    main._load_conversation_bot_user_id = fake_load_bot  # type: ignore[assignment]
    main._load_active_widget_origins = fake_load_origins  # type: ignore[assignment]
    support_bot.run_bot_deflection_turn = fake_run_bot_deflection_turn  # type: ignore[assignment]
    main._WIDGET_ORIGIN_SNAPSHOT.invalidate()
    main._RATE_LIMITER = None  # default: limiter no-op (breaker + US-076 inert)  # type: ignore[assignment]

    def _first_message(msg: str) -> "object":
        return client.post(
            "/widget/conversations/messages",
            json={"public_key": pk, "message": msg},
            headers={"Origin": LISTED},
        )

    total = 0
    try:
        client = TestClient(main.app)

        # ---- ANSWERED branch: grounded answer streams + both turns persisted. ----
        state["persisted"].clear()
        state["turn_calls"] = 0
        state["next_result"] = _answered()
        r = _first_message("how do I reset my password?")
        assert r.status_code == 200, f"answered turn must succeed, got {r.status_code} {r.text}"
        assert "event: conversation" in r.text, "the SSE announces the conversation"
        assert "event: delta" in r.text and "event: done" in r.text, (
            "the bot answer must stream as delta events then done (the request SSE)"
        )
        assert _collect_deltas(r.text) == ANSWER, (
            "FAILURE INDICATOR: the streamed delta text must re-assemble to the grounded answer"
        )
        assert state["turn_calls"] == 1, "the ADR-0003 pipeline runs exactly once on an active turn"
        # The pipeline ran AS the workspace bot with the conversation's workspace.
        assert state["turn_kwargs"]["bot_user_id"] == BOT
        assert state["turn_kwargs"]["workspace_id"] == WORKSPACE
        assert state["turn_kwargs"]["message"] == "how do I reset my password?"
        # Both the customer message AND the bot answer are persisted (AC2).
        assert ("conv-1", "user", "how do I reset my password?") in state["persisted"], (
            "the customer message must persist to conversation_messages (role='user')"
        )
        assert ("conv-1", "assistant", ANSWER) in state["persisted"], (
            "FAILURE INDICATOR: the bot answer must persist to conversation_messages (role='assistant')"
        )
        total += 1
        print("  answered: grounded answer streamed (delta→done), pipeline ran AS the bot, user+assistant persisted")

        # ---- ESCALATE branch: the deferral streams, NEVER a confident answer. ----
        state["persisted"].clear()
        state["turn_calls"] = 0
        state["next_result"] = _escalated()
        r = _first_message("what is the meaning of life?")
        assert r.status_code == 200, f"escalate turn must succeed, got {r.status_code} {r.text}"
        streamed = _collect_deltas(r.text)
        assert streamed == GENERIC_DEFERRAL, (
            "the escalate branch must stream the generic deferral (no answer)"
        )
        assert ANSWER not in r.text, (
            "FAILURE INDICATOR: a confident answer must NOT stream on the escalate branch"
        )
        assert ("conv-1", "assistant", GENERIC_DEFERRAL) in state["persisted"], (
            "the deferral is persisted as the bot's turn"
        )
        total += 1
        print("  escalate: the generic deferral streamed (no confident answer), persisted as the bot turn")

        # ---- BREAKER TRIP (US-077 live call site): pipeline NEVER runs. ----
        state["persisted"].clear()
        state["turn_calls"] = 0
        state["next_result"] = _answered()  # would stream IF the pipeline ran
        main._RATE_LIMITER = _SelectiveLimiter()  # type: ignore[assignment]
        try:
            r = _first_message("anything at all")
            assert r.status_code == 200, f"a tripped breaker is a 200 deferral, got {r.status_code}"
            assert state["turn_calls"] == 0, (
                "FAILURE INDICATOR: a tripped breaker must run ZERO pipeline calls (zero retrieval/LLM)"
            )
            assert _collect_deltas(r.text) == GENERIC_BREAKER_DEFERRAL, (
                "a tripped breaker streams the breaker deferral, not the answer"
            )
            assert ANSWER not in r.text, "the answer must not leak when the breaker tripped"
        finally:
            main._RATE_LIMITER = None  # type: ignore[assignment]
        total += 1
        print("  breaker: a tripped per-workspace breaker streamed the deferral and ran ZERO pipeline calls")

        # ---- BOTLESS workspace: escalate, pipeline never called. ----
        state["persisted"].clear()
        state["turn_calls"] = 0
        state["next_result"] = _answered()
        state["bot_for_workspace"] = None  # provisioning unavailable
        try:
            r = _first_message("hello?")
            assert r.status_code == 200
            assert state["turn_calls"] == 0, "a botless workspace must not run the pipeline"
            assert _collect_deltas(r.text) == GENERIC_DEFERRAL, "a botless workspace escalates"
        finally:
            state["bot_for_workspace"] = BOT
        total += 1
        print("  botless: a workspace with no provisioned bot escalated without running the pipeline")

        # ---- RESUME: a token-bearing turn still gets a bot answer. ----
        state["persisted"].clear()
        state["turn_calls"] = 0
        state["next_result"] = _answered()
        r = client.post(
            "/widget/conversations/messages",
            json={"message": "and what about 2FA?"},
            headers={"Origin": LISTED, main._CONVERSATION_TOKEN_HEADER: "tok-raw-secret-value"},
        )
        assert r.status_code == 200, f"resumed turn must succeed, got {r.status_code} {r.text}"
        assert state["turn_calls"] == 1, "a resumed active conversation still runs the pipeline"
        assert state["turn_kwargs"]["bot_user_id"] == BOT, (
            "the resume path must resolve bot_user_id (service-role read) so the bot can answer"
        )
        assert _collect_deltas(r.text) == ANSWER, "the bot answer streams on a resumed conversation"
        assert ("conv-1", "assistant", ANSWER) in state["persisted"]
        # A resume mints no new token (US-071: returned once, never rotated).
        assert main._CONVERSATION_TOKEN_HEADER not in r.headers, "a resume must not mint a new token"
        total += 1
        print("  resume: a token-bearing turn resolved the bot and streamed its answer (no new token)")

        print(
            f"OK: US-079 integration passed — {total} scenarios; the deflection turn streams "
            "its answer over the request SSE (delta→done) and persists user+assistant, escalate "
            "streams the deferral, and a tripped breaker runs ZERO pipeline calls"
        )
        return total
    finally:
        for name, value in originals.items():
            setattr(main, name, value)
        support_bot.run_bot_deflection_turn = orig_turn  # type: ignore[assignment]
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
