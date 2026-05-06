"""US-026 / US-027: full-document sub-agent.

When a user asks something that needs full-document context (summarize,
outline, key takeaways) the main agent invokes `spawn_document_agent` instead
of chunk-level `search_documents`. This module:

  1. Detects "full-document" intent on the user's message via a keyword
     heuristic — score in [0, 1], threshold tunable via
     `FULL_DOCUMENT_INTENT_THRESHOLD` (US-026). When the score clears the
     threshold, the main turn's system prompt is augmented with a hint
     telling the model to prefer `spawn_document_agent`.

  2. Runs the sub-agent in its own Chat Completions loop with an isolated
     message list — the only context it sees is the target document's
     filename + metadata + chunk count, NOT the parent chat history (US-027).
     Two tools are exposed: `read_document_chunk(chunk_index)` and
     `finalize(summary)`. The sub-agent walks chunks until it finalizes
     (or hits the iteration cap, in which case we force a final summary).

  3. Returns `{summary, activity, ...}` to the main agent. `activity` is a
     structured log of every read / reasoning step / finalize so the
     hierarchical tool-call tree in the UI (US-028) can render the
     sub-agent's work nested under the spawning tool call.

Failures are caught at the top level and serialised into the tool result so
the parent agent sees an error (matching the OpenAI tool-call pattern used
elsewhere in this codebase) rather than aborting the whole turn.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Literal

import httpx
from langsmith import traceable
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

log = logging.getLogger("agentic_rag.backend.subagent")

# Keyword heuristic for "this question needs the full document" intent.
# Conservative list: each token has a strong "operate on the whole document"
# connotation. Tokens are matched as whole words so "summary" matches but
# "summit" doesn't.
_FULL_DOC_KEYWORDS: tuple[str, ...] = (
    "summarize", "summarise", "summary", "summarized", "summarised",
    "outline", "outlined", "overview", "abstract", "thesis",
    "tldr", "tl;dr",
    "executive summary", "key points", "main points", "key takeaways",
    "takeaways", "high-level", "high level",
    "full document", "entire document", "whole document",
    "the full", "the entire", "the whole", "whole paper", "entire paper",
    "full paper", "the paper", "the document",
)

DEFAULT_INTENT_THRESHOLD = 0.5
DEFAULT_SUBAGENT_MAX_ITERATIONS = 12
DEFAULT_SUBAGENT_PREVIEW_CHARS = 240


def get_intent_threshold() -> float:
    """`FULL_DOCUMENT_INTENT_THRESHOLD` env, default 0.5.

    Score is 1.0 when any keyword matches the user message and 0.0 otherwise,
    so a 0.5 default means "any match triggers". Setting to 0.0 always
    triggers the system-prompt nudge (verbose); setting to 1.0 effectively
    disables the heuristic — the agent picks `spawn_document_agent` purely
    on tool-description signals.
    """
    raw = os.environ.get("FULL_DOCUMENT_INTENT_THRESHOLD")
    if raw is None or raw == "":
        return DEFAULT_INTENT_THRESHOLD
    try:
        v = float(raw)
    except ValueError as e:
        raise ValueError(
            f"FULL_DOCUMENT_INTENT_THRESHOLD must be a float in [0, 1], got {raw!r}"
        ) from e
    if not 0.0 <= v <= 1.0:
        raise ValueError(
            f"FULL_DOCUMENT_INTENT_THRESHOLD must be in [0, 1], got {v}"
        )
    return v


def get_subagent_max_iterations() -> int:
    """`SUBAGENT_MAX_ITERATIONS` env, default 12. Caps the sub-agent's
    tool-call loop so a misbehaving model can't spin against the OpenAI API
    indefinitely. The cap is conservative because each iteration is a full
    chat.completions round-trip; raise it if you're summarising very long
    documents and the sub-agent runs out of reads before finalising."""
    raw = os.environ.get("SUBAGENT_MAX_ITERATIONS")
    if raw is None or raw == "":
        return DEFAULT_SUBAGENT_MAX_ITERATIONS
    try:
        v = int(raw)
    except ValueError as e:
        raise ValueError(f"SUBAGENT_MAX_ITERATIONS must be an int, got {raw!r}") from e
    if v < 1:
        raise ValueError(f"SUBAGENT_MAX_ITERATIONS must be >= 1, got {v}")
    return v


def get_subagent_model() -> str:
    """Model used for the sub-agent. Falls through `OPENAI_SUBAGENT_MODEL`
    → `OPENAI_MODEL` → `gpt-4o-mini` so deployers can pin a cheaper /
    longer-context model just for sub-agent runs without affecting chat."""
    return (
        os.environ.get("OPENAI_SUBAGENT_MODEL")
        or os.environ.get("OPENAI_MODEL")
        or "gpt-4o-mini"
    )


def detect_full_document_intent(text: str) -> float:
    """Score in [0, 1] indicating "this question needs the full document".

    Returns 1.0 if any of `_FULL_DOC_KEYWORDS` matches as a whole-word /
    whole-phrase span in `text`, else 0.0. The binary shape keeps the
    threshold useful (any user-meaningful value is actionable) — a finer
    score would just be noise given the small keyword set.
    """
    if not text:
        return 0.0
    lowered = text.lower()
    for kw in _FULL_DOC_KEYWORDS:
        # Whole-phrase match for multi-word keywords; whole-word match for
        # single tokens. \b alone wouldn't anchor multi-word phrases.
        if " " in kw:
            if kw in lowered:
                return 1.0
        else:
            if re.search(rf"\b{re.escape(kw)}\b", lowered):
                return 1.0
    return 0.0


# -----------------------------------------------------------------------------
# Tool schema for the *parent* agent (registered alongside search_documents in
# main.py). The sub-agent itself uses a separate tools list — see below.
# -----------------------------------------------------------------------------


class SpawnDocumentAgentInput(BaseModel):
    document_id: str = Field(
        ...,
        description=(
            "ID of the user's document to delegate to the sub-agent. If you "
            "don't already have a document_id, first call `search_documents` "
            "with a query that identifies the right document, then take the "
            "`document_id` from a high-similarity result."
        ),
    )
    task: str = Field(
        ...,
        min_length=1,
        description=(
            "What the sub-agent should do with the document, in natural "
            "language. Examples: 'summarize the paper', 'outline the main "
            "argument', 'list the key takeaways'."
        ),
    )


SPAWN_DOCUMENT_AGENT_TOOL_DESCRIPTION = (
    "Delegate a full-document task to a sub-agent that reads the document "
    "chunk-by-chunk and returns a final answer. Use this for questions that "
    "need broad understanding of an entire document — summaries, outlines, "
    "overall arguments, executive summaries, key takeaways. Prefer "
    "`search_documents` for chunk-level questions where only a few passages "
    "are relevant. The sub-agent runs in an isolated context (it does NOT "
    "see the chat history) and returns `{summary, activity, document_id, "
    "filename}`. Cite the returned summary in your reply."
)


def spawn_document_agent_tool_schema() -> dict[str, Any]:
    """Chat Completions `tools[]` entry for the spawn_document_agent tool."""
    return {
        "type": "function",
        "function": {
            "name": "spawn_document_agent",
            "description": SPAWN_DOCUMENT_AGENT_TOOL_DESCRIPTION,
            "parameters": SpawnDocumentAgentInput.model_json_schema(),
        },
    }


SPAWN_DOCUMENT_AGENT_PROMPT_BLOCK = (
    "\n\nYou also have a `spawn_document_agent` tool that delegates a "
    "full-document task (summarize / outline / overview / key takeaways) to "
    "a sub-agent that reads the document chunk-by-chunk in an isolated "
    "context. Routing rules: prefer `search_documents` for chunk-level "
    "questions (specific facts, named entities, single-passage lookups). "
    "Use `spawn_document_agent` when the user's question implies the full "
    "document — keywords like 'summarize', 'outline', 'overall', 'entire "
    "paper'. If you don't yet have a `document_id`, first call "
    "`search_documents` with a query identifying the right document, then "
    "spawn the sub-agent with that id."
)


# -----------------------------------------------------------------------------
# Sub-agent runtime.
# -----------------------------------------------------------------------------


class SubAgentActivityEntry(BaseModel):
    """One step in the sub-agent's activity log.

    `kind` discriminates the entry shape:
      * 'read'     — sub-agent called `read_document_chunk`. `chunk_index` +
                     short content `preview` populated.
      * 'reason'   — sub-agent emitted free-form text between tool calls.
                     `text` populated.
      * 'finalize' — sub-agent called `finalize(summary)`. `summary`
                     populated. Always last when present.
      * 'error'    — sub-agent failed (read out-of-range index, exceeded
                     iteration cap, etc.). `text` populated.
    """

    kind: Literal["read", "reason", "finalize", "error"]
    chunk_index: int | None = None
    preview: str | None = None
    text: str | None = None
    summary: str | None = None


class SpawnDocumentAgentResult(BaseModel):
    document_id: str
    filename: str
    summary: str
    activity: list[SubAgentActivityEntry]
    iterations: int
    chunks_total: int
    truncated: bool = False


# Sub-agent's own tool list. Note: these are NOT the same as the parent
# agent's tools — they live entirely inside the sub-agent's chat loop.
def _read_chunk_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "read_document_chunk",
            "description": (
                "Read one chunk of the document by zero-based index. Returns "
                "the chunk's full text. Call this repeatedly to walk through "
                "the document; you may skip ahead, re-read, or read in any "
                "order. Out-of-range indices return an error."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "chunk_index": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Zero-based chunk index in [0, chunks_total).",
                    }
                },
                "required": ["chunk_index"],
            },
        },
    }


def _finalize_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "finalize",
            "description": (
                "Emit the sub-agent's final answer for the parent task. Once "
                "you call this, the sub-agent loop ends and the summary is "
                "returned to the main agent. The summary should be a "
                "self-contained answer that the main agent can cite without "
                "re-reading the document."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "summary": {
                        "type": "string",
                        "minLength": 1,
                        "description": "The final answer to the parent task.",
                    }
                },
                "required": ["summary"],
            },
        },
    }


def _build_subagent_system_prompt(
    *, filename: str, chunks_total: int, metadata: dict | None, task: str,
) -> str:
    metadata_line = ""
    if metadata:
        title = metadata.get("title")
        topics = metadata.get("topics") or []
        doc_type = metadata.get("document_type")
        bits: list[str] = []
        if title:
            bits.append(f"title={title!r}")
        if doc_type:
            bits.append(f"type={doc_type}")
        if topics:
            bits.append(f"topics={topics}")
        if bits:
            metadata_line = "\nKnown metadata: " + ", ".join(bits) + "."
    return (
        f"You are a sub-agent running in isolation. Your job is one task only:\n"
        f"  TASK: {task}\n\n"
        f"You are working on a single document:\n"
        f"  filename: {filename}\n"
        f"  chunks_total: {chunks_total} (indices 0..{chunks_total - 1})"
        f"{metadata_line}\n\n"
        "Tools:\n"
        "  * read_document_chunk(chunk_index) — read one chunk's full text.\n"
        "  * finalize(summary) — emit the final answer and end the loop.\n\n"
        "Workflow:\n"
        "  1. Read chunks in order (or sample widely if the document is long).\n"
        "  2. Keep your reasoning between tool calls brief — you have a "
        "limited iteration budget.\n"
        "  3. When you have enough material, call `finalize` with a "
        "self-contained answer to the TASK.\n\n"
        "Important: you do NOT have access to the user's chat history, "
        "other documents, or any external tools. Stay scoped to this one "
        "document. Always finish with `finalize`."
    )


async def _fetch_document(
    *, http: httpx.AsyncClient, supabase_url: str, supabase_headers: dict,
    document_id: str,
) -> dict:
    """Look up `document_id` in `public.documents` under the user's JWT.

    RLS scopes the result to the requesting user's documents — a foreign
    document_id returns no rows, which we surface as a 'not found' error
    (indistinguishable from the actual missing case, which is the right
    behaviour). Soft-deleted rows are excluded.
    """
    r = await http.get(
        f"{supabase_url}/rest/v1/documents",
        params={
            "id": f"eq.{document_id}",
            "deleted_at": "is.null",
            "select": "id,filename,chunks_count,metadata",
        },
        headers=supabase_headers,
    )
    r.raise_for_status()
    rows = r.json()
    if not rows:
        raise ValueError(
            f"document {document_id} not found (or not owned by you)"
        )
    return rows[0]


async def _fetch_chunks_by_index(
    *, http: httpx.AsyncClient, supabase_url: str, supabase_headers: dict,
    document_id: str,
) -> list[str]:
    """Return chunk content ordered by chunk_index, indexed positionally.

    We pull every chunk for the document up front so `read_document_chunk`
    is an in-memory dict lookup rather than another DB round-trip per call.
    Memory is bounded by chunk size (~500 tokens) × chunks_total — typically
    well under 1MB even for long documents.
    """
    r = await http.get(
        f"{supabase_url}/rest/v1/chunks",
        params={
            "document_id": f"eq.{document_id}",
            "select": "chunk_index,content",
            "order": "chunk_index.asc",
        },
        headers=supabase_headers,
    )
    r.raise_for_status()
    rows = r.json()
    # Index → content. The chunker writes contiguous indices starting at 0;
    # we don't rely on that here — just return them in chunk_index order.
    out: list[str] = []
    seen: set[int] = set()
    for row in rows:
        idx = int(row["chunk_index"])
        if idx in seen:
            continue
        seen.add(idx)
        out.append(row["content"] or "")
    return out


def _truncate_for_preview(text: str, n: int = DEFAULT_SUBAGENT_PREVIEW_CHARS) -> str:
    if len(text) <= n:
        return text
    return text[: n - 1].rstrip() + "…"


@traceable(run_type="chain", name="subagent_run")
async def run_document_subagent(
    *,
    openai_client: AsyncOpenAI,
    http: httpx.AsyncClient,
    supabase_url: str,
    supabase_headers: dict,
    document_id: str,
    task: str,
) -> SpawnDocumentAgentResult:
    """Spawn a sub-agent scoped to one document and run it to completion.

    The sub-agent's chat-completions loop is fully isolated — its message
    list is just `[system, user]` to start, plus its own assistant +
    tool turns. The parent's chat history never enters this scope (US-027).

    Returns a `SpawnDocumentAgentResult`. Raises on infrastructure failures
    (Supabase fetch errors, OpenAI API errors); the caller in main.py
    serialises those into a tool-error payload so the main agent can recover.
    """
    doc = await _fetch_document(
        http=http,
        supabase_url=supabase_url,
        supabase_headers=supabase_headers,
        document_id=document_id,
    )
    chunks = await _fetch_chunks_by_index(
        http=http,
        supabase_url=supabase_url,
        supabase_headers=supabase_headers,
        document_id=document_id,
    )
    chunks_total = len(chunks)
    if chunks_total == 0:
        return SpawnDocumentAgentResult(
            document_id=document_id,
            filename=doc.get("filename") or "",
            summary=(
                "Document has no readable chunks (it may still be processing "
                "or have failed ingestion). No summary produced."
            ),
            activity=[
                SubAgentActivityEntry(
                    kind="error",
                    text="document has no chunks",
                )
            ],
            iterations=0,
            chunks_total=0,
        )

    system_prompt = _build_subagent_system_prompt(
        filename=doc.get("filename") or "",
        chunks_total=chunks_total,
        metadata=doc.get("metadata"),
        task=task,
    )
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]
    tools = [_read_chunk_tool_schema(), _finalize_tool_schema()]
    activity: list[SubAgentActivityEntry] = []
    summary: str | None = None
    truncated = False
    max_iter = get_subagent_max_iterations()
    model = get_subagent_model()

    for iteration in range(max_iter):
        try:
            resp = await openai_client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools,
                # Lean toward calling a tool — the workflow is read → read →
                # finalize, so we want the model to pick a tool every turn
                # rather than padding with prose.
                tool_choice="auto",
            )
        except Exception as e:  # noqa: BLE001 — re-raise after activity log
            activity.append(
                SubAgentActivityEntry(kind="error", text=f"openai error: {e}")
            )
            raise

        choice = resp.choices[0]
        msg = choice.message
        # Capture any free-form reasoning the model emits between tool calls
        # so the UI can render it in the hierarchical tree (US-028).
        msg_content = (msg.content or "").strip()
        if msg_content:
            activity.append(SubAgentActivityEntry(kind="reason", text=msg_content))

        if not msg.tool_calls:
            # No tool call and a final message — treat as the finalize fallback
            # if the model produced text instead of calling the tool.
            if msg_content:
                summary = msg_content
                activity.append(
                    SubAgentActivityEntry(kind="finalize", summary=msg_content)
                )
            else:
                activity.append(
                    SubAgentActivityEntry(
                        kind="error",
                        text="sub-agent emitted no content and no tool call",
                    )
                )
            break

        # Append the assistant turn (with tool_calls) so the next loop
        # iteration can include matching tool messages.
        messages.append(
            {
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            }
        )

        finalized = False
        for tc in msg.tool_calls:
            tool_name = tc.function.name
            try:
                tool_args = (
                    json.loads(tc.function.arguments)
                    if tc.function.arguments
                    else {}
                )
            except json.JSONDecodeError as e:
                tool_result = json.dumps(
                    {"error": f"invalid tool arguments json: {e}"}
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": tool_result,
                    }
                )
                activity.append(
                    SubAgentActivityEntry(
                        kind="error",
                        text=f"{tool_name}: invalid arguments json",
                    )
                )
                continue

            if tool_name == "read_document_chunk":
                idx_raw = tool_args.get("chunk_index")
                try:
                    idx = int(idx_raw)
                except (TypeError, ValueError):
                    err = f"chunk_index must be an int, got {idx_raw!r}"
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps({"error": err}),
                        }
                    )
                    activity.append(
                        SubAgentActivityEntry(kind="error", text=err)
                    )
                    continue
                if idx < 0 or idx >= chunks_total:
                    err = (
                        f"chunk_index {idx} out of range [0, {chunks_total})"
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps({"error": err}),
                        }
                    )
                    activity.append(
                        SubAgentActivityEntry(kind="error", text=err)
                    )
                    continue
                content = chunks[idx]
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(
                            {"chunk_index": idx, "content": content}
                        ),
                    }
                )
                activity.append(
                    SubAgentActivityEntry(
                        kind="read",
                        chunk_index=idx,
                        preview=_truncate_for_preview(content),
                    )
                )
                continue

            if tool_name == "finalize":
                summary_arg = tool_args.get("summary")
                if not isinstance(summary_arg, str) or not summary_arg.strip():
                    err = "finalize.summary must be a non-empty string"
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps({"error": err}),
                        }
                    )
                    activity.append(
                        SubAgentActivityEntry(kind="error", text=err)
                    )
                    continue
                summary = summary_arg.strip()
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps({"ok": True}),
                    }
                )
                activity.append(
                    SubAgentActivityEntry(kind="finalize", summary=summary)
                )
                finalized = True
                continue

            # Unknown tool — should never happen since we control the schema.
            err = f"unknown sub-agent tool: {tool_name}"
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps({"error": err}),
                }
            )
            activity.append(SubAgentActivityEntry(kind="error", text=err))

        if finalized:
            break
    else:
        # Loop exhausted without a finalize call. Force a salvage summary
        # built from whatever chunks were read so the parent agent sees
        # something useful instead of a hard error.
        truncated = True
        read_indices = [
            e.chunk_index for e in activity if e.kind == "read" and e.chunk_index is not None
        ]
        if read_indices:
            forced = (
                f"Sub-agent exhausted its iteration budget ({max_iter}) "
                f"after reading chunks {sorted(set(read_indices))} of "
                f"{chunks_total}. Partial findings:\n\n"
                + "\n\n".join(
                    f"[chunk {e.chunk_index}] {e.preview or ''}"
                    for e in activity
                    if e.kind == "read"
                )
            )
        else:
            forced = (
                f"Sub-agent exhausted its iteration budget ({max_iter}) "
                "without reading any chunks."
            )
        summary = forced
        activity.append(
            SubAgentActivityEntry(
                kind="error",
                text=f"iteration cap ({max_iter}) reached; salvage summary used",
            )
        )

    if summary is None:
        # Edge case: the no-tool-no-content branch above broke out without
        # producing a summary. Fail noisily so the parent surfaces the issue.
        summary = "Sub-agent produced no summary."

    return SpawnDocumentAgentResult(
        document_id=document_id,
        filename=doc.get("filename") or "",
        summary=summary,
        activity=activity,
        iterations=iteration + 1,
        chunks_total=chunks_total,
        truncated=truncated,
    )
