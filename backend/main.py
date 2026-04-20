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

import json
import logging
import os
from typing import AsyncIterator, Literal

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langsmith import traceable
from langsmith.run_helpers import get_current_run_tree
from langsmith.wrappers import wrap_openai
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from chunking import chunk_text, get_chunk_config
from embeddings import embed_texts, get_embedding_model, to_pgvector
from retrieval import (
    SearchDocumentsInput,
    get_similarity_threshold,
    search_documents,
    search_documents_tool_schema,
)

load_dotenv()

log = logging.getLogger("agentic_rag.backend")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_ANON_KEY = os.environ["SUPABASE_ANON_KEY"]
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
COMPLETIONS_SYSTEM_PROMPT = (
    "You are a helpful assistant with access to the user's ingested documents "
    "via the `search_documents` tool. Prefer calling the tool to ground your "
    "answer whenever the question might be answerable from the user's own "
    "documents. When you cite tool results, mention the document filename. If "
    "no relevant chunks are returned, answer from general knowledge and say so."
)

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
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)


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
            results = await search_documents(
                openai_client=openai_client,
                http=http,
                supabase_url=SUPABASE_URL,
                supabase_headers=_supabase_headers(user),
                query=validated.query,
                top_k=validated.top_k,
            )
            return json.dumps(
                {
                    "results": [r.model_dump() for r in results],
                    "count": len(results),
                    "similarity_threshold": get_similarity_threshold(),
                }
            )
        except Exception as e:  # noqa: BLE001 — surface to model as tool error
            log.exception("search_documents tool failed")
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
        messages: list[dict] = [{"role": "system", "content": COMPLETIONS_SYSTEM_PROMPT}]
        messages.extend(_prior_to_completions(windowed))
        messages.append({"role": "user", "content": message})

        tools = [search_documents_tool_schema()]
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
    }


# -----------------------------------------------------------------------------
# US-008 + US-009: ingestion pipeline. US-007 handles upload + Storage blob;
# this endpoint picks up the uploaded document, reads the blob, chunks it,
# embeds each chunk (OpenAI batched + retried), and persists chunk rows with
# their pgvector embeddings. Retrieval tools land in US-010+.
# -----------------------------------------------------------------------------

DOCUMENT_COLUMNS = (
    "id,user_id,filename,storage_path,byte_size,content_type,status,"
    "error_message,chunks_count,uploaded_at,deleted_at"
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


async def _insert_chunks(
    http: httpx.AsyncClient,
    user: AuthedUser,
    document_id: str,
    chunks: list[str],
    embeddings: list[list[float]] | None = None,
) -> int:
    if not chunks:
        return 0
    if embeddings is not None and len(embeddings) != len(chunks):
        raise ValueError(
            f"embeddings/chunks length mismatch: {len(embeddings)} vs {len(chunks)}"
        )
    BATCH = 200
    inserted = 0
    for i in range(0, len(chunks), BATCH):
        batch = chunks[i : i + BATCH]
        batch_embeddings = (
            embeddings[i : i + BATCH] if embeddings is not None else [None] * len(batch)
        )
        rows: list[dict] = []
        for j, (text, emb) in enumerate(zip(batch, batch_embeddings)):
            row: dict = {
                "document_id": document_id,
                "user_id": user.id,
                "chunk_index": i + j,
                "content": text,
            }
            if emb is not None:
                row["embedding"] = to_pgvector(emb)
            rows.append(row)
        r = await http.post(
            f"{SUPABASE_URL}/rest/v1/chunks",
            headers={**_supabase_headers(user), "Prefer": "return=minimal"},
            json=rows,
        )
        r.raise_for_status()
        inserted += len(rows)
    return inserted


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
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as e:
                raise ValueError(f"file is not valid utf-8: {e}") from e

            chunks = chunk_text(text)
            # Embed first so a mid-pipeline failure doesn't leave half-written
            # chunk rows behind — _delete_chunks + _insert_chunks are the
            # atomic-ish boundary (PostgREST doesn't give us a real tx, but
            # re-running ingest is idempotent via _delete_chunks).
            embeddings = await embed_texts(openai_client, chunks)
            await _delete_chunks(http, user, document_id)
            chunk_count = await _insert_chunks(
                http, user, document_id, chunks, embeddings
            )
            updated = await _patch_document(
                http,
                user,
                document_id,
                status="ready",
                chunks_count=chunk_count,
                error_message=None,
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
        "chunk_size_tokens": size,
        "chunk_overlap_tokens": overlap,
        "embedding_model": get_embedding_model(),
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
        )
    return {
        "results": [r.model_dump() for r in results],
        "similarity_threshold": get_similarity_threshold(),
        "embedding_model": get_embedding_model(),
    }


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "model": OPENAI_MODEL, "file_search": bool(OPENAI_VECTOR_STORE_ID)}
