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

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

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
from starlette.types import ASGIApp, Receive, Scope, Send

from chunking import chunk_text, get_chunk_config
from conversation_tokens import (
    CONVERSATION_TOKEN_TTL_SECONDS,
    generate_conversation_token,
    hash_conversation_token,
)
from widget_keys import (
    WILDCARD_ORIGIN,
    generate_public_key,
    has_registered_origin,
    is_origin_allowed,
    is_widget_public_key,
)
from embeddings import (
    EmbeddingStamp,
    check_embedder_drift,
    embed_texts,
    get_embedding_model,
    probe_embed_dim,
    to_pgvector,
)
from metadata import extract_document_metadata, get_metadata_model
from model_config import (
    ChatMode,
    ProviderConfig,
    build_openai_client,
    resolve_chat_mode_default,
    responses_capable,
)
from parsing import UnsupportedFormatError, get_selected_parser, warmup as warmup_parsing
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
    ListDocumentsInput,
    SearchDocumentsInput,
    SearchDocumentsResult,
    get_retrieval_mode,
    get_rrf_k,
    get_similarity_threshold,
    hybrid_search,
    keyword_only_search,
    keyword_search,
    list_documents,
    list_documents_tool_schema,
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

# US-024: the model-provider key (OPENAI_API_KEY / AZURE_OPENAI_API_KEY / a
# role-specific *_API_KEY) is NOT required here — which key is needed depends on
# the resolved provider per role, and ProviderConfig.from_env validates that
# fail-closed when the answerer/embedder/judge clients are built below. Requiring
# OPENAI_API_KEY unconditionally would crash an all-Azure deployment (which sets
# no OPENAI_API_KEY) before that per-provider check ever runs.
_REQUIRED_ENV = ("SUPABASE_URL", "SUPABASE_ANON_KEY")
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
# US-069 (ADR-0008): also the key `backend.support_bot.provision_workspace_bot`
# uses to create the per-workspace support bot's auth.users row via the GoTrue
# admin API (required only when support is enabled — that helper resolves it
# fail-closed at call time). It bypasses RLS — keep it server-side, never
# client-side; this module never logs or returns it.
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or None
# US-068 (ADR-0008): the project JWT secret GoTrue signs with. NEW signing
# surface — before this the backend held only the anon key (public, non-signing)
# and forwarded user tokens it never minted. Optional: required only when the
# support bot is enabled (it is the secret `backend.supabase_jwt.mint_supabase_jwt`
# self-signs the ~60s bot token with, US-070), so a knowledge-assistant-only
# deployment may leave it unset. P5 threat-model: whoever holds this can forge
# any identity — keep it server-side only, never embed it client-side. The
# minting helper reads the env itself (fail-closed) so this is documentation +
# forward-discovery, not a hard requirement gate.
SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET") or None
# US-021: the OpenAI/Azure connection (api key, base_url, Azure params) is now
# resolved once via model_config.ProviderConfig.from_env (see the client build
# below), not read ad hoc here. OPENAI_MODEL stays — model selection is
# per-call-site (ADR-0006). A missing key for the resolved provider is caught
# fail-closed by ProviderConfig.from_env at the client build below (per-provider,
# not the unconditional _REQUIRED_ENV check above).
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_VECTOR_STORE_ID = os.environ.get("OPENAI_VECTOR_STORE_ID") or None
FRONTEND_ORIGINS = [
    o.strip()
    for o in os.environ.get("FRONTEND_ORIGIN", "http://localhost:5173").split(",")
    if o.strip()
]

# US-074: the public widget surface (`/widget/*`) runs its OWN CORS posture keyed
# off the per-key `widget_keys.allowed_origins` — never FRONTEND_ORIGINS (which is
# the authenticated `/api/*` app's trust). The registered-origin set is unioned
# from the DB and cached in-process for this many seconds so a preflight / simple
# request does not pay a service-role round-trip per call (CORS runs AHEAD of the
# US-076 rate limit, so a per-request DB hit would be a cheap DoS lever). Issuing
# / revoking a key invalidates the cache immediately on THIS instance; the TTL is
# the cross-instance backstop (another backend issued a key). Default 30s.
try:
    WIDGET_CORS_ORIGIN_CACHE_TTL = float(
        os.environ.get("WIDGET_CORS_ORIGIN_CACHE_TTL", "30")
    )
except ValueError as e:
    raise ValueError("WIDGET_CORS_ORIGIN_CACHE_TTL must be a number") from e
if WIDGET_CORS_ORIGIN_CACHE_TTL <= 0:
    # 0 makes `_fresh()` always False (elapsed < 0 is never true), so every
    # /widget/* request would trigger a serialized service-role DB read — exactly
    # the per-request-DB-hit DoS lever the cache exists to prevent. Require > 0.
    raise ValueError("WIDGET_CORS_ORIGIN_CACHE_TTL must be > 0")

# US-025: `ChatMode` and the resolved `DEFAULT_CHAT_MODE` moved below — the
# default now depends on the answerer provider (responses is OpenAI-only), so it
# is resolved against `_ANSWERER_CONFIG` after that config is built. See
# `resolve_chat_mode_default` (model_config.py) for the fail-closed binding.

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
    "When the user names a specific file by filename (e.g. '090725.txt', "
    "'foo.pdf') OR asks a content-light question about a named document "
    "(summarize / outline / tldr of <filename>), call `list_documents` first "
    "to resolve the filename to a `document_id`. `search_documents` matches "
    "chunk content only, so filenames alone will not retrieve anything.\n\n"
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

# US-021/US-022: per-role provider binding. Each role's ProviderConfig is
# resolved once at startup (openai|azure). The answerer is the primary client;
# the embedder embeds queries/chunks and the runtime-judge backs the ADR-0003
# faithfulness gate (consumed in area D — built here so its provider binding is
# validated at startup, fail-closed). wrap_openai keeps LangSmith tracing intact
# for every provider. Rerankers (COHERE/VOYAGE in reranking.py) stay a SEPARATE
# provider axis and are deliberately not part of this surface.
#
# US-023: the answerer client is the single chat host for ALL text generation.
# The five auxiliary helpers — metadata (extract_document_metadata), planner
# (plan_query), SQL-gen (query_database), subagent (run_document_subagent), and
# the `llm` reranker (build_reranker) — are each passed THIS `openai_client`,
# never their own. Model selection is per call-site (METADATA_MODEL /
# OPENAI_PLANNER_MODEL / OPENAI_SQL_MODEL / OPENAI_SUBAGENT_MODEL /
# OPENAI_RERANK_MODEL, each → OPENAI_MODEL); provider/base_url is never split
# per helper (ADR-0006). Grep for `# US-023: answerer-role` at the call sites.
_ANSWERER_CONFIG = ProviderConfig.from_env("answerer")
_EMBEDDER_CONFIG = ProviderConfig.from_env("embedder")
_JUDGE_CONFIG = ProviderConfig.from_env("judge")
openai_client = wrap_openai(build_openai_client(_ANSWERER_CONFIG))

