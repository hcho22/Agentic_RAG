"""US-080: the escalation latch routes all later customer messages to the human.

Two layers, the same shape as the other support-surface tests
(`test_us079_deflection_streaming.py`, `test_us077_circuit_breaker.py`):

  * a UNIT layer (always runs, no DB / no LLM / no app network):
      - `_escalate_conversation` issues a service-role PATCH that sets ONLY
        `status='escalated'` and NEVER plants `escalated_at` (the US-067 trigger
        owns it) — pinned with an httpx `MockTransport` that captures the request.
      - `_run_widget_bot_turn` latches a DELIBERATE escalate (the pipeline deciding
        `escalated`, or a tripped US-077 breaker) and DOES NOT latch a confident
        answer or a recoverable degraded deferral (botless workspace) — so a blip
        never permanently silences the bot. The breaker-trip latch goes through the
        breaker's `on_trip` hook AND runs ZERO pipeline calls (the US-077 guarantee).

  * an INTEGRATION layer (skips cleanly when the app can't import), encoding the
    PRD US-080 "Validation Test" end-to-end through the REAL endpoints via a FastAPI
    TestClient, with every DB-touching helper mocked (no Supabase, no OpenAI):

      - MODEL-MEDIATED escalate: the first turn whose ADR-0003 decision is escalate
        latches `status='escalated'` (escalated_at stamped once) and streams the
        generic deferral — never a bot answer. The two NEXT customer messages
        persist + route to the queue but the pipeline-entry counter does NOT
        increment, and `escalated_at` is NOT reset (the two failure indicators).
      - EXPLICIT "talk to a human" (AC3): `POST /widget/conversations/escalate`
        latches via the SAME path WITHOUT running the pipeline, and the next message
        skips the pipeline too.

Run:
    python -m backend.test_us080_escalation_latch
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

LISTED = "https://client.example"  # a buyer's page — registered on the key
WORKSPACE = "ws-uuid-1"
BOT = "bot-uuid-1"
ANSWER = "Reset your password from Settings, then Security, then Reset password."
CREATED_AT = "2026-06-29T00:00:00+00:00"
ESCALATED_AT = "2026-06-29T00:00:05+00:00"
TOKEN = "tok-raw-secret-value"


def _import_main():
    try:
        import main  # noqa: E402

        return main
    except Exception as e:  # pragma: no cover - environment-dependent
        print(f"SKIP: cannot import backend app ({e})")
        return None


def _events(sse_text: str) -> list[str]:
    """The ordered list of `event:` names in a raw SSE body."""
    return [
        line[len("event:"):].strip()
        for line in sse_text.split("\n")
        if line.startswith("event:")
    ]


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
# Unit layer — the latch PATCH shape + the deliberate-vs-degraded latch policy.
# --------------------------------------------------------------------------- #
def _run_unit(main) -> int:
    import httpx

    from circuit_breaker import GENERIC_BREAKER_DEFERRAL
    from escalation import (
        GENERIC_DEFERRAL,
        DeflectionResult,
        FaithfulnessDecision,
        RetrievalGateDecision,
    )
    from rate_limiting import RateLimitDecision, RateLimiter
    import support_bot

    checks = 0

    # ---- _escalate_conversation: PATCH sets status, NEVER plants escalated_at. ----
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content) if request.content else None
        return httpx.Response(
            200,
            json=[{
                "id": "c1",
                "status": "escalated",
                "created_at": CREATED_AT,
                "escalated_at": ESCALATED_AT,
                "workspace_id": WORKSPACE,
            }],
        )

    orig_key = main.SUPABASE_SERVICE_ROLE_KEY
    main.SUPABASE_SERVICE_ROLE_KEY = "service-test-key"  # type: ignore[assignment]
    try:
        async def _do() -> dict | None:
            transport = httpx.MockTransport(_handler)
            async with httpx.AsyncClient(transport=transport) as http:
                return await main._escalate_conversation(http, "c1")

        row = asyncio.run(_do())
    finally:
        main.SUPABASE_SERVICE_ROLE_KEY = orig_key  # type: ignore[assignment]

    assert captured["method"] == "PATCH", "the latch is a PATCH on the conversation row"
    assert "id=eq.c1" in captured["url"], "the PATCH is scoped to the one conversation id"
    assert captured["json"] == {"status": "escalated"}, (
        "FAILURE INDICATOR: the latch must set ONLY status — escalated_at is the "
        f"US-067 trigger's to own, never planted by the caller (got {captured['json']!r})"
    )
    assert row is not None and row["status"] == "escalated"
    checks += 1
    print("  unit: _escalate_conversation PATCHes status='escalated' and never plants escalated_at")

    # ---- _run_widget_bot_turn latch policy: deliberate escalate latches; a
    #      confident answer / a recoverable degraded deferral does NOT. ----
    def _result(action: str) -> DeflectionResult:
        if action == "answered":
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
        return DeflectionResult(
            action="escalated",
            customer_message=GENERIC_DEFERRAL,
            retrieval=RetrievalGateDecision(
                strong=False, top1_cosine=0.1, n_cleared=0, reason="weak: top1"
            ),
            faithfulness=None,
            reason="retrieval_weak",
        )

    state = {"next": _result("answered"), "pipeline_calls": 0, "escalated": []}

    async def fake_pipeline(**kwargs):
        state["pipeline_calls"] += 1
        return state["next"]

    async def fake_escalate(http, conversation_id, *, customer_email=None):
        state["escalated"].append(conversation_id)
        return {"id": conversation_id, "status": "escalated",
                "created_at": CREATED_AT, "workspace_id": WORKSPACE}

    class _TripWsLimiter(RateLimiter):
        """Trips ONLY the US-077 `ws:` breaker bucket (so US-076 windows pass)."""

        name = "trip-ws"

        async def hit(self, key, *, limit, window_seconds, cost=1) -> RateLimitDecision:
            tripped = key.startswith("ws:")
            return RateLimitDecision(
                allowed=not tripped,
                count=(limit + 1 if tripped else cost),
                limit=limit,
                window_seconds=window_seconds,
            )

        async def count(self, key, *, window_seconds) -> int:
            return 0

    orig_turn = support_bot.run_bot_deflection_turn
    orig_escalate = main._escalate_conversation
    orig_limiter = main._RATE_LIMITER
    support_bot.run_bot_deflection_turn = fake_pipeline  # type: ignore[assignment]
    main._escalate_conversation = fake_escalate  # type: ignore[assignment]
    main._RATE_LIMITER = None  # type: ignore[assignment]
    try:
        def _turn() -> str:
            return asyncio.run(
                main._run_widget_bot_turn(
                    None,  # type: ignore[arg-type]
                    conversation_id="c1",
                    workspace_id=WORKSPACE,
                    bot_user_id=BOT,
                    message="hi",
                )
            )

        # answered → confident answer, NO latch.
        state["next"], state["pipeline_calls"], state["escalated"][:] = (
            _result("answered"), 0, []
        )
        reply = _turn()
        assert reply == ANSWER and state["escalated"] == [], (
            "a confident answer must NOT latch the conversation"
        )
        checks += 1
        print("  unit: a confident answer does not latch")

        # model-mediated escalate → latch + generic deferral.
        state["next"], state["pipeline_calls"], state["escalated"][:] = (
            _result("escalated"), 0, []
        )
        reply = _turn()
        assert reply == GENERIC_DEFERRAL and state["escalated"] == ["c1"], (
            "a deliberate ADR-0003 escalate must latch the conversation"
        )
        checks += 1
        print("  unit: a model-mediated escalate latches (status='escalated')")

        # botless workspace → recoverable degraded deferral, NO latch, NO pipeline.
        state["pipeline_calls"], state["escalated"][:] = 0, []
        reply = asyncio.run(
            main._run_widget_bot_turn(
                None,  # type: ignore[arg-type]
                conversation_id="c1",
                workspace_id=WORKSPACE,
                bot_user_id=None,
                message="hi",
            )
        )
        assert reply == GENERIC_DEFERRAL, "a botless workspace still defers"
        assert state["escalated"] == [], (
            "a botless/degraded deferral must NOT permanently latch (it may be transient)"
        )
        assert state["pipeline_calls"] == 0, "a botless workspace runs no pipeline"
        checks += 1
        print("  unit: a botless degraded deferral does not latch (recoverable, not permanent)")

        # breaker trip → latch via on_trip, ZERO pipeline calls.
        state["next"], state["pipeline_calls"], state["escalated"][:] = (
            _result("answered"), 0, []
        )
        main._RATE_LIMITER = _TripWsLimiter()  # type: ignore[assignment]
        reply = _turn()
        assert reply == GENERIC_BREAKER_DEFERRAL, "a tripped breaker streams the breaker deferral"
        assert state["pipeline_calls"] == 0, (
            "FAILURE INDICATOR: a tripped breaker must run ZERO pipeline calls"
        )
        assert state["escalated"] == ["c1"], (
            "a tripped breaker latches the conversation via the on_trip hook (US-080)"
        )
        checks += 1
        print("  unit: a tripped breaker latches via on_trip and runs ZERO pipeline calls")
    finally:
        support_bot.run_bot_deflection_turn = orig_turn  # type: ignore[assignment]
        main._escalate_conversation = orig_escalate  # type: ignore[assignment]
        main._RATE_LIMITER = orig_limiter  # type: ignore[assignment]

    return checks


# --------------------------------------------------------------------------- #
# Integration layer — the PRD validation test through the real endpoints.
# --------------------------------------------------------------------------- #
def _run_integration(main) -> int:
    from fastapi.testclient import TestClient

    from escalation import (
        GENERIC_DEFERRAL,
        DeflectionResult,
        FaithfulnessDecision,
        RetrievalGateDecision,
    )
    from widget_keys import generate_public_key
    import support_bot

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

    def _fresh_active() -> dict:
        return {
            "id": "conv-1",
            "workspace_id": WORKSPACE,
            "bot_user_id": BOT,
            "status": "active",
            "escalated_at": None,
            "created_at": CREATED_AT,
        }

    state = {
        "persisted": [],
        "turn_calls": 0,
        "escalate_calls": 0,
        "next_result": _escalated(),
        "conversation": _fresh_active(),
        # The status `_load_conversation_status` reports AFTER the turn ran — the
        # US-080 in-flight race re-read. 'active' unless a scenario simulates a
        # concurrent escalation latching while the bot was mid-pipeline.
        "midflight_status": "active",
    }

    async def fake_resolve(http, public_key):
        return {"id": "k", "workspace_id": WORKSPACE, "allowed_origins": [LISTED]}

    async def fake_ensure_bot(http, workspace_id):
        return BOT

    async def fake_create(http, *, workspace_id, bot_user_id):
        state["conversation"] = _fresh_active()
        return dict(state["conversation"])

    async def fake_issue_token(http, conversation_id):
        return TOKEN

    async def fake_persist(http, *, conversation_id, role, content):
        state["persisted"].append((conversation_id, role, content))
        return {"id": f"m{len(state['persisted'])}", "role": role, "content": content}

    async def fake_resume(http, raw_token, *, slide=True):
        return dict(state["conversation"]) if raw_token == TOKEN else None

    async def fake_load_bot(http, conversation_id):
        return BOT

    async def fake_load_status(http, conversation_id):
        # The US-080 in-flight re-read: report whatever the scenario set, simulating a
        # status the conversation reached AFTER the captured entry snapshot.
        return state["midflight_status"]

    async def fake_escalate(http, conversation_id, *, customer_email=None):
        # Simulate the US-067 trigger: stamp escalated_at ONCE, preserve afterward.
        state["escalate_calls"] += 1
        row = state["conversation"]
        if row.get("escalated_at") is None:
            row["escalated_at"] = ESCALATED_AT
        row["status"] = "escalated"
        if customer_email:  # US-092: fold in an optional follow-up email if left.
            row["customer_email"] = customer_email
        return dict(row)

    async def fake_run_pipeline(**kwargs):
        state["turn_calls"] += 1
        return state["next_result"]

    async def fake_load_origins():
        return frozenset({LISTED}), False

    originals = {
        "_resolve_widget_key": main._resolve_widget_key,
        "_ensure_workspace_bot": main._ensure_workspace_bot,
        "_create_widget_conversation": main._create_widget_conversation,
        "_issue_conversation_token": main._issue_conversation_token,
        "_persist_conversation_message": main._persist_conversation_message,
        "_resume_conversation_by_token": main._resume_conversation_by_token,
        "_load_conversation_bot_user_id": main._load_conversation_bot_user_id,
        "_load_conversation_status": main._load_conversation_status,
        "_escalate_conversation": main._escalate_conversation,
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
    main._load_conversation_status = fake_load_status     # type: ignore[assignment]
    main._escalate_conversation = fake_escalate          # type: ignore[assignment]
    main._load_active_widget_origins = fake_load_origins  # type: ignore[assignment]
    support_bot.run_bot_deflection_turn = fake_run_pipeline  # type: ignore[assignment]
    main._WIDGET_ORIGIN_SNAPSHOT.invalidate()
    main._RATE_LIMITER = None  # limiter no-op (US-076 + breaker inert)  # type: ignore[assignment]

    def _first_message(msg: str):
        return client.post(
            "/widget/conversations/messages",
            json={"public_key": pk, "message": msg},
            headers={"Origin": LISTED},
        )

    def _resumed_message(msg: str):
        return client.post(
            "/widget/conversations/messages",
            json={"message": msg},
            headers={"Origin": LISTED, main._CONVERSATION_TOKEN_HEADER: TOKEN},
        )

    total = 0
    try:
        client = TestClient(main.app)

        # ===== MODEL-MEDIATED escalate latches; later messages skip the pipeline. =====
        state["conversation"] = _fresh_active()
        state["persisted"].clear()
        state["turn_calls"], state["escalate_calls"] = 0, 0
        state["next_result"] = _escalated()

        # Step 1: the escalating first turn.
        r = _first_message("what is the meaning of life?")
        assert r.status_code == 200, f"escalate turn must succeed, got {r.status_code} {r.text}"
        assert _collect_deltas(r.text) == GENERIC_DEFERRAL, (
            "the escalate turn streams the generic deferral (not a bot answer)"
        )
        assert ANSWER not in r.text, "no confident answer on the escalate turn"
        assert state["turn_calls"] == 1, "the escalating turn DID run the pipeline (once)"
        assert state["conversation"]["status"] == "escalated", "step 1 latches status='escalated'"
        assert state["conversation"]["escalated_at"] == ESCALATED_AT, "step 1 stamps escalated_at"
        assert state["escalate_calls"] == 1, "the latch is written exactly once"
        assert ("conv-1", "user", "what is the meaning of life?") in state["persisted"]
        assert ("conv-1", "assistant", GENERIC_DEFERRAL) in state["persisted"]
        total += 1
        print("  escalate: first escalating turn latched status='escalated' + escalated_at, streamed the deferral")

        # Steps 2 & 3: two more customer messages — persisted + queued, NO pipeline.
        for n, msg in enumerate(["are you there?", "hello??"], start=2):
            state["persisted"].clear()
            r = _resumed_message(msg)
            assert r.status_code == 200, f"msg {n} must succeed, got {r.status_code} {r.text}"
            events = _events(r.text)
            assert events == ["conversation", "done"], (
                f"FAILURE INDICATOR: an escalated conversation must NOT stream a bot "
                f"answer (msg {n} events were {events})"
            )
            assert "event: delta" not in r.text, f"no delta on escalated msg {n}"
            assert state["turn_calls"] == 1, (
                f"FAILURE INDICATOR: the bot re-answered after escalation (pipeline ran "
                f"again on msg {n}; turn_calls={state['turn_calls']})"
            )
            assert (("conv-1", "user", msg) in state["persisted"]), (
                f"msg {n} persists to the queue (role='user')"
            )
            assert not any(role == "assistant" for _, role, _ in state["persisted"]), (
                f"msg {n} produces NO bot reply"
            )
            assert state["escalate_calls"] == 1, (
                f"FAILURE INDICATOR: escalated_at must not be re-latched on msg {n} "
                f"(escalate_calls={state['escalate_calls']})"
            )
            assert state["conversation"]["escalated_at"] == ESCALATED_AT, (
                f"FAILURE INDICATOR: escalated_at reset/unset on msg {n}"
            )
        total += 1
        print("  routing: the two later messages persisted + queued with the pipeline NEVER re-run; escalated_at preserved")

        # ===== EXPLICIT 'talk to a human' button latches via the SAME path (AC3). =====
        state["conversation"] = _fresh_active()
        state["persisted"].clear()
        state["turn_calls"], state["escalate_calls"] = 0, 0

        # No token → 401 (the iframe starts fresh).
        r = client.post("/widget/conversations/escalate", headers={"Origin": LISTED})
        assert r.status_code == 401, "explicit escalate requires a conversation token"

        # With the token → latch, no pipeline, returns the escalated view.
        r = client.post(
            "/widget/conversations/escalate",
            headers={"Origin": LISTED, main._CONVERSATION_TOKEN_HEADER: TOKEN},
        )
        assert r.status_code == 200, f"explicit escalate must succeed, got {r.status_code} {r.text}"
        assert r.json()["conversation"]["status"] == "escalated", "the button latches status='escalated'"
        assert state["escalate_calls"] == 1, "the button writes the latch once"
        assert state["turn_calls"] == 0, (
            "FAILURE INDICATOR: the explicit button must NOT run the deflection pipeline "
            "(it is UI-initiated, never a model tool)"
        )
        total += 1
        print("  explicit: 'talk to a human' latched via the same path WITHOUT running the pipeline")

        # A message after the button → pipeline still skipped (bot silent).
        state["persisted"].clear()
        r = _resumed_message("ok I'll wait")
        assert _events(r.text) == ["conversation", "done"], "post-button message streams no answer"
        assert state["turn_calls"] == 0, "the bot stays silent after the explicit escalate"
        assert ("conv-1", "user", "ok I'll wait") in state["persisted"], "the message still queues"
        total += 1
        print("  explicit: a message after the button routed to the queue with no bot answer")

        # ===== IN-FLIGHT race (Risk #3): a concurrent escalation latching WHILE the
        #       bot is mid-pipeline suppresses the in-flight CONFIDENT answer. The
        #       entry-snapshot short-circuit cannot catch this (the conversation was
        #       `active` at entry); the post-turn status re-read closes the window. =====
        state["conversation"] = _fresh_active()
        state["persisted"].clear()
        state["turn_calls"], state["escalate_calls"] = 0, 0
        state["next_result"] = _answered()       # the bot DID draft a confident answer…
        state["midflight_status"] = "escalated"  # …but a human took over while it thought.

        r = _first_message("how do I reset my password?")
        assert r.status_code == 200, f"the turn must still close cleanly, got {r.status_code} {r.text}"
        events = _events(r.text)
        assert events == ["conversation", "done"], (
            "FAILURE INDICATOR: an in-flight confident answer must be SUPPRESSED when a "
            f"concurrent escalation latched mid-pipeline (events were {events})"
        )
        assert "event: delta" not in r.text, "the suppressed answer streams NO delta events"
        assert ANSWER not in r.text, (
            "FAILURE INDICATOR: the confident answer must not reach the customer after handoff"
        )
        assert state["turn_calls"] == 1, "the turn DID run (the race is post-turn, pre-delivery)"
        assert not any(role == "assistant" for _, role, _ in state["persisted"]), (
            "FAILURE INDICATOR: the suppressed bot answer must NOT be persisted as an assistant row"
        )
        assert ("conv-1", "user", "how do I reset my password?") in state["persisted"], (
            "the customer message still queued for the human"
        )
        total += 1
        print("  in-flight: a confident answer is suppressed (no assistant row, no deltas) when escalation latches mid-pipeline")

        # Positive control: the SAME confident answer streams normally when there is NO
        # concurrent escalation — the guard must not over-suppress a valid answer.
        state["conversation"] = _fresh_active()
        state["persisted"].clear()
        state["turn_calls"] = 0
        state["next_result"] = _answered()
        state["midflight_status"] = "active"
        r = _first_message("how do I reset my password?")
        assert _collect_deltas(r.text) == ANSWER, (
            "a confident answer with NO mid-flight escalation must stream normally (no over-suppression)"
        )
        assert ("conv-1", "assistant", ANSWER) in state["persisted"], (
            "the answer is persisted when the conversation is not escalated"
        )
        total += 1
        print("  in-flight: the same answer streams normally with no concurrent escalation (no over-suppression)")

        print(
            f"OK: US-080 integration passed — {total} scenarios; the first escalate "
            "latches status='escalated'/escalated_at and silences the bot, later "
            "messages persist + queue without re-running the pipeline (escalated_at "
            "preserved), and the explicit button latches via the same path"
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
