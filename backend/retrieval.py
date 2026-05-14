"""US-010: search_documents retrieval tool.

Embeds the caller's query with OpenAI, invokes the public.match_chunks RPC
(which runs under the user's JWT so RLS keeps cross-user chunks invisible),
and returns the top-k results that clear the similarity threshold.

The Pydantic input schema is re-used both for runtime validation (when the
backend calls this tool on behalf of the agent in US-011) and as the JSON
Schema handed to OpenAI via `tools[]`, so the two can never drift.

US-017: optional `filters` narrow retrieval by the structured metadata
extracted in US-016 (topics, document_type, published_date range). The
schema is surfaced to the agent via the tool description so it knows what
filters are valid.

US-020: `keyword_search` is the Postgres full-text counterpart to
match_chunks. It calls the `keyword_search` RPC (added in
20260505120100_add_keyword_search_fn.sql) and returns rows in the same
SearchDocumentsResult shape so US-021 can fuse the two via RRF without a
per-side projection.

US-021: `hybrid_search` runs both `match_chunks` and `keyword_search` in
parallel against a wider candidate pool, then fuses the two rankings via
Reciprocal Rank Fusion (RRF). The fused score replaces `similarity` on
returned rows. The `search_documents` tool dispatches through this by
default; `RETRIEVAL_MODE=vector` flips back to vector-only as a safety
escape hatch.
"""

from __future__ import annotations

import asyncio
import os
from datetime import date
from typing import Any, Literal

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel, Field, model_validator

from embeddings import embed_texts, to_pgvector

DEFAULT_TOP_K = 5
MAX_TOP_K = 50
DEFAULT_SIMILARITY_THRESHOLD = 0.3

# US-021: RRF constant. 60 is the canonical default from Cormack et al. — it
# damps near the top of either ranking so a #1 in one list and a #1 in the
# other contribute roughly equally, while still penalising far-down items.
DEFAULT_RRF_K = 60

# Each side of hybrid pulls top_k * this multiplier candidates before fusion,
# clamped to MAX_TOP_K. A wider pool gives RRF room to surface items that one
# strategy ranks low but the other ranks well — too narrow and hybrid
# degenerates to "whichever side ranked first".
HYBRID_POOL_MULTIPLIER = 4

RetrievalMode = Literal["hybrid", "vector", "keyword"]


class DateRange(BaseModel):
    """Inclusive ISO-8601 date range on documents.metadata->>'published_date'.

    Either bound may be omitted for an open-ended range. Documents with a
    null/missing published_date are excluded whenever any bound is set — the
    agent has explicitly asked for dated results.
    """

    start: date | None = Field(
        default=None,
        description="Inclusive lower bound (YYYY-MM-DD). Omit for no lower bound.",
    )
    end: date | None = Field(
        default=None,
        description="Inclusive upper bound (YYYY-MM-DD). Omit for no upper bound.",
    )

    @model_validator(mode="after")
    def _check_order(self) -> "DateRange":
        if self.start and self.end and self.start > self.end:
            raise ValueError("date_range.start must be <= date_range.end")
        return self


class MetadataFilters(BaseModel):
    """Optional filters over the US-016 document metadata schema.

    All fields are optional; omitted fields apply no filter. `topics` matches
    documents whose metadata.topics contains ANY of the listed topics (OR
    semantics) — narrow by passing a single value.
    """

    topics: list[str] | None = Field(
        default=None,
        min_length=1,
        description=(
            "Return only chunks whose parent document's metadata.topics "
            "contains ANY of these values (OR match). Omit for no topic filter."
        ),
    )
    document_type: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Return only chunks whose parent document's metadata.document_type "
            "equals this value exactly. Omit for no type filter."
        ),
    )
    date_range: DateRange | None = Field(
        default=None,
        description=(
            "Return only chunks whose parent document's metadata.published_date "
            "falls within this inclusive range. Documents with no published_date "
            "are excluded when this filter is set."
        ),
    )


class SearchDocumentsInput(BaseModel):
    query: str = Field(..., min_length=1, description="Natural-language query to search the user's ingested documents for.")
    top_k: int = Field(
        default=DEFAULT_TOP_K,
        ge=1,
        le=MAX_TOP_K,
        description="Max number of chunks to return (1..50).",
    )
    filters: MetadataFilters | None = Field(
        default=None,
        description=(
            "Optional metadata filters (US-017). Applied as an AND against the "
            "parent document's US-016 structured metadata. Omit entirely when the "
            "user's question doesn't imply a narrowing (topic, format, or date)."
        ),
    )


class SearchDocumentsResult(BaseModel):
    id: str
    document_id: str
    chunk_index: int
    content: str
    similarity: float
    filename: str


def get_similarity_threshold() -> float:
    raw = os.environ.get("SEARCH_SIMILARITY_THRESHOLD")
    if raw is None or raw == "":
        return DEFAULT_SIMILARITY_THRESHOLD
    try:
        v = float(raw)
    except ValueError as e:
        raise ValueError(
            f"SEARCH_SIMILARITY_THRESHOLD must be a float, got {raw!r}"
        ) from e
    if not 0.0 <= v <= 1.0:
        raise ValueError(f"SEARCH_SIMILARITY_THRESHOLD must be in [0,1], got {v}")
    return v