# US-025 (FR-M4): resolve + validate the process-wide default chat mode against
# the now-resolved answerer config. Responses mode (hosted file_search +
# server-side previous_response_id threading) runs on OpenAI proper only
# (provider=openai with no base_url override) and is non-portable, so the
# portable `completions` path is the cross-provider default, and an explicit
# CHAT_MODE_DEFAULT=responses under a non-responses-capable answerer (Azure, or
# an openai base_url-overridden host) fails closed HERE (at startup), never
# silently downgraded. For an OpenAI-proper answerer the historical `responses`
# default is preserved.
DEFAULT_CHAT_MODE: ChatMode = resolve_chat_mode_default(
    _ANSWERER_CONFIG, os.environ.get("CHAT_MODE_DEFAULT")
)
# US-025: the hosted-file_search + server-side-threading Responses path is
# reachable ONLY on OpenAI proper (provider=openai with no base_url override).
# Gates the per-request `mode` override at /api/chat so an explicit
# mode=responses can't sneak onto a non-openai provider OR an OpenAI-compatible
# base_url host (where the Responses endpoint doesn't exist).
RESPONSES_MODE_AVAILABLE = responses_capable(_ANSWERER_CONFIG)


def _build_role_client(cfg: ProviderConfig) -> AsyncOpenAI:
    """Reuse the answerer client when a role's resolved config is identical (the
    common single-provider case → no redundant connection pool); otherwise build
    a dedicated client for the split provider."""
    if cfg == _ANSWERER_CONFIG:
        return openai_client
    return wrap_openai(build_openai_client(cfg))


embedder_client = _build_role_client(_EMBEDDER_CONFIG)
judge_client = _build_role_client(_JUDGE_CONFIG)


# ---------------------------------------------------------------------------
# US-074: TWO CORS postures on one app, partitioned by path (ADR-0008).
#
# The authenticated app (`/api/*`, `/healthz`, everything that is NOT a public
# widget route) keeps its existing posture: a static allowlist = FRONTEND_ORIGINS.
# The PUBLIC widget surface (`/widget/*`) gets its OWN posture: it trusts ONLY the
# origins registered on an ACTIVE (not-revoked) widget key — the union of every
# `widget_keys.allowed_origins` — NEVER FRONTEND_ORIGINS. So the public surface
# never inherits the authenticated app's origin trust, and `/api/*` never trusts a
# widget origin: the two origin sets are independent (an app origin is rejected at
# `/widget/*`; a widget origin is rejected at `/api/*`).
#
# CORS is a BROWSER-side control and DEFENSE-IN-DEPTH here, NOT the hard boundary.
# A preflight (OPTIONS) carries no body, so the specific `public_key` is unknown at
# preflight time — this layer can only answer "is this Origin registered for SOME
# active key". The authoritative PER-KEY origin gate (US-073) + not-revoked gate
# (US-072) + rate limit (US-076) all run inside the endpoints under the real
# `public_key`. Letting the browser read a `/widget/*` response from a registered
# origin grants no data access by itself.
# ---------------------------------------------------------------------------

# The raw opaque customer token (US-071) travels in this header, deliberately
# distinct from `Authorization: Bearer <supabase-jwt>` so the customer leg can
# never be mistaken for a Supabase-authenticated principal. Defined here (ahead of
# the US-071 endpoints) because the widget CORS allow-list below must name it as an
# allowed request header for the cross-origin preflight to pass.
_CONVERSATION_TOKEN_HEADER = "X-Conversation-Token"

_WIDGET_PATH_PREFIX = "/widget/"

# A few routes live under `/widget/*` but are AUTHENTICATED, not part of the
# anonymous-customer surface: a workspace agent calls them under their real Supabase
# JWT from the operator dashboard (an APP origin in FRONTEND_ORIGINS), not from a
# buyer's iframe. They therefore belong to the authenticated CORS posture, NOT the
# per-key-origin widget posture — routing them to the widget allowlist would block
# the dashboard's own app origin. US-074's public surface is exactly the routes its
# AC enumerates (key resolution, conversation create/message, transcript, customer
# SSE); the agent-reply endpoint (US-082) is the known authenticated exception, named
# here so it lands on the right posture the moment it ships rather than being silently
# CORS-blocked. Matched by suffix because the conversation id is in the path.
_WIDGET_AUTHENTICATED_SUFFIXES = ("/agent-reply",)


def _is_widget_public_path(path: str) -> bool:
    """Single source of truth for the PUBLIC-widget path partition, so the two CORS
    layers (anonymous-customer vs authenticated) are guaranteed COMPLEMENTARY — every
    request is owned by exactly one posture, never both (no double
    `Access-Control-Allow-Origin`) and never neither. A `/widget/*` path that is an
    authenticated exception (above) is NOT public and falls to the authenticated
    posture."""
    if not path.startswith(_WIDGET_PATH_PREFIX):
        return False
    return not any(path.endswith(suffix) for suffix in _WIDGET_AUTHENTICATED_SUFFIXES)


async def _load_active_widget_origins() -> tuple[frozenset[str], bool]:
    """Service-role read of every ACTIVE (not-revoked) widget key's `allowed_origins`,
    unioned for the widget CORS layer. Returns `(origins, has_wildcard)` where
    `has_wildcard` is True iff some active key carries the dev-only `"*"` entry.

    Fail-closed by construction: when support is unconfigured (no service-role key)
    or the read errors, it returns `(empty, False)` so the public CORS surface
    DENIES every origin rather than ever fail-opening to an unregistered one. Blank
    entries are dropped (they can match no real browser `Origin`); `"*"` is hoisted
    into the wildcard flag (it is NEVER stored as a matchable origin)."""
    headers = _service_role_headers()
    if headers is None:
        # SUPABASE_SERVICE_ROLE_KEY unset → support widget not configured → there is
        # no widget surface to admit any origin for. Deny all (fail closed).
        return frozenset(), False
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            r = await http.get(
                f"{SUPABASE_URL}/rest/v1/widget_keys",
                params={"revoked_at": "is.null", "select": "allowed_origins"},
                headers=headers,
            )
            r.raise_for_status()
            rows = r.json()
    except Exception as e:
        # A transient upstream failure must FAIL CLOSED (deny), never fail open. Log
        # concisely (no full traceback): this runs on the refresh path at most once
        # per cache TTL, so a multi-frame stack per outage tick would just be spam.
        log.warning(
            "widget_cors.origin_load_failed: %s — denying all widget origins until "
            "the next refresh (fail-closed)",
            e,
        )
        return frozenset(), False
    origins: set[str] = set()
    wildcard = False
    for row in rows:
        for o in row.get("allowed_origins") or []:
            if o == WILDCARD_ORIGIN:
                # Dev-only opt-in (NON-PRODUCTION): a single active key with "*"
                # loosens the widget CORS layer to admit any present origin. The
                # authoritative per-key US-073 gate still rejects a wrong origin for
                # any OTHER key at the endpoint, so this only widens the browser-side
                # CORS check, never data access. Production keys never set "*".
                wildcard = True
            elif o and o.strip():
                origins.add(o)
    return frozenset(origins), wildcard


