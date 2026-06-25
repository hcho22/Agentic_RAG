"""US-070 validation test: support-bot retrieval mints a per-turn bot token and
calls match_chunks AS the bot, with the active-workspace narrowing filter.

Drives the **real** `support_bot.run_bot_deflection_turn` (and through it the real
`escalation.run_deflection_pipeline` + `retrieval.hybrid_search`) end to end. The
Supabase RPCs run through an `httpx.MockTransport` that captures, per request, the
`Authorization` bearer and the JSON body — so the test can assert, against the
real call, that:

  * a fresh bot token is minted EXACTLY ONCE per turn via the injected minter,
    with `sub = bot_user_id` and the ~60s TTL, and is the Bearer on BOTH the
    `match_chunks` and `keyword_search` calls (retrieval really runs as the bot);
  * the conversation's `workspace_id` is passed as the ordinary `filter_workspace_id`
    body param on BOTH legs — a non-security narrowing filter distinct from the
    bearer that carries identity;
  * NO bot token is cached across turns (two turns => two distinct mints);
  * the bot token NEVER appears in the returned `DeflectionResult` (no SSE /
    response-body leak surface) nor in a propagated retrieval error;
  * the deflection decision still flows: chunks the bot can see => answered;
    nothing visible => escalate with the generic deferral.

The embedder / answerer / judge are call-counting fakes and the minter is a fake,
so there is no real LLM, no DB, and no JWT secret — it runs anywhere. The LIVE RLS
claim ("the bot sees only share-to-bot docs; the filter narrows non-securely") is
pinned separately against a real Postgres in
`test_us070_bot_retrieval_integration.py`.

Run:
    python -m backend.test_us070_bot_retrieval
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from typing import Any, cast

import httpx
from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from escalation import GENERIC_DEFERRAL, DeflectionResult, EscalationConfig  # noqa: E402
from support_bot import (  # noqa: E402
    BOT_TOKEN_TTL_SECONDS,
    build_bot_supabase_headers,
    run_bot_deflection_turn,
)

SUPABASE_URL = "http://supabase.test"
ANON_KEY = "anon-key-public-not-an-identity"
BOT_USER_ID = "11111111-1111-1111-1111-111111111111"
WORKSPACE_ID = "22222222-2222-2222-2222-222222222222"
CONFIG = EscalationConfig(tau_sim=0.4, n_min=2, faithfulness_cutoff=0.7)
MATCH_THRESHOLD = 0.3


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


# --- fake minter ----------------------------------------------------------


class FakeMinter:
    """Stand-in for US-068 `mint_supabase_jwt`. Records every call and returns a
    distinct, recognisable token each time so the test can prove freshness and
    trace exactly which token reached the wire."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def __call__(self, sub: str, ttl_seconds: int) -> str:
        self.calls.append((sub, ttl_seconds))
        # A token a real JWT would never collide with, unique per call.
        return f"BOTJWT.sub={sub}.ttl={ttl_seconds}.n={len(self.calls)}"

    @property
    def last_token(self) -> str:
        n = len(self.calls)
        sub, ttl = self.calls[-1]
        return f"BOTJWT.sub={sub}.ttl={ttl}.n={n}"


# --- RPC rows + capturing transport ---------------------------------------


def _row(chunk_id: str, similarity: float, *, keyword: bool = False) -> dict[str, Any]:
    return {
        "id": chunk_id,
        "document_id": f"doc-{chunk_id}",
        "chunk_index": 0,
        "content": f"content {chunk_id}",
        "similarity": similarity,
        "filename": f"{chunk_id}.txt",
        "granting_principal_id": None,
        "granting_principal_display": None if keyword else "shared-to-bot",
    }


STRONG = [_row("a", 0.70), _row("b", 0.60)]  # top1 0.70 >= tau, 2 >= thresh
EMPTY: list[dict[str, Any]] = []
KW = [_row("k", 4.0, keyword=True)]


