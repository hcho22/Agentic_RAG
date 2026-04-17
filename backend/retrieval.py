"""US-010: search_documents retrieval tool.

Embeds the caller's query with OpenAI, invokes the public.match_chunks RPC
(which runs under the user's JWT so RLS keeps cross-user chunks invisible),
and returns the top-k results that clear the similarity threshold.

The Pydantic input schema is re-used both for runtime validation (when the
backend calls this tool on behalf of the agent in US-011) and as the JSON
Schema handed to OpenAI via `tools[]`, so the two can never drift.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from embeddings import embed_texts, to_pgvector

DEFAULT_TOP_K = 5
MAX_TOP_K = 50
DEFAULT_SIMILARITY_THRESHOLD = 0.3


class SearchDocumentsInput(BaseModel):
    query: str = Field(..., min_length=1, description="Natural-language query to search the user's ingested documents for.")
    top_k: int = Field(
        default=DEFAULT_TOP_K,
        ge=1,
        le=MAX_TOP_K,
        description="Max number of chunks to return (1..50).",
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
                "to the caller's own documents — do not attempt to filter by user."
            ),
            "parameters": SearchDocumentsInput.model_json_schema(),
        },
    }


async def search_documents(
    openai_client: AsyncOpenAI,
    http: httpx.AsyncClient,
    supabase_url: str,
    supabase_headers: dict[str, str],
    query: str,
    top_k: int = DEFAULT_TOP_K,
) -> list[SearchDocumentsResult]:
    """Embed `query`, call match_chunks RPC under the user's JWT, return rows.

    `supabase_headers` MUST carry the user's access token so PostgREST runs
    the RPC as the `authenticated` role with RLS active. Calling this with
    service-role headers would bypass RLS and leak cross-user chunks.
    """
    embeddings = await embed_texts(openai_client, [query])
    if not embeddings:
        return []
    payload = {
        "query_embedding": to_pgvector(embeddings[0]),
        "match_threshold": get_similarity_threshold(),
        "match_count": min(max(top_k, 1), MAX_TOP_K),
    }
    r = await http.post(
        f"{supabase_url}/rest/v1/rpc/match_chunks",
        headers=supabase_headers,
        json=payload,
    )
    r.raise_for_status()
    return [SearchDocumentsResult(**row) for row in r.json()]