class _WidgetOriginSnapshot:
    """In-memory, TTL-cached snapshot of the union of `allowed_origins` across all
    active widget keys (+ whether any carries the dev-only `"*"` wildcard).

    Backs the widget CORS origin check so a preflight / simple request never pays a
    service-role round-trip per call. `allows()` is SYNC (Starlette's
    `is_allowed_origin` is sync); `ensure_fresh()` is the ASYNC refresh the
    middleware awaits BEFORE the sync read, TTL-gated so it hits the DB at most once
    per `WIDGET_CORS_ORIGIN_CACHE_TTL`. Issuance / revocation call `invalidate()` so
    a newly registered origin works (and a revoked one stops) immediately on this
    instance, with the TTL as the cross-instance backstop."""

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = ttl_seconds
        self._origins: frozenset[str] = frozenset()
        self._wildcard = False
        self._loaded_at: float | None = None
        # Bumped by every invalidate(); a refresh stamps its result fresh only if the
        # generation is unchanged across its `await`, so an invalidate that fires
        # during an in-flight load forces a reload instead of caching stale data.
        self._generation = 0
        # Created lazily inside a running loop — constructing an asyncio.Lock at
        # import time (no loop yet, py3.9) risks binding to the wrong loop.
        self._lock: asyncio.Lock | None = None

    def _fresh(self) -> bool:
        return (
            self._loaded_at is not None
            and (time.monotonic() - self._loaded_at) < self._ttl
        )

    def allows(self, origin: str | None) -> bool:
        """Mirror `widget_keys.is_origin_allowed`'s fail-closed rules at the CORS
        layer: a missing/blank Origin is refused even under the wildcard; otherwise
        the dev-only `"*"` admits any present origin, else exact (un-normalized)
        membership — a trailing slash / case / port mismatch fails CLOSED."""
        if not origin or not origin.strip():
            return False
        if self._wildcard:
            return True
        return origin in self._origins

    def invalidate(self) -> None:
        """Force the next `ensure_fresh()` to reload (after a key issue/revoke).

        Race-safe against an in-flight refresh: bumping `_generation` makes a load
        already suspended in `_load_active_widget_origins()` (which may have read the
        pre-mutation DB state) discard its result instead of stamping a stale
        snapshot fresh, so the next `ensure_fresh()` reloads the post-invalidation
        state rather than serving stale data for the full TTL."""
        self._generation += 1
        self._loaded_at = None

    async def ensure_fresh(self) -> None:
        if self._fresh():
            return
        if self._lock is None:
            # Benign cold-start race: two concurrent first calls may each create a
            # lock and both load. The load is idempotent, so the only cost is one
            # redundant read; thereafter the lock is stable and serializes refreshes.
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._fresh():
                return
            # Capture the generation BEFORE the await; if invalidate() fires during
            # the load (capturing pre-mutation DB state), commit nothing and leave
            # `_loaded_at` None so the next call reloads the post-invalidation state.
            gen = self._generation
            origins, wildcard = await _load_active_widget_origins()
            if self._generation == gen:
                self._origins, self._wildcard = origins, wildcard
                self._loaded_at = time.monotonic()


_WIDGET_ORIGIN_SNAPSHOT = _WidgetOriginSnapshot(WIDGET_CORS_ORIGIN_CACHE_TTL)


class _ScopedCORSMiddleware(CORSMiddleware):
    """A `CORSMiddleware` that acts ONLY on the requests it owns (by path) and passes
    everything else straight through to the next app. Two instances with
    complementary predicates run two independent CORS postures on one app (US-074)
    without ever both touching the same request — so exactly one posture owns each
    path and there is no double `Access-Control-Allow-Origin`."""

    def __init__(self, app: ASGIApp, *, owns_widget: bool, **kwargs: object) -> None:
        super().__init__(app, **kwargs)  # type: ignore[arg-type]
        # True  → this instance owns `/widget/*`; False → it owns everything else.
        self._owns_widget = owns_widget

    def _owns(self, scope: Scope) -> bool:
        return _is_widget_public_path(scope.get("path", "")) == self._owns_widget

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._owns(scope):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


class _WidgetCORSMiddleware(_ScopedCORSMiddleware):
    """The public-widget CORS posture (owns `/widget/*`). Its allowlist is DYNAMIC —
    the union of active widget keys' registered origins — so it overrides
    `is_allowed_origin` to consult the TTL-cached `_WIDGET_ORIGIN_SNAPSHOT` instead
    of a static list, refreshing it (async) before Starlette's sync preflight /
    simple-response logic reads it. `allow_origins=()` (not `["*"]`) means the
    reflected `Access-Control-Allow-Origin` is ALWAYS the specific request Origin,
    never the literal `*` — even the dev-only `"*"` key opt-in only widens WHICH
    origins are admitted, never the reflected value."""

    def __init__(self, app: ASGIApp, **kwargs: object) -> None:
        super().__init__(app, owns_widget=True, allow_origins=(), **kwargs)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and self._owns(scope):
            await _WIDGET_ORIGIN_SNAPSHOT.ensure_fresh()
        await super().__call__(scope, receive, send)

    def is_allowed_origin(self, origin: str) -> bool:
        return _WIDGET_ORIGIN_SNAPSHOT.allows(origin)


