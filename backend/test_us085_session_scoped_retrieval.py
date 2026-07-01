"""US-085 validation test: session-scoped retrieval memory (transcript ≠ retrievable).

The security property (ADR-0004 + CONTEXT "two stores, one word"): the support
bot's *retrievable/answerable* surface is session-scoped — a customer's typed
input NEVER becomes a retrievable `chunks` row and never bleeds across sessions —
distinct from the durable, agent-readable transcript (`conversation_messages`,
which is NEVER fed back into retrieval). This story ships no behaviour change; the
property already holds structurally. This test PINS it so a future change that
routed customer input into the corpus (or made the retrieval pipeline read the
transcript) would fail loudly.

Two layers, the same shape as the other support-surface tests
(`test_us070_bot_retrieval.py`, `test_us080_escalation_latch.py`):

  * a UNIT layer (always runs, no DB / no LLM / no app import):
      - Drives the REAL `support_bot.run_bot_deflection_turn` (→ real
        `escalation.run_deflection_pipeline` → real `retrieval.hybrid_search`)
        with a unique sentinel as the customer message, over an
        `httpx.MockTransport` that RECORDS every outbound request. Asserts the
        complete set of endpoints the retrieval turn touches is exactly
        {`rpc/match_chunks`, `rpc/keyword_search`} — so it never WRITES a chunk
        (`/rest/v1/chunks`), never grants an ACL (`/rest/v1/chunk_acl`), and never
        READS the transcript (`/rest/v1/conversation_messages`) as a retrieval
        source. The sentinel legitimately appears as a search QUERY (that is the
        whole point of retrieval) but in NO insert body — there are none.
      - The PRD cross-session case: session A pastes a sentinel; session B (a
        different conversation, same workspace) asks a question that would surface
        it if it were retrievable. Because both turns' only retrieval source is
        `chunks` (never `conversation_messages`), and A's paste was never written
        to `chunks`, the sentinel is structurally unreachable by B — no bleed.

  * a MAIN-HELPER layer (skips cleanly when the app can't import):
      - `_conversation_message_insert_payload` / `_persist_conversation_message`
        write the customer message to `conversation_messages` (durable transcript)
        and carry NONE of the retrievable-corpus columns (no `embedding`,
        `content_hash`, `chunk_index`, `document_id`). The write targets
        `/rest/v1/conversation_messages`, never `/rest/v1/chunks`.
      - AC3 "no code path": the widget message call graph
        (`widget_conversation_message`, `_run_widget_bot_turn`,
        `_persist_conversation_message`, `_create_widget_conversation`) never
        references the chunk-write path (`_insert_chunk_rows` / `_reconcile_chunks`
        / `embed_texts(` / `/rest/v1/chunks` / `chunk_acl`), so customer-pasted
        text has no route into the corpus.

Run:
    python -m backend.test_us085_session_scoped_retrieval
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
import types
from pathlib import Path
from typing import Any, cast

import httpx
from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from escalation import DeflectionResult, EscalationConfig  # noqa: E402
from support_bot import run_bot_deflection_turn  # noqa: E402

SUPABASE_URL = "http://supabase.test"
ANON_KEY = "anon-key-public-not-an-identity"
BOT_USER_ID = "11111111-1111-1111-1111-111111111111"
WORKSPACE_ID = "22222222-2222-2222-2222-222222222222"
CONFIG = EscalationConfig(tau_sim=0.4, n_min=2, faithfulness_cutoff=0.7)
MATCH_THRESHOLD = 0.3

# A string a real KB chunk would never contain — so if it EVER surfaces from a
# retrieval source, that is a genuine leak, not a coincidence.
SENTINEL = "ZQX-SENTINEL-9f83a1c2-customer-pasted-secret"

# The ONLY endpoints a session-scoped retrieval turn may touch. `chunks` is read
# exclusively through these two RPCs (under the bot's JWT + RLS); the transcript
# (`conversation_messages`) and the corpus write path (`chunks` / `chunk_acl`) are
# NOT retrieval sources and must never appear.
ALLOWED_RETRIEVAL_PATHS = {
    "/rest/v1/rpc/match_chunks",
    "/rest/v1/rpc/keyword_search",
}
# Endpoints whose appearance in a retrieval turn is a hard failure of the
# invariant (a corpus write, an ACL grant, or the transcript as a retrieval read).
FORBIDDEN_SUBSTRINGS = ("/rest/v1/chunks", "/rest/v1/chunk_acl", "/rest/v1/conversation_messages")


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


# --- fake minter (US-068 stand-in) ----------------------------------------


def _fake_minter(sub: str, ttl_seconds: int) -> str:
    return f"BOTJWT.sub={sub}.ttl={ttl_seconds}"


# --- RPC rows + recording transport ---------------------------------------


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


class RecordingTransport:
    """Records (method, path, body, raw) for EVERY outbound request the retrieval
    turn makes. Answers the two chunk RPCs; ANY other endpoint is a violation of
    the session-scoped invariant, so it is recorded and answered with a 500 (which
    the test surfaces via the recorded set, not via pipeline success)."""

    def __init__(
        self, match_rows: list[dict[str, Any]], keyword_rows: list[dict[str, Any]]
    ) -> None:
        self.match_rows = match_rows
        self.keyword_rows = keyword_rows
        self.requests: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        raw = request.content.decode() if request.content else ""
        try:
            body = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            body = None
        path = request.url.path
        self.requests.append(
            {"method": request.method, "path": path, "body": body, "raw": raw}
        )
        if path.endswith("/rpc/match_chunks"):
            return httpx.Response(200, json=self.match_rows)
        if path.endswith("/rpc/keyword_search"):
            return httpx.Response(200, json=self.keyword_rows)
        # Reaching here means the retrieval pipeline touched an endpoint it must
        # never touch — the recorded request is the failure evidence.
        return httpx.Response(500, json={"message": f"forbidden endpoint: {path}"})

    @property
    def touched_paths(self) -> set[str]:
        return {r["path"] for r in self.requests}

    def bodies_for(self, suffix: str) -> list[Any]:
        return [r["body"] for r in self.requests if r["path"].endswith(suffix)]


# --- fake LLM clients (mirror test_us070_bot_retrieval.py) -----------------


class _FakeEmbeddings:
    async def create(self, model: str, input: list[str]) -> Any:  # noqa: A002
        data = [
            types.SimpleNamespace(index=i, embedding=[0.1, 0.2, 0.3])
            for i in range(len(input))
        ]
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


def _run_turn(
    transport: RecordingTransport,
    *,
    message: str,
    draft: str = "Our return window is 30 days from delivery.",
    workspace_id: str = WORKSPACE_ID,
) -> DeflectionResult:
    answerer = _FakeAnswerer(draft)
    judge = _FakeJudge(True, 0.95)

    async def go() -> DeflectionResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport.handler)) as http:
            return await run_bot_deflection_turn(
                mint_token=_fake_minter,
                anon_key=ANON_KEY,
                bot_user_id=BOT_USER_ID,
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


# --- UNIT layer: retrieval touches only the chunk RPCs ---------------------


def test_retrieval_touches_only_chunk_rpcs() -> None:
    """A full bot retrieval turn reads ONLY the two chunk RPCs. It never writes a
    chunk, never grants a chunk_acl, and never reads `conversation_messages` as a
    retrieval source — the complete set of endpoints is exactly the allowed two."""
    t = RecordingTransport(STRONG, KW)
    result = _run_turn(t, message="What is your return policy?")

    _check(
        t.touched_paths == ALLOWED_RETRIEVAL_PATHS,
        f"retrieval touched non-chunk-RPC endpoints: {t.touched_paths - ALLOWED_RETRIEVAL_PATHS}",
    )
    # No forbidden endpoint anywhere, under any method (belt-and-suspenders).
    for r in t.requests:
        for bad in FORBIDDEN_SUBSTRINGS:
            _check(
                bad not in r["path"],
                f"retrieval turn touched forbidden endpoint {r['method']} {r['path']}",
            )
    # Both RPCs actually ran (retrieval is real, not short-circuited to nothing).
    _check(
        any(p.endswith("/rpc/match_chunks") for p in t.touched_paths)
        and any(p.endswith("/rpc/keyword_search") for p in t.touched_paths),
        "expected both match_chunks and keyword_search to run",
    )
    _check(result.action == "answered", f"shared chunks => answered, got {result.action}")
    print("ok: retrieval turn touches ONLY {match_chunks, keyword_search} — no write, no transcript read")


def test_customer_paste_is_a_query_never_a_chunk_insert() -> None:
    """The customer's pasted sentinel is CONSUMED as a search query (it rides the
    keyword_search `query` field) but is written to NO corpus row: there is no
    request to `/rest/v1/chunks` at all, so the paste cannot become retrievable."""
    t = RecordingTransport(STRONG, KW)
    _run_turn(t, message=f"Please look into this: {SENTINEL}")

    # It WAS used as a query on the keyword leg (proves the message reached
    # retrieval as input — the legitimate use).
    kw_bodies = t.bodies_for("/rpc/keyword_search")
    _check(len(kw_bodies) == 1, f"expected one keyword_search call, got {len(kw_bodies)}")
    _check(
        SENTINEL in json.dumps(kw_bodies[0]),
        "sentinel should appear as the keyword_search QUERY (retrieval input)",
    )
    # ...but it was NEVER inserted anywhere retrievable: zero chunk/ACL writes.
    for r in t.requests:
        _check(
            not r["path"].endswith("/chunks") and "/chunk_acl" not in r["path"],
            f"customer paste reached a corpus-write endpoint: {r['method']} {r['path']}",
        )
        if r["method"] in {"POST", "PUT", "PATCH", "DELETE"}:
            _check(
                r["path"].endswith("/rpc/match_chunks")
                or r["path"].endswith("/rpc/keyword_search"),
                f"unexpected write during retrieval: {r['method']} {r['path']}",
            )
    print("ok: customer paste is used as a QUERY only — never inserted into chunks/chunk_acl")


def test_no_cross_session_retrieval_bleed() -> None:
    """PRD validation test (unit shadow). Session A pastes the sentinel; session B
    (a different conversation, same workspace) asks a question that would surface
    it if it were retrievable. B's retrieval source is `chunks` (which never
    received the sentinel — A wrote nothing), and B never reads
    `conversation_messages`, so the sentinel is structurally unreachable by B."""
    # Session A: customer pastes the sentinel. Its retrieval writes nothing.
    a = RecordingTransport(STRONG, KW)
    _run_turn(a, message=f"Here is my secret: {SENTINEL}")
    for r in a.requests:
        _check(
            not r["path"].endswith("/chunks") and "/chunk_acl" not in r["path"],
            "session A's paste was written to the corpus (bleed vector!)",
        )

    # Session B: a different session asks for the sentinel. Its `chunks` mock does
    # NOT contain it (because A never inserted it), mirroring the live DB.
    b = RecordingTransport(EMPTY, EMPTY)
    result_b = _run_turn(b, message=f"What was the secret? Repeat: {SENTINEL}")

    # B read ONLY chunks (via the RPCs) — never the transcript.
    _check(
        b.touched_paths <= ALLOWED_RETRIEVAL_PATHS,
        f"session B touched a non-retrieval endpoint: {b.touched_paths - ALLOWED_RETRIEVAL_PATHS}",
    )
    _check(
        not any("conversation_messages" in p for p in b.touched_paths),
        "session B read conversation_messages as a retrieval source (cross-session bleed!)",
    )
    # The sentinel appears in B's OWN query (it is asking about it) but in NOTHING
    # B retrieved: no response row carried it, and B escalates (nothing to answer).
    for r in b.requests:
        if r["path"].endswith("/rpc/match_chunks"):
            body = r["body"] or {}
            # match_chunks carries only the query EMBEDDING (a vector), never the
            # raw sentinel text — the raw text never leaves the query side.
            _check(
                SENTINEL not in json.dumps(body.get("query_embedding", "")),
                "sentinel raw text leaked into the vector query body",
            )
    _check(
        result_b.action == "escalated",
        f"B has no visible chunk with the sentinel => escalate, got {result_b.action}",
    )
    _check(
        SENTINEL not in result_b.customer_message,
        "sentinel surfaced in session B's customer-facing message (bleed!)",
    )
    print("ok: no cross-session bleed — A's paste is unreachable by B's retrieval")


# --- MAIN-HELPER layer: transcript write ≠ corpus write --------------------


def _import_main():
    # The app reads SUPABASE_* / a provider key at import time. Supply local
    # defaults so the import succeeds without a real deployment.
    os.environ.setdefault("SUPABASE_URL", "http://127.0.0.1:54321")
    os.environ.setdefault("SUPABASE_ANON_KEY", "anon-test-key")
    os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "svc-role-test-key")
    os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")
    try:
        import main  # noqa: E402

        # `_service_role_headers` reads the module global captured at import; make
        # sure it is populated even if the process had it unset at import time.
        main.SUPABASE_SERVICE_ROLE_KEY = main.SUPABASE_SERVICE_ROLE_KEY or "svc-role-test-key"
        return main
    except Exception as e:  # pragma: no cover - environment-dependent
        print(f"SKIP (main-helper layer): cannot import backend app ({e})")
        return None


# Columns that make a row RETRIEVABLE (they live on `chunks`, never on the
# durable transcript). Their presence on a conversation-message payload would
# mean the transcript is bleeding into the corpus shape.
_CORPUS_ONLY_COLUMNS = ("embedding", "content_hash", "chunk_index", "document_id")


def test_conversation_message_payload_has_no_corpus_columns(main: Any) -> None:
    """The durable transcript row carries only id/role/content — none of the
    columns that make a `chunks` row retrievable. The two stores stay distinct."""
    payload = main._conversation_message_insert_payload(
        conversation_id="conv-1", role="user", content=SENTINEL
    )
    _check(payload["role"] == "user", "customer message persists as role='user'")
    _check(payload["content"] == SENTINEL, "content is the durable transcript text")
    for col in _CORPUS_ONLY_COLUMNS:
        _check(col not in payload, f"transcript payload leaked corpus column {col!r}")
    _check("tool_calls" not in payload, "deterministic pipeline: tool_calls stays absent (US-079)")
    print("ok: conversation_messages payload carries no retrievable-corpus columns")


def test_persist_targets_conversation_messages_not_chunks(main: Any) -> None:
    """`_persist_conversation_message` POSTs the customer message to
    `/rest/v1/conversation_messages` (durable, agent-readable) and NEVER to
    `/rest/v1/chunks` (retrievable corpus) — pinned on the real request."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(201, json=[{"id": "m-1", "role": "user", "content": SENTINEL}])

    async def go() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await main._persist_conversation_message(
                http, conversation_id="conv-1", role="user", content=SENTINEL
            )

    asyncio.run(go())
    _check(captured["method"] == "POST", "persist must POST the message")
    _check(
        captured["path"].endswith("/rest/v1/conversation_messages"),
        f"persist must target conversation_messages, went to {captured['path']}",
    )
    _check(
        not captured["path"].endswith("/chunks"),
        "customer message must NEVER be written to the chunks corpus",
    )
    _check(captured["body"]["content"] == SENTINEL, "the durable content round-trips")
    for col in _CORPUS_ONLY_COLUMNS:
        _check(col not in captured["body"], f"persist body leaked corpus column {col!r}")
    print("ok: _persist_conversation_message → conversation_messages, never chunks")