def get_rrf_k() -> int:
    """RRF damping constant (`HYBRID_RRF_K` env, default 60)."""
    raw = os.environ.get("HYBRID_RRF_K")
    if raw is None or raw == "":
        return DEFAULT_RRF_K
    try:
        v = int(raw)
    except ValueError as e:
        raise ValueError(f"HYBRID_RRF_K must be an int, got {raw!r}") from e
    if v < 1:
        raise ValueError(f"HYBRID_RRF_K must be >= 1, got {v}")
    return v


def get_retrieval_mode() -> RetrievalMode:
    """`RETRIEVAL_MODE` env: `hybrid` (default) | `vector` | `keyword`.

    `keyword` was added in US-033 so the retrieval eval can sweep all three
    modes through the same env switch the production path uses.
    """
    raw = (os.environ.get("RETRIEVAL_MODE") or "hybrid").strip().lower()
    if raw not in ("hybrid", "vector", "keyword"):
        raise ValueError(
            f"RETRIEVAL_MODE must be 'hybrid', 'vector', or 'keyword', got {raw!r}"
        )
    return raw  # type: ignore[return-value]


# Kept as a module-level string so both the tool schema description and the
# chat system prompt stay in sync — if the metadata schema evolves in US-016,
# updating this constant propagates to both surfaces automatically.
METADATA_SCHEMA_HINT = (
    "Each document carries structured metadata extracted at ingestion:\n"
    "  - title: string | null\n"
    "  - authors: string[]\n"
    "  - topics: string[]  (short lowercase tags, e.g. \"ml\", \"finance\")\n"
    "  - published_date: ISO-8601 date (YYYY-MM-DD) | null\n"
    "  - document_type: string | null  (e.g. \"paper\", \"blog\", \"spec\")\n"
    "Use `filters.topics` / `filters.document_type` / `filters.date_range` "
    "when the user's question implies a narrowing (a topic name, a format, "
    "or a time window). Omit filters otherwise — over-filtering returns zero "
    "results."
)


def search_documents_tool_schema() -> dict[str, Any]:
    """Chat Completions `tools[]` entry for the search_documents tool."""
    return {
        "type": "function",
        "function": {
            "name": "search_documents",
            "description": (
                "Search the caller's ingested documents via vector similarity. "
                "Returns the top-k most relevant chunks with filename and a "
                "cosine-similarity score in [0,1]. Results are already scoped "
                "to the caller's own documents — do not attempt to filter by "
                "user.\n\n" + METADATA_SCHEMA_HINT
            ),
            "parameters": SearchDocumentsInput.model_json_schema(),
        },
    }


def _filters_to_rpc_payload(filters: MetadataFilters | None) -> dict[str, Any]:
    """Flatten the nested Pydantic filter shape to the RPC's named params."""
    if filters is None:
        return {
            "filter_topics": None,
            "filter_document_type": None,
            "filter_date_from": None,
            "filter_date_to": None,
        }
    date_from = filters.date_range.start if filters.date_range else None
    date_to = filters.date_range.end if filters.date_range else None
    return {
        "filter_topics": filters.topics,
        "filter_document_type": filters.document_type,
        "filter_date_from": date_from.isoformat() if date_from else None,
        "filter_date_to": date_to.isoformat() if date_to else None,
    }


async def search_documents(
    openai_client: AsyncOpenAI,
    http: httpx.AsyncClient,
    supabase_url: str,
    supabase_headers: dict[str, str],
    query: str,
    top_k: int = DEFAULT_TOP_K,
    filters: MetadataFilters | None = None,
) -> list[SearchDocumentsResult]:
    """Embed `query`, call match_chunks RPC under the user's JWT, return rows.

    `supabase_headers` MUST carry the user's access token so PostgREST runs
    the RPC as the `authenticated` role with RLS active. Calling this with
    service-role headers would bypass RLS and leak cross-user chunks.
    """
    embeddings = await embed_texts(openai_client, [query])
    if not embeddings:
        return []
    payload: dict[str, Any] = {
        "query_embedding": to_pgvector(embeddings[0]),
        "match_threshold": get_similarity_threshold(),
        "match_count": min(max(top_k, 1), MAX_TOP_K),
    }
    payload.update(_filters_to_rpc_payload(filters))
    r = await http.post(
        f"{supabase_url}/rest/v1/rpc/match_chunks",
        headers=supabase_headers,
        json=payload,
    )
    r.raise_for_status()
    return [SearchDocumentsResult(**row) for row in r.json()]