app = FastAPI(title="Agentic RAG backend")
# Two complementary, path-scoped CORS postures (US-074). Order is immaterial — the
# predicates partition every request to exactly one — but the authenticated posture
# is listed first to mirror the historical single-middleware config it replaces.
# `/api/*` keeps EXACTLY its prior posture (FRONTEND_ORIGINS, the same methods +
# `allow_headers=["*"]`); the widget posture is keyed off per-key origins instead.
app.add_middleware(
    _ScopedCORSMiddleware,
    owns_widget=False,
    allow_origins=FRONTEND_ORIGINS,
    allow_methods=["POST", "GET", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)
app.add_middleware(
    _WidgetCORSMiddleware,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["content-type", _CONVERSATION_TOKEN_HEADER],
)


@app.on_event("startup")
async def _on_startup() -> None:
    # US-039: resolve the PARSER-selected parser once at startup so a
    # misconfigured PARSER (unknown value, or a commercial adapter with no API
    # key) fails CLOSED at boot rather than on the first upload. Default
    # PARSER=docling, so today's behavior is unchanged.
    get_selected_parser()
    # US-018: front-load docling's heavy import + model init (only when
    # PARSER=docling, the default) so the first file-upload ingest doesn't pay
    # that multi-second cost on the request path; a non-docling parser skips it.
    # Failures are swallowed — the lazy path still works. Parser internals stay
    # behind parsing.py (the ADR-0007 seam); main.py only knows the boundary
    # entry points (warmup_parsing / get_selected_parser / parse).
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

    # US-027: fail-closed embedder-drift guard. Probe-embed one string to
    # measure the LIVE embedder's actual output dim, then compare the running
    # embedder (model + dim) against the corpus stamp written at index time
    # (US-026). A genuine drift — different dims, OR the dangerous
    # same-dims-different-model case — RAISES and stops startup: silently
    # degrading retrieval is worse than a loud boot failure (same posture as
    # the semantic-layer load above). An empty corpus (no stamp) or an
    # unreadable stamp is a no-op; the probe is skipped entirely when there is
    # nothing to compare against, so an empty corpus pays no embedding call.
    # A failure to READ the stamp is logged and skipped (it must not mask a
    # drift — a broken embedder resurfaces on the first real query), but a
    # confirmed drift propagates. Likewise a probe API error (rate-limit / 5xx /
    # transient outage after retries) is logged and skips the check rather than
    # crash-looping boot on a momentarily-unreachable embedder; only
    # check_embedder_drift's RuntimeError (a real drift) aborts startup.
    async with httpx.AsyncClient(timeout=30.0) as http:
        try:
            stamp = await _fetch_embedding_stamp(http)
        except Exception:  # noqa: BLE001
            log.exception("embedder_guard.stamp_read_failed — skipping drift check")
            stamp = None
    if stamp is not None:
        try:
            measured_dim = await probe_embed_dim(embedder_client)
        except Exception:  # noqa: BLE001
            log.warning(
                "embedder_guard.probe_failed — skipping drift check (the embedder "
                "was unreachable at startup; a real drift resurfaces on the first "
                "query)",
                exc_info=True,
            )
            measured_dim = None
        if measured_dim is not None:
            check_embedder_drift(get_embedding_model(), measured_dim, stamp)
            log.info(
                "embedder_guard.ok — embedder %r @ %d dims matches the corpus stamp",
                get_embedding_model(),
                measured_dim,
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


def _service_role_headers() -> dict[str, str] | None:
    """Headers that bypass RLS. Three call paths use them:
      * the doc-owner authorization check on the share endpoints (US-039),
      * `_stamp_embedding_config` — the production `embedding_config` stamp
        WRITE (US-026), and
      * `_fetch_embedding_stamp` — the drift-guard stamp READ (US-027).
    Returns None when no service role key is configured so callers can fall
    back to user-scoped reads (or, for the stamp paths, skip)."""
    if not SUPABASE_SERVICE_ROLE_KEY:
        return None
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _require_service_role_headers() -> dict[str, str]:
    """Service-role headers, or a 503 when the support widget is unconfigured.

    The backend-mediated conversation-token surface (US-071) writes/reads under
    the service role; without `SUPABASE_SERVICE_ROLE_KEY` there is no path, so
    fail closed with a clear 503 instead of proceeding unauthenticated."""
    headers = _service_role_headers()
    if headers is None:
        raise HTTPException(
            status_code=503,
            detail="support widget is not configured (SUPABASE_SERVICE_ROLE_KEY unset)",
        )
    return headers


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
            openai_client=embedder_client,  # US-022: embed under the embedder role
            http=http,
            supabase_url=SUPABASE_URL,
            supabase_headers=_supabase_headers(user),
            query=query,
            top_k=pool_k,
            filters=filters,  # type: ignore[arg-type]
        )
    elif mode == "keyword":
        candidates = await keyword_only_search(
            openai_client=embedder_client,  # US-022: signature parity (unused)
            http=http,
            supabase_url=SUPABASE_URL,
            supabase_headers=_supabase_headers(user),
            query=query,
            top_k=pool_k,
            filters=filters,  # type: ignore[arg-type]
        )
    else:
        candidates = await search_documents(
            openai_client=embedder_client,  # US-022: embed under the embedder role
            http=http,
            supabase_url=SUPABASE_URL,
            supabase_headers=_supabase_headers(user),
            query=query,
            top_k=pool_k,
            filters=filters,  # type: ignore[arg-type]
        )

    if reranker_name == "none":
        return candidates, mode, reranker_name

    # US-023: answerer-role — the `llm` reranker runs on the answerer client.
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

    if name == "list_documents":
        try:
            validated_list = ListDocumentsInput(**args)
            items = await list_documents(
                http=http,
                supabase_url=SUPABASE_URL,
                supabase_headers=_supabase_headers(user),
                limit=validated_list.limit,
            )
            return json.dumps(
                {
                    "documents": [item.model_dump() for item in items],
                    "count": len(items),
                }
            )
        except Exception as e:  # noqa: BLE001 — surface to model as tool error
            log.exception("list_documents tool failed")
            return json.dumps({"error": str(e)})

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
                openai_client=openai_client,  # US-023: answerer-role
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
                openai_client=openai_client,  # US-023: answerer-role
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

        tools = [list_documents_tool_schema(), search_documents_tool_schema()]
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
    # US-025: keep the Responses path (hosted file_search + previous_response_id
    # threading) reachable only on a validated openai answerer. A per-request
    # mode=responses on a non-openai provider fails closed (400) rather than
    # silently falling back to completions — consistent with the startup guard.
    if mode == "responses" and not RESPONSES_MODE_AVAILABLE:
        reason = (
            f"provider={_ANSWERER_CONFIG.provider!r}"
            if _ANSWERER_CONFIG.provider != "openai"
            else f"an OpenAI-compatible host (OPENAI_BASE_URL={_ANSWERER_CONFIG.base_url!r})"
        )
        raise HTTPException(
            status_code=400,
            detail=(
                "chat mode 'responses' requires OpenAI proper (provider=openai with "
                f"no base_url override), but the resolved answerer is {reason}; use "
                "mode=completions. Responses mode (hosted file_search + server-side "
                "threading) is OpenAI-only and non-portable."
            ),
        )
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
        # US-025: only advertise `responses` when the answerer is openai, so the
        # frontend mode toggle never offers a mode the /api/chat guard would 400.
        "supported_chat_modes": (
            ["responses", "completions"] if RESPONSES_MODE_AVAILABLE else ["completions"]
        ),
        # US-025: file_search is the hosted Responses tool (_build_tools), reachable
        # only via _stream_responses_reply. Gate it on responses-availability so a
        # non-responses-capable answerer (Azure / base_url-override) never advertises
        # a capability /api/chat can't run, mirroring supported_chat_modes above.
        "file_search_enabled": bool(OPENAI_VECTOR_STORE_ID) and RESPONSES_MODE_AVAILABLE,
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
) -> tuple[dict[str, int], int | None]:
    """US-015: diff `chunks` against existing rows by SHA-256 and replace.

    Existing chunks whose content hash matches a new chunk have their
    embedding reused verbatim (pgvector round-trips as a string). Only new /
    modified chunks hit the OpenAI embeddings API. Pre-US-015 rows with a
    NULL `content_hash` are treated as "not reusable" and get re-embedded on
    their first post-migration ingest — one-time backfill via normal use.

    We fetch existing rows first, then embed, then delete-and-reinsert so a
    mid-pipeline failure never leaves half-written chunks behind (matches the
    pre-US-015 safety boundary).

    Returns `(metrics, produced_dim)` where metrics is the per-position counts
    (`chunks_added`, `chunks_removed`, `chunks_unchanged`, `chunks_total`) and
    `produced_dim` is the length of the freshly-produced embedding vectors (None
    when this ingest reused every chunk and produced no new embedding). US-026
    uses it to stamp the corpus embedding model + dim; pgvector rejects a
    wrong-length insert, so once the rows are written `produced_dim` necessarily
    equals the `chunks.embedding` column dim.
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
        await embed_texts(embedder_client, to_embed_texts) if to_embed_texts else []
    )
    # US-026: the actually-produced vector length, for the embedding_config
    # stamp. None when nothing new was embedded (all chunks reused) — the stamp
    # was then already written by the ingest that first produced these vectors.
    produced_dim = len(new_embeddings[0]) if new_embeddings else None
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

    return (
        {
            "chunks_added": chunks_added,
            "chunks_removed": chunks_removed,
            "chunks_unchanged": chunks_unchanged,
            "chunks_total": len(rows),
        },
        produced_dim,
    )


async def _stamp_embedding_config(
    http: httpx.AsyncClient,
    model: str,
    dim: int,
) -> None:
    """US-026: stamp the single-row `embedding_config` with the embedder model +
    the actually-produced vector dim, so a later embedder change is detectable
    (US-027's startup probe) instead of silently degrading retrieval.

    **Insert-if-absent, never update** (`resolution=ignore-duplicates` +
    `on_conflict=singleton` → `ON CONFLICT DO NOTHING`): the first ingest that
    produces embeddings seeds the row; every later ingest is a no-op. A routine
    per-user ingest must NOT rewrite the corpus's recorded model — if it did, an
    accidental model swap would re-stamp itself and blind US-027's drift guard.
    Overwriting the stamp is reserved for a deliberate bulk re-index (the
    seeders, service-role).

    Writes go through the **service-role** key, not the caller's JWT: the table's
    RLS restricts INSERT to service-role (no authenticated-insert policy), which
    closes the cross-tenant poisoning hole where any authenticated tenant could
    pre-seed the global singleton with an arbitrary (model, dim) and trip the
    US-027 guard for everyone. When no service-role key is configured the stamp
    is skipped (logged) — consistent with the US-027 guard, which also disables
    itself without the key. Best-effort: a stamp failure must not fail an
    otherwise-successful ingest, so errors are logged, not raised.
    """
    headers = _service_role_headers()
    if headers is None:
        log.warning(
            "embedding_config stamp skipped — SUPABASE_SERVICE_ROLE_KEY is unset, "
            "so the corpus stamp can't be written (its RLS restricts INSERT to "
            "service-role). Set the service-role key to enable US-026 stamping."
        )
        return
    try:
        r = await http.post(
            f"{SUPABASE_URL}/rest/v1/embedding_config",
            params={"on_conflict": "singleton"},
            headers={
                **headers,
                "Prefer": "resolution=ignore-duplicates, return=minimal",
            },
            json={"singleton": True, "model": model, "dim": dim},
        )
        r.raise_for_status()
    except httpx.HTTPError:
        log.warning("embedding_config stamp failed (model=%s dim=%d)", model, dim, exc_info=True)


async def _fetch_embedding_stamp(http: httpx.AsyncClient) -> EmbeddingStamp | None:
    """US-027: read the single-row `embedding_config` corpus stamp for the
    startup drift guard.

    This is a *system* read with no user in scope, and the stamp's RLS exposes
    it only to `authenticated` / service-role (never `anon`), so it goes through
    the service-role key. Returns None — making the guard a no-op — when:
      * no service-role key is configured (the stamp can't be read at all; the
        guard is disabled and that is logged loudly), or
      * the corpus has no stamp yet (empty corpus — nothing has been indexed).
    """
    headers = _service_role_headers()
    if headers is None:
        log.warning(
            "embedder_guard.disabled — SUPABASE_SERVICE_ROLE_KEY is unset, so the "
            "embedding_config stamp can't be read at startup (its RLS hides it from "
            "anon). Set the service-role key to enable US-027 drift detection."
        )
        return None
    r = await http.get(
        f"{SUPABASE_URL}/rest/v1/embedding_config",
        params={"select": "model,dim", "limit": "1"},
        headers=headers,
    )
    r.raise_for_status()
    rows = r.json()
    if not rows:
        return None
    return EmbeddingStamp(model=rows[0]["model"], dim=int(rows[0]["dim"]))


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
            # US-018/US-039: multi-format parsing via the PARSER-selected parser
            # (ADR-0007). `.parse` raises `UnsupportedFormatError` on unknown
            # types and `ValueError` with a human-readable message on parse
            # failure — both are caught by the outer except below and surfaced
            # as `status=error` with `error_message` so the UI can show why a
            # given file failed.
            try:
                text = await asyncio.to_thread(
                    get_selected_parser().parse,
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

            # US-042 (ADR-0007): the markdown `str` is the ONLY coupling between
            # the parser boundary and the chunker — `chunk_text` is fed every
            # parser's output identically, with no isinstance/`name ==` branch on
            # the selected parser between parse and chunk. Proven by
            # `test_parser_chunker_contract.py`.
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
            metrics, produced_dim = await _reconcile_chunks(http, user, document_id, chunks)
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

            # US-026: stamp the corpus with the embedder model + the dim it just
            # produced. Only when this ingest actually embedded something
            # (produced_dim is not None) — a reuse-only ingest leaves the
            # existing stamp untouched. Insert-if-absent, so the first such
            # ingest seeds it and the rest no-op.
            if produced_dim is not None:
                await _stamp_embedding_config(
                    http, get_embedding_model(), produced_dim
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
                openai_client, text, doc["filename"]  # US-023: answerer-role
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
            params={
                "id": f"eq.{doc_id}",
                "select": "id,user_id,status,workspace_id",
            },
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
        params={"id": f"eq.{doc_id}", "select": "id,user_id,status,workspace_id"},
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
    doc_workspace_id: str,
) -> tuple[PrincipalType, str, str] | None:
    """Try profiles.email, then principals.name. None → 404 at the endpoint.

    Returns (principal_type, principal_id, display_name). Reads under the
    caller's JWT: profiles has permissive select RLS (US-037), while principals
    is membership-gated (US-006) — so a caller resolves only groups in their own
    workspaces and an out-of-workspace group name resolves to nothing (404).
    Group resolution is additionally scoped to the target document's workspace:
    per-workspace unique (workspace_id, name) means the same name can exist in
    several workspaces the caller belongs to, so without this filter `limit 1`
    would bind nondeterministically.
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
        params={
            "name": f"eq.{identifier}",
            "workspace_id": f"eq.{doc_workspace_id}",
            "select": "id,name",
            "limit": "1",
        },
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
            http, headers, req.principal_email_or_name.strip(),
            doc["workspace_id"],
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
            openai_client=embedder_client,  # US-022: embed under the embedder role
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
            openai_client=embedder_client,  # US-022: embed under the embedder role
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
            openai_client=openai_client,  # US-023: answerer-role
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
                openai_client=openai_client,  # US-023: answerer-role
                http=http,
                supabase_url=SUPABASE_URL,
                supabase_headers=_supabase_headers(user),
                document_id=req.document_id,
                task=req.task,
            )
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
    return result.model_dump()


