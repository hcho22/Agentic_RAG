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
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel, Field, model_validator

from embeddings import embed_texts, to_pgvector

DEFAULT_TOP_K = 5
MAX_TOP_K = 50
DEFAULT_SIMILARITY_THRESHOLD = 0.3


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