class CapturingTransport:
    """Records (path, authorization, json-body) for every Supabase RPC and
    replies with the configured rows. Optionally errors match_chunks to prove no
    token leaks through a propagated exception."""

    def __init__(
        self,
        match_rows: list[dict[str, Any]],
        keyword_rows: list[dict[str, Any]],
        *,
        match_status: int = 200,
    ) -> None:
        self.match_rows = match_rows
        self.keyword_rows = keyword_rows
        self.match_status = match_status
        self.captured: list[dict[str, Any]] = []
        self.counter = {"match": 0, "keyword": 0}

    def _record(self, request: httpx.Request, leg: str) -> None:
        import json

        body = json.loads(request.content.decode() or "{}")
        self.captured.append(
            {
                "leg": leg,
                "authorization": request.headers.get("authorization"),
                "apikey": request.headers.get("apikey"),
                "body": body,
            }
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/rpc/match_chunks"):
            self.counter["match"] += 1
            self._record(request, "match")
            if self.match_status != 200:
                # PostgREST-shaped error body; importantly it carries NO auth.
                return httpx.Response(
                    self.match_status, json={"message": "boom", "code": "XX000"}
                )
            return httpx.Response(200, json=self.match_rows)
        if path.endswith("/rpc/keyword_search"):
            self.counter["keyword"] += 1
            self._record(request, "keyword")
            return httpx.Response(200, json=self.keyword_rows)
        return httpx.Response(404, json={"message": f"unexpected {path}"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


# --- fake LLM clients (mirror test_deflection_pipeline.py) -----------------


class _FakeEmbeddings:
    async def create(self, model: str, input: list[str]) -> Any:  # noqa: A002
        data = [types.SimpleNamespace(index=i, embedding=[0.1, 0.2, 0.3]) for i in range(len(input))]
        return types.SimpleNamespace(data=data)


class _FakeEmbedder:
    embeddings = _FakeEmbeddings()


class _AnswererCompletions:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    async def create(self, **kwargs: Any) -> Any:
        self.calls += 1
        message = types.SimpleNamespace(content=self.content)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


class _FakeAnswerer:
    def __init__(self, content: str) -> None:
        self.chat = types.SimpleNamespace(completions=_AnswererCompletions(content))


class _JudgeCompletions:
    def __init__(self, parsed: Any) -> None:
        self.parsed = parsed
        self.calls = 0

    async def parse(self, *, model: str, messages: Any, response_format: Any) -> Any:
        self.calls += 1
        message = types.SimpleNamespace(parsed=self.parsed, refusal=None)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


class _FakeJudge:
    def __init__(self, supported: bool, score: float) -> None:
        from escalation import FaithfulnessJudgment

        parsed = FaithfulnessJudgment(supported=supported, score=score)
        self.chat = types.SimpleNamespace(completions=_JudgeCompletions(parsed))


def _turn(
    transport: CapturingTransport,
    minter: FakeMinter,
    *,
    draft: str = "Our return window is 30 days from delivery.",
    judge_supported: bool = True,
    judge_score: float = 0.95,
    message: str = "What is your return policy?",
    bot_user_id: str = BOT_USER_ID,
    workspace_id: str = WORKSPACE_ID,
) -> DeflectionResult:
    answerer = _FakeAnswerer(draft)
    judge = _FakeJudge(judge_supported, judge_score)

    async def go() -> DeflectionResult:
        async with httpx.AsyncClient(transport=transport.transport()) as http:
            return await run_bot_deflection_turn(
                mint_token=minter,
                anon_key=ANON_KEY,
                bot_user_id=bot_user_id,
                workspace_id=workspace_id,
                embedder_client=cast(AsyncOpenAI, _FakeEmbedder()),
                answerer_client=cast(AsyncOpenAI, answerer),
                judge_client=cast(AsyncOpenAI, judge),
                http=http,
                supabase_url=SUPABASE_URL,
                message=message,
                config=CONFIG,
                match_threshold=MATCH_THRESHOLD,
            )

    return asyncio.run(go())


# --- tests ----------------------------------------------------------------


def test_mints_per_turn_and_retrieves_as_bot() -> None:
    """One turn: mint called exactly once with (bot_user_id, 60s); the minted
    token is the Bearer on BOTH RPC legs; the apikey is the (non-identity) anon
    key; and the conversation's workspace_id rides as filter_workspace_id on
    both legs."""
    t = CapturingTransport(STRONG, KW)
    minter = FakeMinter()
    result = _turn(t, minter)

    _check(len(minter.calls) == 1, f"exactly ONE mint per turn, got {minter.calls}")
    _check(
        minter.calls[0] == (BOT_USER_ID, BOT_TOKEN_TTL_SECONDS),
        f"mint must use sub=bot_user_id and ~60s TTL, got {minter.calls[0]}",
    )
    expected_bearer = f"Bearer {minter.last_token}"
    _check(t.counter == {"match": 1, "keyword": 1}, f"hybrid once, got {t.counter}")
    for cap in t.captured:
        _check(
            cap["authorization"] == expected_bearer,
            f"{cap['leg']} must run as the bot (bearer = minted token), got {cap['authorization']!r}",
        )
        _check(
            cap["apikey"] == ANON_KEY,
            f"{cap['leg']} apikey must be the public anon key, got {cap['apikey']!r}",
        )
        _check(
            cap["body"].get("filter_workspace_id") == WORKSPACE_ID,
            f"{cap['leg']} must carry filter_workspace_id={WORKSPACE_ID}, body={cap['body']}",
        )
    _check(result.action == "answered", f"shared chunks => answered, got {result.action}")
    print("ok: one mint per turn (sub=bot, 60s); bot bearer + filter_workspace_id on both legs")


def test_no_cross_turn_token_caching() -> None:
    """Two turns reuse the same minter: each turn mints a FRESH token (two calls,
    two distinct tokens). There is no cache of bot tokens across turns (US-070)."""
    minter = FakeMinter()
    _turn(CapturingTransport(STRONG, KW), minter)
    first_token = minter.last_token
    t2 = CapturingTransport(STRONG, KW)
    _turn(t2, minter)
    second_token = minter.last_token

    _check(len(minter.calls) == 2, f"two turns => two mints, got {len(minter.calls)}")
    _check(first_token != second_token, "each turn must mint a DISTINCT token (no caching)")
    _check(
        t2.captured[0]["authorization"] == f"Bearer {second_token}",
        "the second turn must use the second (fresh) token, not a cached first one",
    )
    print("ok: no cross-turn caching — two turns mint two distinct bot tokens")


def test_bot_token_never_in_result() -> None:
    """The bot token must not appear in ANY field of the returned DeflectionResult
    — that result is the only thing a caller may serialise toward the customer
    (SSE / response body), so a leak there is the failure indicator."""
    t = CapturingTransport(STRONG, KW)
    minter = FakeMinter()
    result = _turn(t, minter)
    token = minter.last_token

    # The token DID reach the wire (proves it's a real, sensitive value)...
    _check(any(c["authorization"] == f"Bearer {token}" for c in t.captured), "token must have been used")
    # ...but appears nowhere a client could see it.
    blob = result.model_dump_json()
    _check(token not in blob, "bot token leaked into DeflectionResult JSON (SSE/response surface)")
    _check(token not in repr(result), "bot token leaked into DeflectionResult repr")
    _check(token not in result.customer_message, "bot token leaked into customer_message")
    _check(token not in (result.reason or ""), "bot token leaked into reason")
    print("ok: bot token reaches the wire but is absent from the DeflectionResult")


def test_bot_token_not_in_propagated_error() -> None:
    """If match_chunks fails, the error propagates (the caller/US-080 decides how
    to fail closed) but must NOT carry the bot token in its string — logging the
    exception must never leak the bearer."""
    t = CapturingTransport(STRONG, KW, match_status=500)
    minter = FakeMinter()
    raised: Exception | None = None
    try:
        _turn(t, minter)
    except Exception as e:  # noqa: BLE001 — we are asserting on the error text
        raised = e
    _check(raised is not None, "a 500 from match_chunks must propagate")
    _check(len(minter.calls) == 1, "token still minted exactly once even on failure")
    token = minter.last_token
    _check(token not in str(raised), f"bot token leaked into error str: {raised}")
    _check(token not in repr(raised), "bot token leaked into error repr")
    print("ok: retrieval error propagates without leaking the bot token")


def test_no_visible_chunks_escalates() -> None:
    """When the bot can see nothing (RLS/grant returns no rows — simulated as
    empty match + empty keyword), retrieval is weak so the turn escalates with the
    generic deferral and makes ZERO draft/judge calls. This is the unit-level
    shadow of the PRD step-2 case (answer lives only in a NOT-shared doc)."""
    t = CapturingTransport(EMPTY, EMPTY)
    minter = FakeMinter()
    answerer = _FakeAnswerer("should never be drafted")
    judge = _FakeJudge(True, 1.0)

    async def go() -> DeflectionResult:
        async with httpx.AsyncClient(transport=t.transport()) as http:
            return await run_bot_deflection_turn(
                mint_token=minter,
                anon_key=ANON_KEY,
                bot_user_id=BOT_USER_ID,
                workspace_id=WORKSPACE_ID,
                embedder_client=cast(AsyncOpenAI, _FakeEmbedder()),
                answerer_client=cast(AsyncOpenAI, answerer),
                judge_client=cast(AsyncOpenAI, judge),
                http=http,
                supabase_url=SUPABASE_URL,
                message="Tell me the secret in the doc you cannot see.",
                config=CONFIG,
                match_threshold=MATCH_THRESHOLD,
            )

    result = asyncio.run(go())
    _check(result.action == "escalated", f"no visible chunks => escalate, got {result.action}")
    _check(result.customer_message == GENERIC_DEFERRAL, "must show the generic deferral verbatim")
    _check(
        cast(_AnswererCompletions, answerer.chat.completions).calls == 0,
        "weak retrieval must make ZERO draft calls",
    )
    _check(t.captured[0]["body"].get("filter_workspace_id") == WORKSPACE_ID, "filter still applied")
    print("ok: no visible chunks => escalate (generic deferral), workspace filter still applied")


def test_build_bot_supabase_headers_shape() -> None:
    """The header builder puts the JWT in Authorization (identity) and the anon
    key in apikey (gateway, non-identity) — never the reverse."""
    headers = build_bot_supabase_headers("THE.JWT", "the-anon-key")
    _check(headers["Authorization"] == "Bearer THE.JWT", "JWT must be the Bearer")
    _check(headers["apikey"] == "the-anon-key", "anon key must be the apikey")
    _check("THE.JWT" not in headers["apikey"], "the JWT must not appear in apikey")
    print("ok: header builder — JWT in Authorization, anon key in apikey")


def main() -> int:
    tests = [
        test_mints_per_turn_and_retrieves_as_bot,
        test_no_cross_turn_token_caching,
        test_bot_token_never_in_result,
        test_bot_token_not_in_propagated_error,
        test_no_visible_chunks_escalates,
        test_build_bot_supabase_headers_shape,
    ]
    for t in tests:
        t()
    print(f"\nPASS: {len(tests)} US-070 support-bot retrieval test groups")
    return 0


if __name__ == "__main__":
    sys.exit(main())