# ---------------------------------------------------------------------------
# US-071: opaque per-conversation customer token — issuance, hashed storage,
# resume. The anonymous customer is structurally OFF the Supabase trust surface
# (ADR-0008): these public widget endpoints accept the RAW opaque token (in the
# X-Conversation-Token header, NOT an Authorization bearer / Supabase JWT), the
# backend hashes it and resolves it via the service-role-only `resume_conversation`
# RPC. Issuance (`_issue_conversation_token`) is invoked by US-078's first-message
# conversation-creation flow; resume/transcript below let a reloaded iframe
# revalidate and reconnect. Public-widget CORS is US-074's concern — these routes
# do not widen the authenticated `/api/*` CORS posture.
# ---------------------------------------------------------------------------

def _public_conversation_view(conv: dict) -> dict:
    """Curate the customer-facing conversation shape.

    The `resume_conversation` RPC returns `workspace_id` for server-side use, but
    the anonymous customer surface must not leak internal workspace topology, so
    the public view exposes only id/status/created_at.
    """
    return {
        "id": conv["id"],
        "status": conv["status"],
        "created_at": conv["created_at"],
    }


async def _issue_conversation_token(
    http: httpx.AsyncClient, conversation_id: str
) -> str:
    """Issue a fresh opaque token bound to `conversation_id`; store only its hash.

    Returns the RAW token. Its caller (US-078, first-message conversation
    creation) returns it to the iframe EXACTLY ONCE — it is never stored, logged,
    or echoed again. The token table is backend-mediated (RLS deny-all), so this
    writes under the service role.
    """
    headers = _require_service_role_headers()
    raw_token = generate_conversation_token()
    expires_at = (
        datetime.now(timezone.utc)
        + timedelta(seconds=CONVERSATION_TOKEN_TTL_SECONDS)
    ).isoformat()
    r = await http.post(
        f"{SUPABASE_URL}/rest/v1/conversation_tokens",
        headers=headers,
        json={
            "token_hash": hash_conversation_token(raw_token),
            "conversation_id": conversation_id,
            "expires_at": expires_at,
        },
    )
    r.raise_for_status()
    return raw_token