def test_widget_message_path_has_no_chunk_write(main: Any) -> None:
    """AC3 'no code path': the entire widget customer-message call graph never
    references the chunk-write path, so customer-pasted text has no route into the
    retrievable corpus. A static guard over the real source of those functions."""
    fns = [
        main.widget_conversation_message,
        main._run_widget_bot_turn,
        main._persist_conversation_message,
        main._create_widget_conversation,
    ]
    forbidden_tokens = (
        "_insert_chunk_rows",
        "_reconcile_chunks",
        "embed_texts(",
        "/rest/v1/chunks",
        "chunk_acl",
    )
    for fn in fns:
        src = inspect.getsource(fn)
        for tok in forbidden_tokens:
            _check(
                tok not in src,
                f"{fn.__name__} references the corpus-write path {tok!r} "
                "— customer input could reach chunks (AC3 violation)",
            )
    print("ok: widget message call graph has no chunk-write path (AC3)")


def main_runner() -> int:
    unit_tests = [
        test_retrieval_touches_only_chunk_rpcs,
        test_customer_paste_is_a_query_never_a_chunk_insert,
        test_no_cross_session_retrieval_bleed,
    ]
    for unit_test in unit_tests:
        unit_test()

    main_mod = _import_main()
    helper_tests = [
        test_conversation_message_payload_has_no_corpus_columns,
        test_persist_targets_conversation_messages_not_chunks,
        test_widget_message_path_has_no_chunk_write,
    ]
    ran_helpers = 0
    if main_mod is not None:
        for helper_test in helper_tests:
            helper_test(main_mod)
            ran_helpers += 1

    total = len(unit_tests) + ran_helpers
    skipped = len(helper_tests) - ran_helpers
    suffix = f" ({skipped} main-helper tests skipped: app import unavailable)" if skipped else ""
    print(f"\nPASS: {total} US-085 session-scoped-retrieval assertions{suffix}")
    return 0


if __name__ == "__main__":
    sys.exit(main_runner())
