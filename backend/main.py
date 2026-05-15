"""FastAPI backend for the Agentic RAG app.

US-004 scope:
  * POST /api/chat streams an OpenAI Responses API reply (with optional
    file_search retrieval) back to the client via Server-Sent Events.
  * Supabase JWT from the browser is validated against GoTrue and then
    forwarded to PostgREST so row-level security still applies to every
    DB mutation.
  * Each turn: persist the user message, stream the assistant reply,
    persist the assistant message, update threads.openai_thread_id with
    the new response id so the next turn can continue server-side.

US-011: same endpoint now branches on `mode`. `responses` keeps the
managed-thread behaviour above; `completions` swaps in the stateless
Chat Completions API, rebuilds conversation context from the Supabase
messages table, and runs a manual tool-call loop that exposes the
`search_documents` tool (US-010).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import AsyncIterator, Literal

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from langsmith import traceable
from langsmith.run_helpers import get_current_run_tree
from langsmith.wrappers import wrap_openai
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from chunking import chunk_text, get_chunk_config
from embeddings import embed_texts, get_embedding_model, to_pgvector
from metadata import extract_document_metadata, get_metadata_model
from parsing import UnsupportedFormatError, parse_document, warmup as warmup_parsing
from permissions import (
    AclGrant,
    PrincipalType,
    ShareSummary,
    grant_doc_to_principal,
    list_doc_shares,
    replay_doc_acls,
    revoke_doc_from_principal,
    snapshot_doc_acls,
)
from reranking import (
    build_reranker,
    get_rerank_input_k,
    get_reranker_name,
    rerank_with_timing,
)
from retrieval import (
    METADATA_SCHEMA_HINT,
    SearchDocumentsInput,
    SearchDocumentsResult,
    get_retrieval_mode,
    get_rrf_k,
    get_similarity_threshold,
    hybrid_search,
    keyword_only_search,
    keyword_search,
    search_documents,
    search_documents_tool_schema,
)
from text_to_sql import (
    QueryDatabaseInput,
    SqlSafetyError,
    get_allowed_schemas,
    get_analytics_database_url,
    get_schema_snapshot,
    is_enabled as sql_tool_enabled,
    query_database,
    query_database_tool_schema,
)
from web_search import (
    WebSearchInput,
    get_web_search_timeout_s,
    is_enabled as web_search_tool_enabled,
    web_search,
    web_search_tool_schema,
)
from subagent import (
    SPAWN_DOCUMENT_AGENT_PROMPT_BLOCK,
    SpawnDocumentAgentInput,
    detect_full_document_intent,
    get_intent_threshold,
    run_document_subagent,
    spawn_document_agent_tool_schema,
)
from semantic_layer import (
    SemanticLayer,
    SemanticLayerError,
    load_and_validate as load_semantic_layer,
)
from planner import (
    PlanMatched,
    PlanNoMatch,
    PlanQueryInput,
    plan_query,
    plan_query_tool_schema,
)
from sql_compiler import (
    CompileError,
    SqlSearchInput,
    is_enabled as crm_tool_enabled,
    sql_search,
    sql_search_tool_schema,
)

load_dotenv()

log = logging.getLogger("agentic_rag.backend")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

_REQUIRED_ENV = ("SUPABASE_URL", "SUPABASE_ANON_KEY", "OPENAI_API_KEY")
_missing = [k for k in _REQUIRED_ENV if not os.environ.get(k)]
if _missing:
    raise RuntimeError(
        "missing required environment variable(s): "
        + ", ".join(_missing)
        + ". Set them on the Railway service (Variables tab) and redeploy."
    )

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_ANON_KEY = os.environ["SUPABASE_ANON_KEY"]
# US-039: optional. Used only for the doc-owner authorization check on the
# share endpoints — it lets the backend distinguish "you're not the owner"
# (403) from "doc doesn't exist" (404) without depending on whether the
# caller has any RLS-visible row. If unset, the share endpoints fall back
# to the user-JWT lookup, which collapses 403 → 404 for callers who can't
# see the doc at all (still secure, just less precise).
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or None
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_VECTOR_STORE_ID = os.environ.get("OPENAI_VECTOR_STORE_ID") or None
FRONTEND_ORIGINS = [
    o.strip()
    for o in os.environ.get("FRONTEND_ORIGIN", "http://localhost:5173").split(",")
    if o.strip()
]

ChatMode = Literal["responses", "completions"]

_DEFAULT_CHAT_MODE_RAW = os.environ.get("CHAT_MODE_DEFAULT", "responses").strip().lower()
if _DEFAULT_CHAT_MODE_RAW not in ("responses", "completions"):
    raise ValueError(
        f"CHAT_MODE_DEFAULT must be 'responses' or 'completions', got {_DEFAULT_CHAT_MODE_RAW!r}"
    )
DEFAULT_CHAT_MODE: ChatMode = _DEFAULT_CHAT_MODE_RAW  # type: ignore[assignment]

# US-011: cap the Chat Completions tool-call loop so a misbehaving model can't
# spin forever. PRD Technical Considerations section pins this at 5.
MAX_TOOL_ITERATIONS = 5

# US-012: sliding-window size for the stateless Chat Completions history
# rebuild. A "turn" is one user message plus everything that followed it
# (assistant replies, tool-call intermediates, tool results) until the next
# user message. Default 20; env-configurable.
_DEFAULT_HISTORY_TURNS = 20
try:
    CHAT_HISTORY_MAX_TURNS = int(os.environ.get("CHAT_HISTORY_MAX_TURNS", _DEFAULT_HISTORY_TURNS))
except ValueError as e:
    raise ValueError("CHAT_HISTORY_MAX_TURNS must be an integer") from e
if CHAT_HISTORY_MAX_TURNS < 0:
    raise ValueError("CHAT_HISTORY_MAX_TURNS must be >= 0")
COMPLETIONS_SYSTEM_PROMPT_BASE = (
    "You are a helpful assistant with access to the user's ingested documents "
    "via the `search_documents` tool. Prefer calling the tool to ground your "
    "answer whenever the question might be answerable from the user's own "
    "documents. When you cite tool results, mention the document filename. If "
    "no relevant chunks are returned, answer from general knowledge and say so.\n\n"
    # US-017: tell the agent what structured metadata is available so it can
    # decide when to pass `filters` to search_documents.
    + METADATA_SCHEMA_HINT
)

# US-023: appended to the system prompt only when the text-to-SQL tool is
# configured (ANALYTICS_DATABASE_URL set). Includes the live schema snapshot
# so the agent picks the right tool based on the question type.
COMPLETIONS_PLAN_QUERY_PROMPT = (
    "\n\nYou have a two-step structured-data path for quantitative questions "
    "(totals, counts, aggregates by dimension, gross margin, customer counts) "
    "over the business `crm` schema:\n"
    "  1. Call `plan_query(question)` first. It returns either "
    "{status: \"matched\", plan: ...} when the question maps onto the semantic "
    "layer, or {status: \"no_match\", reason, suggested_fallback} when it doesn't.\n"
    "  2. If matched, call `sql_search(plan=<that plan>, row_limit=...)`. You "
    "CANNOT call `sql_search` without a plan — it requires the structured "
    "object from step 1.\n"
    "  3. If no_match with suggested_fallback=\"file_search\", call "
    "`search_documents` next. If suggested_fallback=\"web_search\" and that "
    "tool is enabled, call `web_search`. Otherwise tell the user the "
    "question is out of scope for the structured business data and explain "
    "the reason briefly.\n"
    "Prefer `search_documents` for free-text questions about uploaded "
    "documents; only enter the plan_query path when the question is "
    "clearly about quantitative business data."
)

# US-024: appended only when the web search tool is configured. The routing
# rule (prefer local retrieval first, fall back to web on empty results) is
# stated here AND in the tool description because models sometimes skip
# system-prompt detail when many tools are available.
COMPLETIONS_WEB_SEARCH_PROMPT = (
    "\n\nYou also have a `web_search` tool for current events and public "
    "facts that aren't in the user's local documents. Routing rules: ALWAYS "
    "try `search_documents` first. Only call `web_search` when "
    "`search_documents` returns no relevant chunks (empty results or none "
    "above the similarity threshold), OR when the question is obviously "
    "about current events / breaking news. Do not use `web_search` for "
    "questions whose answer is plausibly in the user's corpus. When you "
    "cite a web result, include the URL in your reply so the user can click "
    "through."
)


def _build_completions_system_prompt(
    schema_snapshot: str | None,
    *,
    full_document_intent: bool = False,
) -> str:
    """Compose the chat system prompt, appending tool-specific blocks.

    `full_document_intent` is set per-turn (US-026) by the heuristic in
    `subagent.detect_full_document_intent`. When True, an explicit hint is
    appended that nudges the model to prefer `spawn_document_agent` over
    `search_documents` for this turn — saying it twice (here + tool
    description) hardens against the model skipping the system prompt when
    many tools are visible.
    """
    prompt = COMPLETIONS_SYSTEM_PROMPT_BASE
    # US-030: plan_query + sql_search replace query_database. Gated on
    # crm_tool_enabled() (which checks CRM_DATABASE_URL → ANALYTICS_DATABASE_URL)
    # AND a successfully loaded semantic layer — without both, the agent
    # shouldn't see the structured path because sql_search would fail at
    # execution time. `schema_snapshot` is unused here (the planner reads
    # the semantic layer, not the raw schema dump) but the parameter stays
    # for API stability — eval code and tests pass it in.
    if crm_tool_enabled() and _SEMANTIC_LAYER is not None:
        prompt += COMPLETIONS_PLAN_QUERY_PROMPT
    if web_search_tool_enabled():
        prompt += COMPLETIONS_WEB_SEARCH_PROMPT
    # Sub-agent block is unconditional (the tool is always registered) —
    # see Module 8 in the PRD. Intent hint is per-turn.
    prompt += SPAWN_DOCUMENT_AGENT_PROMPT_BLOCK
    if full_document_intent:
        prompt += (
            "\n\n[Hint: this turn's user message looks like a full-document "
            "task — strongly prefer `spawn_document_agent` over "
            "`search_documents` unless the question is clearly chunk-level.]"
        )
    return prompt


# Cached at startup (and refreshable via `_refresh_sql_schema_snapshot`) so we
# don't pay the introspection round-trip on every chat turn. None means "tool
# disabled or introspection failed" — the prompt builder falls back gracefully.
_SQL_SCHEMA_SNAPSHOT: str | None = None

# US-029: structured-RAG semantic layer. Validated and loaded once at startup.
# US-030's planner and compiler will read from this; until then it just
# guarantees the YAML matches the live crm schema.
_SEMANTIC_LAYER: SemanticLayer | None = None

# LangSmith: when LANGSMITH_API_KEY is set the SDK auto-ships traces for every
# wrapped OpenAI call and every @traceable function. When it's missing,
# wrap_openai/traceable both become no-ops so local dev stays free of spurious
# auth errors.
LANGSMITH_API_KEY = os.environ.get("LANGSMITH_API_KEY") or None
LANGSMITH_PROJECT = os.environ.get("LANGSMITH_PROJECT", "agentic-rag")
if LANGSMITH_API_KEY:
    os.environ.setdefault("LANGSMITH_TRACING", "true")
    os.environ["LANGSMITH_PROJECT"] = LANGSMITH_PROJECT
else:
    os.environ["LANGSMITH_TRACING"] = "false"

openai_client = wrap_openai(AsyncOpenAI(api_key=OPENAI_API_KEY))

app = FastAPI(title="Agentic RAG backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=FRONTEND_ORIGINS,
    allow_methods=["POST", "GET", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _on_startup() -> None:
    # US-018: front-load the docling DocumentConverter so the first
    # file-upload ingest doesn't pay multi-second import + backend init on the
    # request path. Failures are swallowed — the lazy path still works.
    warmup_parsing()
    # US-023: introspect the analytics schema once at startup so the system
    # prompt + tool description don't pay an extra DB round-trip per chat
    # turn. Empty result on failure means the prompt falls back to a
    # "ask the user for table names" message.
    global _SQL_SCHEMA_SNAPSHOT, _SEMANTIC_LAYER
    db_url = get_analytics_database_url()
    if db_url:
        try:
            _SQL_SCHEMA_SNAPSHOT = await get_schema_snapshot(db_url, get_allowed_schemas())
            log.info(
                "text_to_sql.snapshot_loaded chars=%d",
                len(_SQL_SCHEMA_SNAPSHOT or ""),
            )
        except Exception:  # noqa: BLE001
            log.exception("text_to_sql.snapshot_load_failed")
            _SQL_SCHEMA_SNAPSHOT = None

    # US-029: load + validate the semantic layer. A broken layer must stop
    # the app from coming up — wrong SQL at query time is worse than a
    # noisy startup failure. SemanticLayerError surfaces verbatim.
    try:
        _SEMANTIC_LAYER = await load_semantic_layer()
        log.info(
            "semantic_layer loaded — %d entities, %d dimensions, %d metrics, %d joins",
            len(_SEMANTIC_LAYER.entities),
            len(_SEMANTIC_LAYER.dimensions),
            len(_SEMANTIC_LAYER.metrics),
            len(_SEMANTIC_LAYER.joins),
        )
    except SemanticLayerError:
        log.exception("semantic_layer.load_failed")
        raise


class ChatRequest(BaseModel):
    thread_id: str = Field(..., description="Supabase threads.id")
    message: str = Field(..., min_length=1)
    mode: ChatMode | None = Field(
        default=None,
        description=(
            "'responses' uses OpenAI's managed Responses API (default for US-004). "
            "'completions' uses Chat Completions with the search_documents tool "
            "(US-011). Defaults to CHAT_MODE_DEFAULT when omitted."
        ),
    )


class AuthedUser(BaseModel):
    id: str
    access_token: str


async def get_user(authorization: str | None = Header(default=None)) -> AuthedUser:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    async with httpx.AsyncClient(timeout=10.0) as http:
        r = await http.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {token}"},
        )
    if r.status_code != 200:
        raise HTTPException(status_code=401, detail="invalid supabase session")
    data = r.json()
    return AuthedUser(id=data["id"], access_token=token)


def _service_role_headers() -> dict[str, str] | None:
    """Headers that bypass RLS — used only by the doc-owner authorization
    check on the share endpoints (US-039). Returns None when no service role
    key is configured so callers can fall back to user-scoped reads."""
    if not SUPABASE_SERVICE_ROLE_KEY:
        return None
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _supabase_headers(user: AuthedUser) -> dict[str, str]:
    return {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {user.access_token}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


async def _fetch_thread(http: httpx.AsyncClient, user: AuthedUser, thread_id: str) -> dict:
    r = await http.get(
        f"{SUPABASE_URL}/rest/v1/threads",
        params={"id": f"eq.{thread_id}", "select": "id,user_id,openai_thread_id"},
        headers=_supabase_headers(user),
    )
    r.raise_for_status()
    rows = r.json()
    if not rows:
        # RLS hides rows the user does not own, so this is indistinguishable
        # from "not found" — correct behaviour either way.
        raise HTTPException(status_code=404, detail="thread not found")
    return rows[0]


async def _insert_message(
    http: httpx.AsyncClient,
    user: AuthedUser,
    thread_id: str,
    role: str,
    content: str | None,
    *,
    tool_calls: list[dict] | None = None,
    tool_call_id: str | None = None,
    name: str | None = None,
) -> dict:
    """Insert a row into public.messages under the user's JWT.

    Extra columns (`tool_calls`, `tool_call_id`, `name`) are US-012 additions —
    left None for simple user/assistant turns, populated for the Chat
    Completions tool-call loop so the full conversation can be rebuilt from
    Supabase on the next turn / after a page refresh.
    """
    payload: dict = {"thread_id": thread_id, "role": role, "content": content}
    if tool_calls is not None:
        payload["tool_calls"] = tool_calls
    if tool_call_id is not None:
        payload["tool_call_id"] = tool_call_id
    if name is not None:
        payload["name"] = name
    r = await http.post(
        f"{SUPABASE_URL}/rest/v1/messages",
        headers=_supabase_headers(user),
        json=payload,
    )
    r.raise_for_status()
    return r.json()[0]


async def _update_thread_openai_id(
    http: httpx.AsyncClient,
    user: AuthedUser,
    thread_id: str,
    openai_thread_id: str,
) -> None:
    r = await http.patch(
        f"{SUPABASE_URL}/rest/v1/threads",
        params={"id": f"eq.{thread_id}"},
        headers=_supabase_headers(user),
        json={"openai_thread_id": openai_thread_id},
    )
    r.raise_for_status()


def _build_tools() -> list[dict] | None:
    if not OPENAI_VECTOR_STORE_ID:
        return None
    return [
        {
            "type": "file_search",
            "vector_store_ids": [OPENAI_VECTOR_STORE_ID],
        }
    ]


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8")


def _attach_run_metadata(**fields: str | None) -> None:
    """Merge metadata into the current LangSmith run, if tracing is active."""
    run = get_current_run_tree()
    if run is None:
        return
    clean = {k: v for k, v in fields.items() if v is not None}
    if clean:
        run.add_metadata(clean)


@traceable(run_type="chain", name="chat_turn_responses")
async def _stream_responses_reply(
    user: AuthedUser,
    thread_id: str,
    message: str,
) -> AsyncIterator[bytes]:
    _attach_run_metadata(user_id=user.id, thread_id=thread_id, mode="responses")

    async with httpx.AsyncClient(timeout=30.0) as http:
        try:
            thread = await _fetch_thread(http, user, thread_id)
            user_msg = await _insert_message(http, user, thread_id, "user", message)
            _attach_run_metadata(user_message_id=user_msg["id"])
        except HTTPException as e:
            yield _sse("error", {"message": e.detail})
            return
        except httpx.HTTPStatusError as e:
            log.exception("supabase precheck failed")
            yield _sse("error", {"message": f"supabase: {e.response.text[:200]}"})
            return

        tools = _build_tools()
        previous_response_id = thread.get("openai_thread_id")

        kwargs: dict = {"model": OPENAI_MODEL, "input": message, "stream": True}
        if tools:
            kwargs["tools"] = tools
        if previous_response_id:
            kwargs["previous_response_id"] = previous_response_id

        full_text_parts: list[str] = []
        final_response_id: str | None = None

        try:
            stream = await openai_client.responses.create(**kwargs)
            async for event in stream:
                etype = getattr(event, "type", "")
                if etype == "response.output_text.delta":
                    delta = getattr(event, "delta", "") or ""
                    if delta:
                        full_text_parts.append(delta)
                        yield _sse("delta", {"text": delta})
                elif etype == "response.completed":
                    resp = getattr(event, "response", None)
                    if resp is not None:
                        final_response_id = getattr(resp, "id", None)
                elif etype == "response.error":
                    err = getattr(event, "error", None)
                    msg = getattr(err, "message", "openai stream error") if err else "openai stream error"
                    log.error("openai stream error: %s", msg)
                    yield _sse("error", {"message": msg})
                    return
        except Exception as e:  # noqa: BLE001 — surface anything OpenAI throws
            log.exception("openai responses call failed")
            yield _sse("error", {"message": f"openai: {e}"})
            return

        full_text = "".join(full_text_parts)
        try:
            assistant_msg = await _insert_message(
                http, user, thread_id, "assistant", full_text
            )
            if final_response_id:
                await _update_thread_openai_id(http, user, thread_id, final_response_id)
        except httpx.HTTPStatusError as e:
            log.exception("supabase persistence failed")
            yield _sse("error", {"message": f"supabase: {e.response.text[:200]}"})
            return

        _attach_run_metadata(
            assistant_message_id=assistant_msg["id"],
            response_id=final_response_id,
        )

        yield _sse(
            "done",
            {
                "message_id": assistant_msg["id"],
                "response_id": final_response_id,
            },
        )


async def _load_prior_messages(
    http: httpx.AsyncClient, user: AuthedUser, thread_id: str
) -> list[dict]:
    """Fetch every persisted message row for the thread (RLS-scoped).

    Ordered ascending by `created_at` so the caller can treat the list as a
    forward transcript. The sliding-window trim is applied in
    `_apply_history_window`; we fetch eagerly here because threads are short
    and the projection is small.
    """
    r = await http.get(
        f"{SUPABASE_URL}/rest/v1/messages",
        params={
            "thread_id": f"eq.{thread_id}",
            "select": "id,role,content,tool_calls,tool_call_id,name,created_at",
            "order": "created_at.asc",
        },
        headers=_supabase_headers(user),
    )
    r.raise_for_status()
    return r.json()


def _apply_history_window(prior: list[dict], max_turns: int) -> list[dict]:
    """Trim `prior` to the last `max_turns` user-message-rooted turns.

    A turn starts at a `user` row and includes every row up to (but not
    including) the next `user` row — so the assistant reply + any tool
    intermediates + tool results that belong to a turn stay together. If
    `max_turns == 0`, returns `[]`; if there are fewer than `max_turns`
    turns, returns `prior` unchanged.
    """
    if max_turns <= 0:
        return []
    user_idx = [i for i, m in enumerate(prior) if m.get("role") == "user"]
    if len(user_idx) <= max_turns:
        return prior
    cutoff = user_idx[-max_turns]
    return prior[cutoff:]


def _prior_to_completions(prior: list[dict]) -> list[dict]:
    """Project persisted rows into the Chat Completions `messages` shape.

    Handles four role cases:
      * `user` → `{role, content}`
      * plain `assistant` (no tool_calls) → `{role, content}`
      * `assistant` with tool_calls → `{role, content, tool_calls}` where
        `tool_calls` is the OpenAI-format list we stored verbatim.
      * `tool` → `{role, tool_call_id, content}` (and optional `name`).

    Orphan-avoidance: if an assistant turn with tool_calls is followed by tool
    rows whose `tool_call_id`s don't match, OpenAI rejects the whole request.
    We collect the pairing on the fly and drop any orphaned `tool` row (e.g.
    from a truncated window) rather than let the request 400.
    """
    out: list[dict] = []
    pending_tool_call_ids: set[str] = set()
    for m in prior:
        role = m.get("role")
        content = m.get("content")
        if role == "user":
            if content:
                out.append({"role": "user", "content": content})
            pending_tool_call_ids = set()
        elif role == "assistant":
            tool_calls = m.get("tool_calls")
            if tool_calls:
                entry: dict = {
                    "role": "assistant",
                    "content": content if content else None,
                    "tool_calls": tool_calls,
                }
                out.append(entry)
                pending_tool_call_ids = {
                    tc.get("id") for tc in tool_calls if tc.get("id")
                }
            elif content:
                out.append({"role": "assistant", "content": content})
                pending_tool_call_ids = set()
        elif role == "tool":
            tcid = m.get("tool_call_id")
            if tcid and tcid in pending_tool_call_ids:
                entry = {
                    "role": "tool",
                    "tool_call_id": tcid,
                    "content": content or "",
                }
                if m.get("name"):
                    entry["name"] = m["name"]
                out.append(entry)
                pending_tool_call_ids.discard(tcid)
    # If we ended with an assistant tool_calls turn that has unanswered ids
    # (e.g. the last turn errored out mid-loop), drop that assistant entry so
    # the next request doesn't 400 on mismatched tool_call_ids.
    if pending_tool_call_ids:
        for i in range(len(out) - 1, -1, -1):
            if out[i].get("role") == "assistant" and out[i].get("tool_calls"):
                out.pop(i)
                break
    return out


async def _retrieve_for_agent(
    *,
    http: httpx.AsyncClient,
    user: AuthedUser,
    query: str,
    top_k: int,
    filters: object,
) -> tuple[list[SearchDocumentsResult], str, str]:
    """Full agent-tool retrieval pipeline: search (US-021) + optional rerank (US-022).

    Returns `(results, retrieval_mode, reranker_name)`. When the reranker is
    `none`, the search backend returns `top_k` directly. When a reranker is
    configured, the search backend pulls a wider candidate pool
    (`RERANK_INPUT_K`, default 20) and the reranker trims to `top_k`.

    Centralising this here keeps the chat tool path and `/api/search/rerank`
    in lockstep — the validation test in the PRD compares hybrid-only vs
    hybrid+rerank against the same code path the agent actually uses.
    """
    mode = get_retrieval_mode()
    reranker_name = get_reranker_name()

    pool_k = top_k if reranker_name == "none" else max(get_rerank_input_k(), top_k)

    if mode == "hybrid":
        candidates = await hybrid_search(
            openai_client=openai_client,
            http=http,
            supabase_url=SUPABASE_URL,
            supabase_headers=_supabase_headers(user),
            query=query,
            top_k=pool_k,
            filters=filters,  # type: ignore[arg-type]
        )
    elif mode == "keyword":
        candidates = await keyword_only_search(
            openai_client=openai_client,
            http=http,
            supabase_url=SUPABASE_URL,
            supabase_headers=_supabase_headers(user),
            query=query,
            top_k=pool_k,
            filters=filters,  # type: ignore[arg-type]
        )
    else:
        candidates = await search_documents(
            openai_client=openai_client,
            http=http,
            supabase_url=SUPABASE_URL,
            supabase_headers=_supabase_headers(user),
            query=query,
            top_k=pool_k,
            filters=filters,  # type: ignore[arg-type]
        )

    if reranker_name == "none":
        return candidates, mode, reranker_name

    reranker = build_reranker(reranker_name, http=http, openai_client=openai_client)
    results = await rerank_with_timing(reranker, query, candidates, top_k)
    return results, mode, reranker_name


async def _execute_tool_call(
    http: httpx.AsyncClient,
    user: AuthedUser,
    name: str,
    raw_arguments: str,
) -> str:
    """Dispatch a tool call produced by the Chat Completions API.

    Returns a JSON string suitable for the `tool` message's `content` field.
    Any error is serialised into the payload so the model can see it rather
    than the whole turn failing — matches OpenAI's recommended pattern.
    """
    try:
        args = json.loads(raw_arguments) if raw_arguments else {}
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"invalid tool arguments json: {e}"})

    if name == "search_documents":
        try:
            validated = SearchDocumentsInput(**args)
            results, mode, reranker_name = await _retrieve_for_agent(
                http=http,
                user=user,
                query=validated.query,
                top_k=validated.top_k,
                filters=validated.filters,
            )
            return json.dumps(
                {
                    "results": [r.model_dump() for r in results],
                    "count": len(results),
                    "retrieval_mode": mode,
                    "reranker": reranker_name,
                    "similarity_threshold": get_similarity_threshold(),
                }
            )
        except Exception as e:  # noqa: BLE001 — surface to model as tool error
            log.exception("search_documents tool failed")
            return json.dumps({"error": str(e)})

    if name == "plan_query":
        # US-030 step 1: map NL to a PlanSpec via OpenAI function-calling.
        # Returns matched (plan ready) or no_match (with suggested_fallback)
        # so the agent's next step is explicit in the result payload.
        try:
            validated_plan = PlanQueryInput(**args)
            if _SEMANTIC_LAYER is None:
                return json.dumps({"error": "semantic layer not loaded"})
            result = await plan_query(
                openai_client=openai_client,
                question=validated_plan.question,
                layer=_SEMANTIC_LAYER,
            )
            return json.dumps(result.model_dump())
        except Exception as e:  # noqa: BLE001 — surface to model as tool error
            log.exception("plan_query tool failed")
            return json.dumps({"error": str(e)})

    if name == "sql_search":
        # US-030 step 2: compile + execute. The tool schema requires `plan`,
        # so the agent can't reach this branch without a planner run. We
        # still defensively reject if the layer somehow isn't loaded.
        try:
            validated_search = SqlSearchInput(**args)
            if _SEMANTIC_LAYER is None:
                return json.dumps({"error": "semantic layer not loaded"})
            result = await sql_search(
                plan=validated_search.plan,
                layer=_SEMANTIC_LAYER,
                row_limit=validated_search.row_limit,
            )
            return json.dumps(result.model_dump())
        except CompileError as e:
            log.warning("sql_search compile failed: %s", e)
            return json.dumps({"error": f"compile: {e}"})
        except SqlSafetyError as e:
            log.exception("sql_search safety violation: %s", e)
            return json.dumps({"error": f"unsafe sql: {e}"})
        except Exception as e:  # noqa: BLE001 — surface to model as tool error
            log.exception("sql_search tool failed")
            return json.dumps({"error": str(e)})

    if name == "web_search":
        # US-024: fallback to public web when local retrieval misses. We hand
        # the model an empty result list (not an error) on provider failure
        # so the agent gracefully falls through to general knowledge with a
        # disclaimer, rather than aborting the turn over a vendor outage.
        try:
            validated_web = WebSearchInput(**args)
            results = await web_search(
                http=http,
                query=validated_web.query,
                top_k=validated_web.top_k,
            )
            return json.dumps(
                {
                    "results": [r.model_dump() for r in results],
                    "count": len(results),
                }
            )
        except Exception as e:  # noqa: BLE001 — surface to model as tool error
            log.exception("web_search tool failed")
            return json.dumps({"error": str(e), "results": [], "count": 0})

    if name == "spawn_document_agent":
        # US-027: delegate full-document tasks to a sub-agent with isolated
        # context (its own message list, scoped to one document, two tools).
        # Failures are caught and serialised so the parent agent sees a tool
        # error rather than the whole turn aborting.
        try:
            validated_sub = SpawnDocumentAgentInput(**args)
            result = await run_document_subagent(
                openai_client=openai_client,
                http=http,
                supabase_url=SUPABASE_URL,
                supabase_headers=_supabase_headers(user),
                document_id=validated_sub.document_id,
                task=validated_sub.task,
            )
            return json.dumps(result.model_dump())
        except Exception as e:  # noqa: BLE001 — surface to model as tool error
            log.exception("spawn_document_agent tool failed")
            return json.dumps({"error": str(e)})

    return json.dumps({"error": f"unknown tool: {name}"})


@traceable(run_type="chain", name="chat_turn_completions")
async def _stream_completions_reply(
    user: AuthedUser,
    thread_id: str,
    message: str,
) -> AsyncIterator[bytes]:
    _attach_run_metadata(user_id=user.id, thread_id=thread_id, mode="completions")

    async with httpx.AsyncClient(timeout=60.0) as http:
        try:
            await _fetch_thread(http, user, thread_id)
            prior = await _load_prior_messages(http, user, thread_id)
            user_msg = await _insert_message(http, user, thread_id, "user", message)
            _attach_run_metadata(user_message_id=user_msg["id"])
        except HTTPException as e:
            yield _sse("error", {"message": e.detail})
            return
        except httpx.HTTPStatusError as e:
            log.exception("supabase precheck failed")
            yield _sse("error", {"message": f"supabase: {e.response.text[:200]}"})
            return

        windowed = _apply_history_window(prior, CHAT_HISTORY_MAX_TURNS)
        # US-026: heuristic-driven nudge that flips the system prompt toward
        # `spawn_document_agent` when the user's message looks like a
        # full-document task. The threshold + score live in `subagent.py`;
        # we only attach the metadata + system-prompt hint here.
        intent_score = detect_full_document_intent(message)
        full_document_intent = intent_score >= get_intent_threshold()
        _attach_run_metadata(
            full_document_intent_score=str(intent_score),
            full_document_intent=str(full_document_intent).lower(),
        )
        system_prompt = _build_completions_system_prompt(
            _SQL_SCHEMA_SNAPSHOT,
            full_document_intent=full_document_intent,
        )
        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        messages.extend(_prior_to_completions(windowed))
        messages.append({"role": "user", "content": message})

        tools = [search_documents_tool_schema()]
        # US-030: plan_query + sql_search replace query_database. Both are
        # registered together (sql_search is useless without plan_query) and
        # gated on a live semantic layer plus a CRM DB URL. The old
        # query_database tool is no longer exposed to the agent; its naive
        # generator stays available as a library function for the US-031 eval.
        if crm_tool_enabled() and _SEMANTIC_LAYER is not None:
            tools.append(plan_query_tool_schema())
            tools.append(sql_search_tool_schema())
        if web_search_tool_enabled():
            tools.append(web_search_tool_schema())
        # US-027: sub-agent tool is always registered — full-document tasks
        # are common enough that the cost of one extra tool slot in every
        # turn is worth the simplification.
        tools.append(spawn_document_agent_tool_schema())
        final_assistant_msg: dict | None = None

        for _iteration in range(MAX_TOOL_ITERATIONS):
            try:
                stream = await openai_client.chat.completions.create(
                    model=OPENAI_MODEL,
                    messages=messages,
                    tools=tools,
                    stream=True,
                )
            except Exception as e:  # noqa: BLE001
                log.exception("openai chat.completions call failed")
                yield _sse("error", {"message": f"openai: {e}"})
                return

            iter_content_parts: list[str] = []
            tool_calls_acc: dict[int, dict] = {}
            finish_reason: str | None = None

            try:
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    delta = choice.delta
                    if delta.content:
                        iter_content_parts.append(delta.content)
                        yield _sse("delta", {"text": delta.content})
                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            idx = tc.index
                            slot = tool_calls_acc.setdefault(
                                idx, {"id": "", "name": "", "arguments": ""}
                            )
                            if tc.id:
                                slot["id"] = tc.id
                            if tc.function is not None:
                                if tc.function.name:
                                    slot["name"] = tc.function.name
                                if tc.function.arguments:
                                    slot["arguments"] += tc.function.arguments
                    if choice.finish_reason:
                        finish_reason = choice.finish_reason
            except Exception as e:  # noqa: BLE001
                log.exception("openai chat.completions stream failed")
                yield _sse("error", {"message": f"openai: {e}"})
                return

            iter_content = "".join(iter_content_parts)

            if finish_reason == "tool_calls" and tool_calls_acc:
                tool_calls_list = [
                    {
                        "id": tool_calls_acc[k]["id"],
                        "type": "function",
                        "function": {
                            "name": tool_calls_acc[k]["name"],
                            "arguments": tool_calls_acc[k]["arguments"],
                        },
                    }
                    for k in sorted(tool_calls_acc.keys())
                ]
                # US-012: persist the intermediate assistant turn (content may
                # be empty — the model often calls a tool without preamble)
                # and every tool result so the next request / a page refresh
                # can rebuild the same conversation from Supabase.
                try:
                    await _insert_message(
                        http,
                        user,
                        thread_id,
                        "assistant",
                        iter_content or None,
                        tool_calls=tool_calls_list,
                    )
                except httpx.HTTPStatusError as e:
                    log.exception("supabase persistence failed (assistant tool_calls)")
                    yield _sse("error", {"message": f"supabase: {e.response.text[:200]}"})
                    return

                messages.append(
                    {
                        "role": "assistant",
                        "content": iter_content or None,
                        "tool_calls": tool_calls_list,
                    }
                )
                for tc_entry in tool_calls_list:
                    tool_name = tc_entry["function"]["name"]
                    tool_content = await _execute_tool_call(
                        http,
                        user,
                        tool_name,
                        tc_entry["function"]["arguments"],
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc_entry["id"],
                            "content": tool_content,
                        }
                    )
                    try:
                        await _insert_message(
                            http,
                            user,
                            thread_id,
                            "tool",
                            tool_content,
                            tool_call_id=tc_entry["id"],
                            name=tool_name,
                        )
                    except httpx.HTTPStatusError as e:
                        log.exception("supabase persistence failed (tool result)")
                        yield _sse(
                            "error",
                            {"message": f"supabase: {e.response.text[:200]}"},
                        )
                        return
                continue

            # Any other finish reason (stop/length/content_filter) means the
            # model produced its final answer — persist it and exit.
            try:
                final_assistant_msg = await _insert_message(
                    http, user, thread_id, "assistant", iter_content or None
                )
            except httpx.HTTPStatusError as e:
                log.exception("supabase persistence failed (final assistant)")
                yield _sse("error", {"message": f"supabase: {e.response.text[:200]}"})
                return
            break

        if final_assistant_msg is None:
            yield _sse(
                "error",
                {"message": f"tool-call loop exceeded {MAX_TOOL_ITERATIONS} iterations"},
            )
            return

        _attach_run_metadata(assistant_message_id=final_assistant_msg["id"])

        yield _sse(
            "done",
            {
                "message_id": final_assistant_msg["id"],
                "response_id": None,
            },
        )


@app.post("/api/chat")
async def chat(
    req: ChatRequest,
    request: Request,
    user: AuthedUser = Depends(get_user),
):
    mode: ChatMode = req.mode or DEFAULT_CHAT_MODE
    streamer = (
        _stream_responses_reply if mode == "responses" else _stream_completions_reply
    )

    async def gen() -> AsyncIterator[bytes]:
        async for chunk in streamer(user, req.thread_id, req.message):
            if await request.is_disconnected():
                return
            yield chunk

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/config")
async def get_config() -> dict:
    """Public config surface: the frontend uses this to seed its mode toggle."""
    return {
        "default_chat_mode": DEFAULT_CHAT_MODE,
        "supported_chat_modes": ["responses", "completions"],
        "file_search_enabled": bool(OPENAI_VECTOR_STORE_ID),
        "sql_tool_enabled": sql_tool_enabled(),
        # US-030: separate flag for the new plan_query + sql_search path.
        # Kept distinct from sql_tool_enabled (Module 7's query_database)
        # so the frontend can show the right card mix and the eval can run
        # the baseline path independently.
        "crm_tool_enabled": crm_tool_enabled() and _SEMANTIC_LAYER is not None,
        "web_search_tool_enabled": web_search_tool_enabled(),
        # US-026 / US-027: spawn_document_agent is always registered, so this
        # flag is `true` whenever the completions path is available. The UI
        # uses it to pre-register the badge style for the tree-attribution
        # tile and to know whether to surface the activity log inline.
        "subagent_tool_enabled": True,
    }


# -----------------------------------------------------------------------------
# US-008 + US-009: ingestion pipeline. US-007 handles upload + Storage blob;
# this endpoint picks up the uploaded document, reads the blob, chunks it,
# embeds each chunk (OpenAI batched + retried), and persists chunk rows with
# their pgvector embeddings. Retrieval tools land in US-010+.
# -----------------------------------------------------------------------------

DOCUMENT_COLUMNS = (
    "id,user_id,filename,storage_path,byte_size,content_type,status,"
    "error_message,chunks_count,uploaded_at,deleted_at,content_hash,metadata"
)


async def _fetch_document(
    http: httpx.AsyncClient, user: AuthedUser, document_id: str
) -> dict:
    r = await http.get(
        f"{SUPABASE_URL}/rest/v1/documents",
        params={"id": f"eq.{document_id}", "select": DOCUMENT_COLUMNS},
        headers=_supabase_headers(user),
    )
    r.raise_for_status()
    rows = r.json()
    if not rows:
        raise HTTPException(status_code=404, detail="document not found")
    return rows[0]


async def _download_storage_object(
    http: httpx.AsyncClient, user: AuthedUser, storage_path: str
) -> bytes:
    # Supabase Storage authenticated download — same JWT as PostgREST so the
    # bucket-level RLS policies from US-007 still apply.
    r = await http.get(
        f"{SUPABASE_URL}/storage/v1/object/documents/{storage_path}",
        headers={
            "apikey": SUPABASE_ANON_KEY,
            "Authorization": f"Bearer {user.access_token}",
        },
    )
    r.raise_for_status()
    return r.content


async def _delete_chunks(
    http: httpx.AsyncClient, user: AuthedUser, document_id: str
) -> None:
    # Idempotent re-ingestion: drop any prior chunks first so we don't hit the
    # (document_id, chunk_index) unique constraint.
    r = await http.delete(
        f"{SUPABASE_URL}/rest/v1/chunks",
        params={"document_id": f"eq.{document_id}"},
        headers=_supabase_headers(user),
    )
    r.raise_for_status()


def _hash_chunk(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _fetch_existing_chunks(
    http: httpx.AsyncClient, user: AuthedUser, document_id: str
) -> list[dict]:
    r = await http.get(
        f"{SUPABASE_URL}/rest/v1/chunks",
        params={
            "document_id": f"eq.{document_id}",
            "select": "id,chunk_index,content_hash,embedding",
        },
        headers=_supabase_headers(user),
    )
    r.raise_for_status()
    return r.json()


async def _insert_chunk_rows(
    http: httpx.AsyncClient,
    user: AuthedUser,
    rows: list[dict],
) -> None:
    if not rows:
        return
    BATCH = 200
    for i in range(0, len(rows), BATCH):
        batch = rows[i : i + BATCH]
        r = await http.post(
            f"{SUPABASE_URL}/rest/v1/chunks",
            headers={**_supabase_headers(user), "Prefer": "return=minimal"},
            json=batch,
        )
        r.raise_for_status()


async def _reconcile_chunks(
    http: httpx.AsyncClient,
    user: AuthedUser,
    document_id: str,
    chunks: list[str],
) -> dict[str, int]:
    """US-015: diff `chunks` against existing rows by SHA-256 and replace.

    Existing chunks whose content hash matches a new chunk have their
    embedding reused verbatim (pgvector round-trips as a string). Only new /
    modified chunks hit the OpenAI embeddings API. Pre-US-015 rows with a
    NULL `content_hash` are treated as "not reusable" and get re-embedded on
    their first post-migration ingest — one-time backfill via normal use.

    We fetch existing rows first, then embed, then delete-and-reinsert so a
    mid-pipeline failure never leaves half-written chunks behind (matches the
    pre-US-015 safety boundary).

    Returns per-position metrics: `chunks_added`, `chunks_removed`,
    `chunks_unchanged`, `chunks_total`.
    """
    new_hashes = [_hash_chunk(c) for c in chunks]
    existing = await _fetch_existing_chunks(http, user, document_id)

    # First-seen-wins when the same hash appears on multiple existing rows
    # (repeated content, or a past ingestion glitch) — the embedding is a
    # pure function of the content, so any occurrence is interchangeable.
    hash_to_embedding: dict[str, str] = {}
    for row in existing:
        h = row.get("content_hash")
        emb = row.get("embedding")
        if h and emb and h not in hash_to_embedding:
            hash_to_embedding[h] = emb

    to_embed_texts: list[str] = []
    to_embed_positions: list[int] = []
    for i, (text, h) in enumerate(zip(chunks, new_hashes)):
        if h not in hash_to_embedding:
            to_embed_positions.append(i)
            to_embed_texts.append(text)

    new_embeddings: list[list[float]] = (
        await embed_texts(openai_client, to_embed_texts) if to_embed_texts else []
    )
    position_to_new_embedding = dict(zip(to_embed_positions, new_embeddings))

    rows: list[dict] = []
    for i, (text, h) in enumerate(zip(chunks, new_hashes)):
        reused = hash_to_embedding.get(h)
        embedding_value = reused if reused is not None else to_pgvector(
            position_to_new_embedding[i]
        )
        rows.append(
            {
                "document_id": document_id,
                "user_id": user.id,
                "chunk_index": i,
                "content": text,
                "content_hash": h,
                "embedding": embedding_value,
            }
        )

    await _delete_chunks(http, user, document_id)
    await _insert_chunk_rows(http, user, rows)

    new_hash_set = set(new_hashes)
    chunks_unchanged = sum(1 for h in new_hashes if h in hash_to_embedding)
    chunks_added = len(new_hashes) - chunks_unchanged
    chunks_removed = sum(
        1 for row in existing
        if not row.get("content_hash") or row["content_hash"] not in new_hash_set
    )

    return {
        "chunks_added": chunks_added,
        "chunks_removed": chunks_removed,
        "chunks_unchanged": chunks_unchanged,
        "chunks_total": len(rows),
    }


async def _patch_document(
    http: httpx.AsyncClient,
    user: AuthedUser,
    document_id: str,
    **fields: object,
) -> dict:
    r = await http.patch(
        f"{SUPABASE_URL}/rest/v1/documents",
        params={"id": f"eq.{document_id}", "select": DOCUMENT_COLUMNS},
        headers=_supabase_headers(user),
        json=fields,
    )
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else {}


@app.post("/api/documents/{document_id}/ingest")
async def ingest_document(
    document_id: str,
    user: AuthedUser = Depends(get_user),
) -> dict:
    async with httpx.AsyncClient(timeout=60.0) as http:
        doc = await _fetch_document(http, user, document_id)
        if not doc.get("storage_path"):
            raise HTTPException(
                status_code=400,
                detail="document has no storage_path; upload must finish first",
            )

        await _patch_document(http, user, document_id, status="processing", error_message=None)

        try:
            raw = await _download_storage_object(http, user, doc["storage_path"])
            # US-018: multi-format parsing via docling. `parse_document`
            # raises `UnsupportedFormatError` on unknown types and `ValueError`
            # with a human-readable message on parse failure — both are caught
            # by the outer except below and surfaced as `status=error` with
            # `error_message` so the UI can show why a given file failed.
            try:
                text = parse_document(
                    raw,
                    filename=doc.get("filename", ""),
                    content_type=doc.get("content_type"),
                )
            except UnsupportedFormatError as e:
                raise ValueError(str(e)) from e

            # US-014: backfill documents.content_hash on re-ingest of rows
            # that pre-date the hashing feature or were created by a non-UI
            # caller. The frontend already sets it on insert for new uploads.
            if not doc.get("content_hash"):
                await _patch_document(
                    http,
                    user,
                    document_id,
                    content_hash=hashlib.sha256(raw).hexdigest(),
                )

            chunks = chunk_text(text)

            # US-038: re-chunking caveat. Deleting chunks cascades and drops
            # every chunk_acl row, so we'd silently lose doc-level grants on
            # re-ingest. Snapshot the current grants per principal, journal
            # the snapshot to documents.metadata.pending_acl_replay so a
            # crash mid-flight is recoverable, run the chunk reconcile, then
            # re-grant per principal against the new chunks. On entry, prefer
            # an existing journal — that means a prior ingest crashed between
            # delete and replay and this run is the recovery path.
            doc_metadata = doc.get("metadata") or {}
            journaled = doc_metadata.get("pending_acl_replay")
            if journaled is not None:
                to_replay = [AclGrant(**g) for g in journaled]
                log.info(
                    "ingest.acl_recover document_id=%s grants=%d",
                    document_id,
                    len(to_replay),
                )
            else:
                to_replay = await snapshot_doc_acls(
                    http, SUPABASE_URL, _supabase_headers(user), document_id
                )
                if to_replay:
                    journal_metadata = {
                        **doc_metadata,
                        "pending_acl_replay": [g.model_dump() for g in to_replay],
                    }
                    await _patch_document(
                        http, user, document_id, metadata=journal_metadata
                    )

            # US-015: reconcile by content_hash so only new/changed chunks
            # hit the OpenAI embeddings API; unchanged chunks reuse the
            # embedding already in the DB. _reconcile_chunks is the
            # atomic-ish boundary (PostgREST has no real tx, but re-running
            # ingest stays idempotent via the delete-then-insert inside).
            metrics = await _reconcile_chunks(http, user, document_id, chunks)
            chunk_count = metrics["chunks_total"]
            log.info(
                "ingest.reconcile document_id=%s chunks_added=%d "
                "chunks_removed=%d chunks_unchanged=%d chunks_total=%d",
                document_id,
                metrics["chunks_added"],
                metrics["chunks_removed"],
                metrics["chunks_unchanged"],
                chunk_count,
            )

            if to_replay:
                replayed = await replay_doc_acls(
                    http,
                    SUPABASE_URL,
                    _supabase_headers(user),
                    document_id,
                    to_replay,
                )
                # Clear the journal so a future ingest doesn't think it's
                # still recovering. Re-fetch metadata so we don't clobber any
                # other writes that happened in this request.
                fresh = await _fetch_document(http, user, document_id)
                fresh_metadata = (fresh.get("metadata") or {}).copy()
                fresh_metadata.pop("pending_acl_replay", None)
                await _patch_document(
                    http,
                    user,
                    document_id,
                    metadata=fresh_metadata if fresh_metadata else None,
                )
                log.info(
                    "ingest.acl_replay document_id=%s principals=%d rows_inserted=%d",
                    document_id,
                    len(to_replay),
                    replayed,
                )

            # US-016: LLM-extracted structured metadata. Non-fatal by
            # design — a None return (network / parse / refusal) leaves
            # documents.metadata as-is (NULL on first ingest, or the prior
            # extraction on re-ingest) and the document still becomes
            # 'ready'. A warning has already been logged inside the helper.
            ready_fields: dict[str, object] = {
                "status": "ready",
                "chunks_count": chunk_count,
                "error_message": None,
            }
            extracted = await extract_document_metadata(
                openai_client, text, doc["filename"]
            )
            if extracted is not None:
                ready_fields["metadata"] = extracted.model_dump(mode="json")
                log.info(
                    "ingest.metadata document_id=%s title=%r topics=%s type=%r",
                    document_id,
                    extracted.title,
                    extracted.topics,
                    extracted.document_type,
                )

            updated = await _patch_document(
                http,
                user,
                document_id,
                **ready_fields,
            )
        except Exception as e:  # noqa: BLE001 — any failure marks the doc errored
            log.exception("ingestion failed for document %s", document_id)
            await _patch_document(
                http,
                user,
                document_id,
                status="error",
                error_message=str(e)[:500],
            )
            raise HTTPException(status_code=500, detail=f"ingestion failed: {e}") from e

    size, overlap = get_chunk_config()
    return {
        "document": updated,
        "chunks": chunk_count,
        "chunks_added": metrics["chunks_added"],
        "chunks_removed": metrics["chunks_removed"],
        "chunks_unchanged": metrics["chunks_unchanged"],
        "chunk_size_tokens": size,
        "chunk_overlap_tokens": overlap,
        "embedding_model": get_embedding_model(),
        "metadata_model": get_metadata_model(),
    }


# -----------------------------------------------------------------------------
# US-039: share endpoints. Thin REST layer over backend/permissions.py so the
# US-040 frontend share dialog has POST/GET/DELETE endpoints to call. Owner-
# only authorization is enforced by `_assert_doc_owner` below; the underlying
# operations all run via PostgREST under the caller's JWT so chunk_acl writes
# remain RLS-checked end-to-end (the doc-owner policies from US-038 cover the
# write path).
# -----------------------------------------------------------------------------


class ShareRequest(BaseModel):
    """Body for POST /api/documents/{id}/share — one identifier, two paths.

    The backend resolves `principal_email_or_name` against profiles.email
    first (user grant), then principals.name (group grant). 404 if neither
    matches. Free-text input — no autocomplete combobox.
    """

    principal_email_or_name: str = Field(
        ..., min_length=1,
        description="Email of an existing user, or name of an existing group.",
    )


async def _assert_doc_owner(
    http: httpx.AsyncClient, user: AuthedUser, doc_id: str
) -> dict:
    """Returns the doc when caller owns it; raises 403/404 otherwise.

    Uses service-role to read `documents.user_id` so the 403/404 distinction
    holds regardless of whether the caller has any RLS-visible row on the
    doc. Falls back to a user-scoped read when no service role key is
    configured — that path collapses 403 → 404 for callers who can't see
    the doc at all (still secure, just less precise).
    """
    service_headers = _service_role_headers()
    if service_headers is not None:
        r = await http.get(
            f"{SUPABASE_URL}/rest/v1/documents",
            params={"id": f"eq.{doc_id}", "select": "id,user_id,status"},
            headers=service_headers,
        )
        r.raise_for_status()
        rows = r.json()
        if not rows:
            raise HTTPException(status_code=404, detail="document not found")
        doc = rows[0]
        if doc["user_id"] != user.id:
            raise HTTPException(
                status_code=403, detail="not the document owner"
            )
        return doc
    # No service role: best we can do is the user-scoped read.
    r = await http.get(
        f"{SUPABASE_URL}/rest/v1/documents",
        params={"id": f"eq.{doc_id}", "select": "id,user_id,status"},
        headers=_supabase_headers(user),
    )
    r.raise_for_status()
    rows = r.json()
    if not rows:
        raise HTTPException(status_code=404, detail="document not found")
    doc = rows[0]
    if doc["user_id"] != user.id:
        raise HTTPException(status_code=403, detail="not the document owner")
    return doc


async def _resolve_principal(
    http: httpx.AsyncClient,
    supabase_headers: dict[str, str],
    identifier: str,
) -> tuple[PrincipalType, str, str] | None:
    """Try profiles.email, then principals.name. None → 404 at the endpoint.

    Returns (principal_type, principal_id, display_name). Reads under the
    caller's JWT — both tables have permissive select RLS (US-037) so any
    authenticated reader can resolve.
    """
    r = await http.get(
        f"{SUPABASE_URL}/rest/v1/profiles",
        params={"email": f"eq.{identifier}", "select": "id,email", "limit": "1"},
        headers=supabase_headers,
    )
    r.raise_for_status()
    rows = r.json()
    if rows:
        return ("user", rows[0]["id"], rows[0]["email"])

    r = await http.get(
        f"{SUPABASE_URL}/rest/v1/principals",
        params={"name": f"eq.{identifier}", "select": "id,name", "limit": "1"},
        headers=supabase_headers,
    )
    r.raise_for_status()
    rows = r.json()
    if rows:
        return ("group", rows[0]["id"], rows[0]["name"])

    return None


@app.post("/api/documents/{document_id}/share")
async def grant_share(
    document_id: str,
    req: ShareRequest,
    user: AuthedUser = Depends(get_user),
) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as http:
        doc = await _assert_doc_owner(http, user, document_id)
        if doc.get("status") != "ready":
            raise HTTPException(
                status_code=409, detail="Document is still ingesting"
            )

        headers = _supabase_headers(user)
        resolved = await _resolve_principal(
            http, headers, req.principal_email_or_name.strip()
        )
        if resolved is None:
            raise HTTPException(
                status_code=404,
                detail="No user or group with that identifier",
            )
        principal_type, principal_id, display_name = resolved

        await grant_doc_to_principal(
            http, SUPABASE_URL, headers, document_id,
            principal_type, principal_id, granted_by=user.id,
        )
        # The grant call returns 0 on a re-grant (idempotent). For the
        # response we want the canonical share row regardless, so re-read
        # via list_doc_shares and project this principal's row.
        shares = await list_doc_shares(http, SUPABASE_URL, headers, document_id)
        match = next(
            (
                s for s in shares
                if s.principal_type == principal_type
                and s.principal_id == principal_id
            ),
            None,
        )
        granted_at = match.granted_at if match else ""
        return {
            "principal_id": principal_id,
            "principal_type": principal_type,
            "display_name": display_name,
            "granted_at": granted_at,
        }


@app.get("/api/documents/{document_id}/shares")
async def get_shares(
    document_id: str,
    user: AuthedUser = Depends(get_user),
) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as http:
        await _assert_doc_owner(http, user, document_id)
        shares = await list_doc_shares(
            http, SUPABASE_URL, _supabase_headers(user), document_id
        )
    return {"shares": [_share_to_dict(s) for s in shares]}


@app.delete(
    "/api/documents/{document_id}/share/{principal_type}/{principal_id}",
    status_code=204,
    response_class=Response,
)
async def delete_share(
    document_id: str,
    principal_type: str,
    principal_id: str,
    user: AuthedUser = Depends(get_user),
) -> Response:
    if principal_type not in ("user", "group"):
        raise HTTPException(
            status_code=400, detail="principal_type must be 'user' or 'group'"
        )
    async with httpx.AsyncClient(timeout=15.0) as http:
        await _assert_doc_owner(http, user, document_id)
        removed = await revoke_doc_from_principal(
            http, SUPABASE_URL, _supabase_headers(user), document_id,
            principal_type,  # type: ignore[arg-type]
            principal_id,
        )
        if removed == 0:
            raise HTTPException(
                status_code=404, detail="No shares for that principal"
            )
    return Response(status_code=204)


def _share_to_dict(s: ShareSummary) -> dict:
    return {
        "principal_type": s.principal_type,
        "principal_id": s.principal_id,
        "display_name": s.display_name,
        "granted_at": s.granted_at,
    }


# -----------------------------------------------------------------------------
# US-010: search_documents tool endpoint. US-011 will wire this through the
# Chat Completions tool-call loop; exposing it directly here makes the tool
# testable (and the PRD validation steps runnable) before that lands.
# -----------------------------------------------------------------------------


@app.post("/api/search")
async def search(
    req: SearchDocumentsInput,
    user: AuthedUser = Depends(get_user),
) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as http:
        results = await search_documents(
            openai_client=openai_client,
            http=http,
            supabase_url=SUPABASE_URL,
            supabase_headers=_supabase_headers(user),
            query=req.query,
            top_k=req.top_k,
            filters=req.filters,
        )
    return {
        "results": [r.model_dump() for r in results],
        "similarity_threshold": get_similarity_threshold(),
        "embedding_model": get_embedding_model(),
    }


# US-020: keyword (full-text) search counterpart to /api/search. Surfaces the
# Postgres tsvector ranking directly so the validation test in the PRD can
# compare vector vs. keyword behaviour for exact-match tokens. US-021 adds
# /api/search/hybrid below that fuses both.
@app.post("/api/search/keyword")
async def search_keyword(
    req: SearchDocumentsInput,
    user: AuthedUser = Depends(get_user),
) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as http:
        results = await keyword_search(
            http=http,
            supabase_url=SUPABASE_URL,
            supabase_headers=_supabase_headers(user),
            query=req.query,
            top_k=req.top_k,
            filters=req.filters,
        )
    return {"results": [r.model_dump() for r in results]}


# US-021: hybrid (vector + keyword via RRF). The chat tool dispatches through
# `hybrid_search` by default; this endpoint exposes the same path directly so
# the PRD validation step (compare hybrid top-5 vs vector-only vs keyword-only)
# is runnable without driving the agent.
@app.post("/api/search/hybrid")
async def search_hybrid(
    req: SearchDocumentsInput,
    user: AuthedUser = Depends(get_user),
) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as http:
        results = await hybrid_search(
            openai_client=openai_client,
            http=http,
            supabase_url=SUPABASE_URL,
            supabase_headers=_supabase_headers(user),
            query=req.query,
            top_k=req.top_k,
            filters=req.filters,
        )
    return {
        "results": [r.model_dump() for r in results],
        "rrf_k": get_rrf_k(),
        "embedding_model": get_embedding_model(),
    }


# US-022: full agent retrieval pipeline (search + rerank). Mirrors what the
# chat tool path runs. Useful for the PRD validation step that compares
# hybrid-only top-5 vs hybrid+rerank top-5 without driving the agent.
@app.post("/api/search/rerank")
async def search_rerank(
    req: SearchDocumentsInput,
    user: AuthedUser = Depends(get_user),
) -> dict:
    async with httpx.AsyncClient(timeout=60.0) as http:
        results, mode, reranker_name = await _retrieve_for_agent(
            http=http,
            user=user,
            query=req.query,
            top_k=req.top_k,
            filters=req.filters,
        )
    return {
        "results": [r.model_dump() for r in results],
        "retrieval_mode": mode,
        "reranker": reranker_name,
        "rerank_input_k": get_rerank_input_k(),
    }


# US-023: direct text-to-SQL endpoint mirroring what the chat tool dispatches.
# Useful for the PRD validation steps (revenue query, adversarial DROP, trace
# inspection) without driving the agent. Auth required so RLS-equivalent access
# control still applies — the read-only role under the hood doesn't grant
# per-user scoping, only schema scoping.
@app.post("/api/sql")
async def sql_query(
    req: QueryDatabaseInput,
    user: AuthedUser = Depends(get_user),
) -> dict:
    if not sql_tool_enabled():
        raise HTTPException(
            status_code=503,
            detail="text-to-SQL tool is not configured (set ANALYTICS_DATABASE_URL)",
        )
    try:
        result = await query_database(
            openai_client=openai_client,
            question=req.question,
            row_limit=req.row_limit,
            schema_snapshot=_SQL_SCHEMA_SNAPSHOT,
        )
    except SqlSafetyError as e:
        raise HTTPException(status_code=400, detail=f"unsafe sql: {e}") from e
    return result.model_dump()


# US-024: direct web search endpoint mirroring what the chat tool dispatches.
# Lets the PRD validation step ("ask about today's tech news on a fresh
# account") be exercised end-to-end without driving the chat loop, and gives
# us a clean way to smoke-test a new provider after rotating API keys.
@app.post("/api/web-search")
async def web_search_endpoint(
    req: WebSearchInput,
    user: AuthedUser = Depends(get_user),
) -> dict:
    if not web_search_tool_enabled():
        raise HTTPException(
            status_code=503,
            detail="web search tool is not configured (set WEB_SEARCH_PROVIDER)",
        )
    async with httpx.AsyncClient(timeout=get_web_search_timeout_s()) as http:
        results = await web_search(http=http, query=req.query, top_k=req.top_k)
    return {
        "results": [r.model_dump() for r in results],
        "count": len(results),
    }


# US-027: direct sub-agent endpoint mirroring what the chat tool dispatches.
# Useful for the PRD validation steps (compare main-agent vs sub-agent
# context size, inspect the activity log) without driving the chat loop.
@app.post("/api/subagent")
async def subagent_endpoint(
    req: SpawnDocumentAgentInput,
    user: AuthedUser = Depends(get_user),
) -> dict:
    async with httpx.AsyncClient(timeout=120.0) as http:
        try:
            result = await run_document_subagent(
                openai_client=openai_client,
                http=http,
                supabase_url=SUPABASE_URL,
                supabase_headers=_supabase_headers(user),
                document_id=req.document_id,
                task=req.task,
            )
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
    return result.model_dump()


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "model": OPENAI_MODEL, "file_search": bool(OPENAI_VECTOR_STORE_ID)}
