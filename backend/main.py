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
"""

from __future__ import annotations

import json
import logging
import os
from typing import AsyncIterator

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
    content: str,
) -> dict:
    r = await http.post(
        f"{SUPABASE_URL}/rest/v1/messages",
        headers=_supabase_headers(user),
        json={"thread_id": thread_id, "role": role, "content": content},
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


@traceable(run_type="chain", name="chat_turn")
async def _stream_reply(
    user: AuthedUser,
    thread_id: str,
    message: str,
) -> AsyncIterator[bytes]:
    _attach_run_metadata(user_id=user.id, thread_id=thread_id)

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


@app.post("/api/chat")
async def chat(
    req: ChatRequest,
    request: Request,
    user: AuthedUser = Depends(get_user),
):
    async def gen() -> AsyncIterator[bytes]:
        async for chunk in _stream_reply(user, req.thread_id, req.message):
            if await request.is_disconnected():
                return
            yield chunk

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "model": OPENAI_MODEL, "file_search": bool(OPENAI_VECTOR_STORE_ID)}