async def _resume_conversation_by_token(
    http: httpx.AsyncClient, raw_token: str, *, slide: bool = True
) -> dict | None:
    """Revalidate an opaque customer token and return its bound conversation row.

    Hashes the raw token and calls the service-role-only `resume_conversation`
    RPC, which atomically checks (not expired AND status != 'resolved') and
    returns the ONE conversation the token is bound to. No caller-supplied
    conversation id reaches the RPC, so a token for X structurally cannot resolve
    to any other conversation. Returns None on a miss (missing/expired/resolved) —
    the iframe's cue to start a fresh conversation. Returns the full RPC row (incl.
    workspace_id) for server-side use; endpoints curate it via
    `_public_conversation_view`.

    `slide` controls the activity refresh: the POST /resume path leaves it True so
    a resume slides the 24h window; the read-only GET transcript path passes False
    so a nominally-safe/idempotent GET never extends the token's lifetime.
    """
    headers = _require_service_role_headers()
    r = await http.post(
        f"{SUPABASE_URL}/rest/v1/rpc/resume_conversation",
        headers=headers,
        json={"p_token_hash": hash_conversation_token(raw_token), "p_slide": slide},
    )
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


@app.post("/widget/conversations/resume")
async def widget_resume_conversation(
    x_conversation_token: str | None = Header(
        default=None, alias=_CONVERSATION_TOKEN_HEADER
    ),
) -> dict:
    """US-071: revalidate the opaque customer token and resume its conversation.

    The anonymous customer presents the raw token it stored in the iframe origin
    (NOT a Supabase JWT). On success returns the conversation so the iframe can
    reconnect its SSE (US-081) and GET the transcript. A missing/expired/resolved
    token → 401, the cue to start a fresh conversation on the next first message;
    this endpoint never creates a row.
    """
    if not x_conversation_token:
        raise HTTPException(status_code=401, detail="missing conversation token")
    async with httpx.AsyncClient(timeout=10.0) as http:
        conv = await _resume_conversation_by_token(http, x_conversation_token)
    if conv is None:
        raise HTTPException(
            status_code=401, detail="invalid or expired conversation token"
        )
    return {"conversation": _public_conversation_view(conv)}


@app.get("/widget/conversations/{conversation_id}/transcript")
async def widget_conversation_transcript(
    conversation_id: str,
    x_conversation_token: str | None = Header(
        default=None, alias=_CONVERSATION_TOKEN_HEADER
    ),
) -> dict:
    """US-071: return a conversation's transcript, authorized by the opaque token.

    Security-critical binding: the token is resolved to its OWN conversation and
    the path `conversation_id` MUST match it, so a token for X can never read Y's
    transcript. The same RPC re-checks not-expired AND not-resolved, so an
    expired/resolved token is rejected here too.

    A GET is nominally safe/idempotent, so the binding check is resolved with
    `slide=False`: reading a transcript (browser prefetch, link-preview crawler,
    transparent retry) must never extend the token's 24h window. Only POST /resume
    counts as activity.
    """
    if not x_conversation_token:
        raise HTTPException(status_code=401, detail="missing conversation token")
    async with httpx.AsyncClient(timeout=10.0) as http:
        conv = await _resume_conversation_by_token(
            http, x_conversation_token, slide=False
        )
        if conv is None or conv["id"] != conversation_id:
            # Token invalid/expired/resolved, OR bound to a different conversation
            # (a token for X requesting Y). Both collapse to "not authorized for
            # this id" — and to a not-found-shaped 401 so the binding is opaque.
            raise HTTPException(
                status_code=401,
                detail="invalid conversation token for this conversation",
            )
        headers = _require_service_role_headers()
        r = await http.get(
            f"{SUPABASE_URL}/rest/v1/conversation_messages",
            params={
                "conversation_id": f"eq.{conversation_id}",
                "role": "in.(user,assistant)",
                "select": "id,role,content,created_at",
                "order": "created_at.asc",
            },
            headers=headers,
        )
        r.raise_for_status()
        messages = r.json()
    return {
        "conversation": _public_conversation_view(conv),
        "messages": messages,
    }