async def keyword_search(
    http: httpx.AsyncClient,
    supabase_url: str,
    supabase_headers: dict[str, str],
    query: str,
    top_k: int = DEFAULT_TOP_K,
    filters: MetadataFilters | None = None,
) -> list[SearchDocumentsResult]:
    """Call keyword_search RPC under the user's JWT, return ranked rows.

    The `similarity` field on each result carries the ts_rank_cd score, which
    is unbounded (unlike cosine similarity in [0,1]) and not directly
    comparable to match_chunks results — US-021 RRF fuses by rank position,
    so the magnitude doesn't need to match. `filters` parity with match_chunks
    (US-017) was added in 20260505121000_keyword_search_filters.sql so hybrid
    queries apply the same metadata filter on both halves.
    """
    payload: dict[str, Any] = {
        "query": query,
        "match_count": min(max(top_k, 1), MAX_TOP_K),
    }
    payload.update(_filters_to_rpc_payload(filters))
    r = await http.post(
        f"{supabase_url}/rest/v1/rpc/keyword_search",
        headers=supabase_headers,
        json=payload,
    )
    r.raise_for_status()
    return [SearchDocumentsResult(**row) for row in r.json()]


async def keyword_only_search(
    openai_client: AsyncOpenAI,  # noqa: ARG001 — accepted for signature parity
    http: httpx.AsyncClient,
    supabase_url: str,
    supabase_headers: dict[str, str],
    query: str,
    top_k: int = DEFAULT_TOP_K,
    filters: MetadataFilters | None = None,
) -> list[SearchDocumentsResult]:
    """Keyword-only retrieval (US-033). Thin wrapper over `keyword_search`.

    The wrapper exists so the dispatcher in `_retrieve_for_agent` (and the
    US-033 eval runner) can route to all three modes through a uniform
    function signature — `search_documents` / `keyword_only_search` /
    `hybrid_search` all take the same arguments. `openai_client` is
    accepted but ignored; keyword retrieval doesn't need embeddings.
    """
    return await keyword_search(
        http=http,
        supabase_url=supabase_url,
        supabase_headers=supabase_headers,
        query=query,
        top_k=top_k,
        filters=filters,
    )


def _rrf_fuse(
    rankings: list[list[SearchDocumentsResult]],
    top_k: int,
    k: int,
) -> list[SearchDocumentsResult]:
    """Reciprocal Rank Fusion over multiple rankings of SearchDocumentsResult.

    For each ranking, item at rank r (1-indexed) contributes `1/(k+r)` to its
    fused score. Duplicate items across rankings have their scores summed —
    this is the deduplication path required by US-021. The returned list
    carries the fused score in `similarity`, sorted descending. Ties broken
    by chunk id for deterministic ordering across runs.
    """
    scores: dict[str, float] = {}
    by_id: dict[str, SearchDocumentsResult] = {}
    for ranked in rankings:
        for rank, item in enumerate(ranked, start=1):
            scores[item.id] = scores.get(item.id, 0.0) + 1.0 / (k + rank)
            # First-seen wins for the row payload — both rankings carry the
            # same content/filename/etc for a given chunk id, so this is
            # really just picking one to surface. Vector ranking is iterated
            # first (caller's responsibility) to keep its filename casing.
            by_id.setdefault(item.id, item)
    ordered_ids = sorted(scores.keys(), key=lambda i: (-scores[i], i))
    fused: list[SearchDocumentsResult] = []
    for chunk_id in ordered_ids[:top_k]:
        row = by_id[chunk_id]
        fused.append(row.model_copy(update={"similarity": scores[chunk_id]}))
    return fused


async def hybrid_search(
    openai_client: AsyncOpenAI,
    http: httpx.AsyncClient,
    supabase_url: str,
    supabase_headers: dict[str, str],
    query: str,
    top_k: int = DEFAULT_TOP_K,
    filters: MetadataFilters | None = None,
) -> list[SearchDocumentsResult]:
    """Vector + keyword retrieval fused via RRF (US-021).

    Each side independently retrieves a wider pool (top_k * pool_multiplier,
    clamped to MAX_TOP_K) so RRF has room to surface items one strategy
    ranks low but the other ranks well. The two HTTP calls run concurrently
    via asyncio.gather — total latency is roughly max(vector, keyword) plus
    one OpenAI embedding round trip, not the sum.

    Returned `similarity` is the fused RRF score (small absolute numbers,
    bounded by `len(rankings) / (k + 1)` ≈ 0.033 at k=60). Magnitudes are not
    comparable to vector-only or keyword-only results — only ordering is.
    """
    pool_size = min(max(top_k * HYBRID_POOL_MULTIPLIER, top_k), MAX_TOP_K)

    vector_task = search_documents(
        openai_client=openai_client,
        http=http,
        supabase_url=supabase_url,
        supabase_headers=supabase_headers,
        query=query,
        top_k=pool_size,
        filters=filters,
    )
    keyword_task = keyword_search(
        http=http,
        supabase_url=supabase_url,
        supabase_headers=supabase_headers,
        query=query,
        top_k=pool_size,
        filters=filters,
    )
    vector_results, keyword_results = await asyncio.gather(vector_task, keyword_task)

    return _rrf_fuse([vector_results, keyword_results], top_k=top_k, k=get_rrf_k())