# ---------------------------------------------------------------------------
# US-072: widget_keys — the non-secret public-key registry + the not-revoked gate
# every key resolution passes before anything is minted/created (ADR-0008).
#
# Two faces, deliberately on different CORS/auth surfaces:
#   * ADMIN management (`/api/support/widget-keys*`) — authed by the admin's real
#     Supabase JWT; issuance/list/revoke INSERT/SELECT/UPDATE under that JWT so the
#     admin RLS (role='admin' of the workspace) is the authorization. The /support
#     /settings UI (US-090) is the caller. These ride the authenticated /api/* CORS.
#   * PUBLIC resolution (`/widget/keys/resolve`) — anonymous; the widget loader
#     (US-083) presents its non-secret public_key on open. Resolution gates on
#     `revoked_at IS NULL` under the SERVICE ROLE (the anonymous widget holds no
#     Postgres role) and leaks no workspace topology. Public-widget CORS is US-074,
#     per-key origin enforcement US-073, rate-limiting US-076 — all layer on top of
#     this gate without changing it; these routes do not widen the /api/* posture.
# ---------------------------------------------------------------------------


class IssueWidgetKeyRequest(BaseModel):
    workspace_id: str
    label: str | None = None
    allowed_origins: list[str] = Field(default_factory=list)


class ResolveWidgetKeyRequest(BaseModel):
    public_key: str


async def _ensure_workspace_bot(
    http: httpx.AsyncClient, workspace_id: str
) -> str | None:
    """US-072 → US-069 hook: idempotently provision the per-workspace support bot
    on first widget-key issuance, returning its user id (or None).

    US-069 owns the provisioning primitive (`support_bot.provision_workspace_bot`),
    which creates (or returns the existing) bot `auth.users` row +
    `workspace_membership(role='member', is_bot=true)` row under the service role,
    exactly one per workspace (lazy/idempotent, race-safe). US-072 is its single
    caller — "first key issued enables support" (PRD US-069/US-072). Because it is
    idempotent, invoking it on every issuance is safe: the first issuance
    provisions, the rest return the same id.

    Best-effort: key issuance must NEVER fail because of provisioning, so a
    provisioning error (e.g. SUPABASE_SERVICE_ROLE_KEY unset) is logged and
    swallowed — the key is still issued and the bot is (re)provisioned
    idempotently later (US-069 is also triggered lazily at first conversation,
    US-078). The ImportError guard is belt-and-suspenders for a build that ships
    the widget without the support-bot module. Never raises.
    """
    try:
        from support_bot import provision_workspace_bot  # US-069
    except ImportError:
        log.info(
            "widget_key.bot_provision_skipped — support_bot module unavailable; "
            "workspace=%s key issued, bot provisions lazily at first conversation",
            workspace_id,
        )
        return None
    try:
        # workspace_id is the only positional arg; http/url/key are keyword-only.
        return await provision_workspace_bot(workspace_id, http=http)
    except Exception:  # best-effort: never block key issuance on provisioning
        log.warning(
            "widget_key.bot_provision_failed — workspace=%s; key issued, bot will "
            "provision lazily later",
            workspace_id,
            exc_info=True,
        )
        return None


async def _resolve_widget_key(
    http: httpx.AsyncClient, public_key: str
) -> dict | None:
    """Resolve a widget public key to its workspace, gating on NOT-REVOKED FIRST.

    The not-revoked gate IS the query — a service-role read filtered by
    `public_key=eq AND revoked_at=is.null`. A revoked or unknown key matches zero
    rows and returns None: the caller's cue to refuse to start anything (no
    conversation row, no token, no minting). Returns the key's workspace + metadata
    for server-side use (US-078 creates the conversation from it); the public
    response NEVER echoes workspace topology (see `widget_resolve_key`).

    Stays cheap and side-effect-free (one indexed SELECT) so widget OPEN — which
    per US-078 does ONLY key resolution — is cheap. The `(workspace_id,
    bot_user_id)` pair the PRD describes is completed downstream: the bot is
    provisioned at issuance (the US-069 hook) and assigned to the conversation by
    US-078, so resolution does not provision per open.

    Reads under the service role because the anonymous widget holds no Postgres
    role; the admin RLS on widget_keys gates only the authenticated admin path.
    """
    headers = _require_service_role_headers()
    r = await http.get(
        f"{SUPABASE_URL}/rest/v1/widget_keys",
        params={
            "public_key": f"eq.{public_key}",
            "revoked_at": "is.null",
            "select": "id,workspace_id,label,allowed_origins",
            "limit": "1",
        },
        headers=headers,
    )
    try:
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        # An upstream failure must surface as 502, never collapse into the
        # not-found/revoked (None → 404) case the empty result encodes.
        if e.response.status_code >= 500:
            raise HTTPException(
                status_code=502,
                detail="could not resolve widget key (upstream error)",
            )
        raise
    rows = r.json()
    return rows[0] if rows else None


@app.post("/api/support/widget-keys")
async def issue_widget_key(
    req: IssueWidgetKeyRequest, user: AuthedUser = Depends(get_user)
) -> dict:
    """US-072: issue a new (active) widget key for a workspace the caller admins.

    The INSERT runs under the admin's OWN JWT, so the widget_keys admin RLS
    (role='admin' of the row's workspace) IS the authorization — a non-admin's
    insert is rejected by Postgres, not by app code. On the workspace's first key
    this also triggers lazy bot provisioning (US-069); rotation is simply this
    endpoint again followed by a revoke of the old key (no auto-rotation).

    The response includes `public_key` ON PURPOSE: it is non-secret and the admin
    must copy it into their page's loader snippet. (This is the opposite of the
    customer token, which is returned once and never logged.)

    Issuance-time guard (issue #36, US-073 follow-up): an empty/blank
    `allowed_origins` is rejected with a 400 BEFORE any key is generated or the bot
    is provisioned. Under US-073's fail-closed resolution gate such a key is
    INACTIVE and would silently never resolve, so we refuse to mint a dead key
    rather than surprise the admin later. This is defense-in-depth UX, not a
    security boundary — the resolution gate already prevents an originless key from
    working; here we just make the failure loud at creation. The dev-only `"*"`
    wildcard is a non-empty allowlist and passes.
    """
    if not has_registered_origin(req.allowed_origins):
        raise HTTPException(
            status_code=400,
            detail=(
                "allowed_origins must list at least one origin; a key with no "
                "origins never resolves (US-073 fail-closed)"
            ),
        )
    public_key = generate_public_key()
    async with httpx.AsyncClient(timeout=10.0) as http:
        try:
            r = await http.post(
                f"{SUPABASE_URL}/rest/v1/widget_keys",
                headers=_supabase_headers(user),
                json={
                    "workspace_id": req.workspace_id,
                    "public_key": public_key,
                    "label": req.label,
                    "allowed_origins": req.allowed_origins,
                    "created_by": user.id,
                },
            )
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status in (401, 403):
                raise HTTPException(
                    status_code=403,
                    detail="must be an admin of this workspace to issue a widget key",
                )
            if status >= 500:
                raise HTTPException(
                    status_code=502,
                    detail="could not issue widget key (upstream error)",
                )
            # FK violation (unknown workspace) or any other 4xx → bad request.
            raise HTTPException(
                status_code=400, detail="could not issue widget key"
            )
        created = r.json()[0]
        # First key for the workspace enables support: provision the bot lazily.
        # Idempotent, best-effort — does not gate the issuance result.
        await _ensure_workspace_bot(http, req.workspace_id)
    # US-074: a freshly issued key adds registered origins; drop the widget CORS
    # snapshot so the new origin is admitted on the very next preflight (this
    # instance), not after the TTL. The cross-instance lag is bounded by the TTL.
    _WIDGET_ORIGIN_SNAPSHOT.invalidate()
    return {"widget_key": created}


@app.get("/api/support/widget-keys")
async def list_widget_keys(
    workspace_id: str, user: AuthedUser = Depends(get_user)
) -> dict:
    """List a workspace's widget keys (active + revoked) for the admin UI (US-090).

    Read under the admin's JWT, so the widget_keys admin RLS returns rows only for
    workspaces the caller administers — a non-admin gets an empty list, not a leak.
    Includes revoked keys so the UI can show active-vs-revoked for rotation.
    """
    async with httpx.AsyncClient(timeout=10.0) as http:
        r = await http.get(
            f"{SUPABASE_URL}/rest/v1/widget_keys",
            params={
                "workspace_id": f"eq.{workspace_id}",
                "select": "id,workspace_id,public_key,label,allowed_origins,"
                "revoked_at,created_at",
                "order": "created_at.desc",
            },
            headers=_supabase_headers(user),
        )
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status >= 500:
                raise HTTPException(
                    status_code=502,
                    detail="could not list widget keys (upstream error)",
                )
            # A malformed non-UUID workspace_id → PostgREST 400/22P02.
            raise HTTPException(status_code=400, detail="invalid workspace id")
    return {"widget_keys": r.json()}


@app.post("/api/support/widget-keys/{key_id}/revoke")
async def revoke_widget_key(
    key_id: str, user: AuthedUser = Depends(get_user)
) -> dict:
    """US-072: revoke a widget key — a one-way flip of `revoked_at` to now().

    Runs under the admin's JWT (admin RLS authorizes). Revoking blocks NEW
    conversations (resolution gates on `revoked_at IS NULL`) but NEVER terminates a
    live one — the opaque per-conversation token (US-071) is independent of the key
    once minted. Only an active key is flipped (`revoked_at=is.null`), so a
    double-revoke is a no-op rather than moving the timestamp.
    """
    async with httpx.AsyncClient(timeout=10.0) as http:
        r = await http.patch(
            f"{SUPABASE_URL}/rest/v1/widget_keys",
            params={"id": f"eq.{key_id}", "revoked_at": "is.null"},
            headers=_supabase_headers(user),
            json={"revoked_at": datetime.now(timezone.utc).isoformat()},
        )
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status >= 500:
                raise HTTPException(
                    status_code=502,
                    detail="could not revoke widget key (upstream error)",
                )
            # A malformed non-UUID key_id → PostgREST 400/22P02.
            raise HTTPException(status_code=400, detail="invalid widget key id")
        rows = r.json()
    if not rows:
        # Not the caller's to manage (RLS-hidden), nonexistent, or already revoked —
        # all collapse to "nothing active to revoke here".
        raise HTTPException(status_code=404, detail="no active widget key to revoke")
    # US-074: a revoked key's origins must stop being CORS-trusted; drop the
    # snapshot so the next preflight reloads without them (this instance now; the
    # TTL bounds the cross-instance lag).
    _WIDGET_ORIGIN_SNAPSHOT.invalidate()
    return {"widget_key": rows[0]}


@app.post("/widget/keys/resolve")
async def widget_resolve_key(
    req: ResolveWidgetKeyRequest, request: Request
) -> dict:
    """US-072/US-073: public key resolution — NOT-REVOKED then per-key ORIGIN gate.

    The widget loader (US-083) calls this on open with its non-secret public_key.
    A valid, active key whose allowlist admits the request `Origin` returns
    `{"active": true}`; a revoked, unknown, malformed, originless, or
    unlisted-origin request returns the SAME opaque 404 — the widget's cue to
    refuse to start (no conversation, no token; US-078 owns conversation creation
    and re-resolves server-side using this same origin helper). The response
    leaks NO workspace topology (workspace_id / bot_user_id stay server-side) and
    nothing about whether the key exists or which origins it allows.

    US-073 origin gate (defense-in-depth, NOT a hard control): the public_key is
    non-secret and `Origin` is forgeable off-browser, so this only blunts casual
    key-lifting and in-browser cross-site abuse; the hard abuse controls are the
    rate limit + circuit breaker (US-076/077) and the leaked-key blast radius is
    the already-public KB. The check is fail-closed (`is_origin_allowed`): a key
    with an empty/null allowlist is INACTIVE, and a missing/unlisted `Origin` is
    refused — never fail-open. The not-revoked gate stays FIRST; origin is an
    additional gate layered on top of it, ordered after so a revoked key and an
    unlisted origin are indistinguishable in the response.

    Anonymous public surface: Public-widget CORS is US-074's concern,
    rate-limiting US-076's — both layer on top of this gate. These routes do not
    widen the authenticated /api/* CORS posture.
    """
    if not is_widget_public_key(req.public_key):
        raise HTTPException(status_code=404, detail="unknown or inactive widget key")
    async with httpx.AsyncClient(timeout=10.0) as http:
        resolved = await _resolve_widget_key(http, req.public_key)
    if resolved is None:
        raise HTTPException(status_code=404, detail="unknown or inactive widget key")
    # US-073: per-key registered-origin allowlist, fail-closed. Same opaque 404 as
    # revoked/unknown so the response never reveals that the key exists or which
    # origins it permits.
    if not is_origin_allowed(
        request.headers.get("origin"), resolved.get("allowed_origins")
    ):
        raise HTTPException(status_code=404, detail="unknown or inactive widget key")
    return {"active": True}


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "ok": True,
        "model": OPENAI_MODEL,
        "file_search": bool(OPENAI_VECTOR_STORE_ID),
        # US-022: per-role provider binding, so ops can confirm a split
        # deployment (e.g. answer on azure, embed on openai) took effect.
        "providers": {
            "answerer": _ANSWERER_CONFIG.provider,
            "embedder": _EMBEDDER_CONFIG.provider,
            "judge": _JUDGE_CONFIG.provider,
        },
        # US-024: surface the resolved Azure deployment per azure-bound role so
        # ops can confirm deployment-name addressing took effect (None = the
        # per-call model arg is used as the deployment). Omitted for openai roles.
        "azure_deployments": {
            role: cfg.azure_deployment
            for role, cfg in (
                ("answerer", _ANSWERER_CONFIG),
                ("embedder", _EMBEDDER_CONFIG),
                ("judge", _JUDGE_CONFIG),
            )
            if cfg.provider == "azure"
        },
        "embedding_model": get_embedding_model(),
    }
