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

US-046: every result also carries `cosine_similarity` — the raw, pre-fusion
vector cosine in `[0,1]` — so the escalation retrieval gate (US-047) can
threshold "weak retrieval" on a calibrated cosine rather than the RRF rank
artifact in `similarity`. Vector rows set it equal to `similarity`; keyword-
only rows leave it `None`; fusion and reranking preserve it while overwriting
`similarity`.
"""

from __future__ import annotations

import asyncio
import os
import re
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

# US-115: bounds for the deterministic per-query fusion weight `predict_alpha`
# returns (the vector-leg weight). The clamp is deliberately narrow: keyword-only
# rows carry `cosine_similarity = None`, so a keyword-heavy top-k would shrink the
# escalation gate's cosine list (US-046/US-047) and could flip an escalation
# decision. Neutral prose maps to the midpoint; identifier-dense queries slide
# toward the lower bound (lexical leg up), never past it.
ALPHA_MIN = 0.3
ALPHA_MAX = 0.7
ALPHA_NEUTRAL = 0.5

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
    # US-046: the raw, pre-fusion vector cosine in a calibrated [0,1], carried
    # separately from `similarity` so the escalation retrieval gate (US-047) can
    # threshold "weak retrieval" on a real cosine instead of the RRF rank
    # artifact. Vector rows set this equal to `similarity` (both are the cosine);
    # keyword-only rows have no embedding, so it is `None`. `hybrid_search`
    # overwrites `similarity` with the RRF score but preserves this field, and a
    # reranker likewise overwrites `similarity` while leaving the cosine intact.
    cosine_similarity: float | None = None
    # US-041: explains *why* the viewer can see the chunk. Owner chunks carry
    # `granting_principal_id=None, granting_principal_display='owner'`. Direct
    # user grants surface the viewer's own email; group grants surface the
    # group name. Both keyword_search and the RRF fuser pass these through
    # unchanged — keyword_search returns `None` for both because the keyword
    # RPC doesn't yet wire them up (its predicate is owner-only via RLS), and
    # the agent-facing tool description glosses these as "for UI badges only".
    granting_principal_id: str | None = None
    granting_principal_display: str | None = None


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


def get_hybrid_fusion_alpha() -> Literal["auto"] | float:
    """Fusion-weight policy from `HYBRID_FUSION_ALPHA` env (US-116).

    Returns the sentinel `"auto"` (the default) to signal per-query adaptive
    weighting via `predict_alpha`, or a fixed vector-leg weight in [0, 1] that
    pins fusion for every query regardless of its shape. `0.5` is the ops escape
    hatch: it reproduces legacy equal-weight RRF byte-for-byte (the seam's
    `(0.5, 0.5)` collapses to `1 / (k + r)`), so operators can revert adaptive
    fusion without a deploy. Validation mirrors `get_rrf_k()`: an unparseable or
    out-of-range value raises rather than silently falling back to a default.
    """
    raw = os.environ.get("HYBRID_FUSION_ALPHA")
    if raw is None or raw.strip() == "":
        return "auto"
    v = raw.strip().lower()
    if v == "auto":
        return "auto"
    try:
        f = float(v)
    except ValueError as e:
        raise ValueError(
            f"HYBRID_FUSION_ALPHA must be 'auto' or a float in [0,1], got {raw!r}"
        ) from e
    if not 0.0 <= f <= 1.0:
        raise ValueError(f"HYBRID_FUSION_ALPHA must be in [0,1], got {f}")
    return f


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
    workspace_id: str | None = None,
) -> list[SearchDocumentsResult]:
    """Embed `query`, call match_chunks RPC under the user's JWT, return rows.

    `supabase_headers` MUST carry the user's access token so PostgREST runs
    the RPC as the `authenticated` role with RLS active. Calling this with
    service-role headers would bypass RLS and leak cross-user chunks.

    `workspace_id` (US-070) is an OPTIONAL ordinary non-security narrowing filter
    forwarded to match_chunks' `filter_workspace_id` param — NOT the trust
    boundary (that is the auth.uid()-resolved membership + owner-OR-ACL predicate
    inside the RPC). When `None` (the authenticated /api/chat path) it is omitted
    entirely, so the call is byte-identical to before; when set (the support-bot
    turn) it narrows to one workspace's documents. Because it is AND-ed inside the
    RPC it can only subtract, never widen.
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
    if workspace_id is not None:
        payload["filter_workspace_id"] = workspace_id
    r = await http.post(
        f"{supabase_url}/rest/v1/rpc/match_chunks",
        headers=supabase_headers,
        json=payload,
    )
    r.raise_for_status()
    # US-046: match_chunks' `similarity` *is* the raw vector cosine, so mirror it
    # onto `cosine_similarity`. This is the canonical source of the cosine that
    # survives RRF fusion and reranking (both of which overwrite `similarity`).
    return [
        SearchDocumentsResult(**row).model_copy(
            update={"cosine_similarity": float(row["similarity"])}
        )
        for row in r.json()
    ]


async def keyword_search(
    http: httpx.AsyncClient,
    supabase_url: str,
    supabase_headers: dict[str, str],
    query: str,
    top_k: int = DEFAULT_TOP_K,
    filters: MetadataFilters | None = None,
    workspace_id: str | None = None,
) -> list[SearchDocumentsResult]:
    """Call keyword_search RPC under the user's JWT, return ranked rows.

    The `similarity` field on each result carries the ts_rank_cd score, which
    is unbounded (unlike cosine similarity in [0,1]) and not directly
    comparable to match_chunks results — US-021 RRF fuses by rank position,
    so the magnitude doesn't need to match. `filters` parity with match_chunks
    (US-017) was added in 20260505121000_keyword_search_filters.sql so hybrid
    queries apply the same metadata filter on both halves.

    `workspace_id` (US-070) mirrors `search_documents`: an optional non-security
    narrowing filter forwarded to keyword_search's `filter_workspace_id`, omitted
    when `None`. Applying it on this leg too keeps hybrid's active-workspace
    narrowing coherent across the fused result (the keyword leg must not re-admit
    a different workspace's row that the vector leg filtered out).
    """
    payload: dict[str, Any] = {
        "query": query,
        "match_count": min(max(top_k, 1), MAX_TOP_K),
    }
    payload.update(_filters_to_rpc_payload(filters))
    if workspace_id is not None:
        payload["filter_workspace_id"] = workspace_id
    r = await http.post(
        f"{supabase_url}/rest/v1/rpc/keyword_search",
        headers=supabase_headers,
        json=payload,
    )
    r.raise_for_status()
    # US-046: keyword rows have no embedding — `similarity` here is the unbounded
    # ts_rank_cd score, not a cosine — so `cosine_similarity` stays at its `None`
    # default. The retrieval gate therefore reads no cosine off a keyword-only
    # hit (and in hybrid the vector side supplies it through fusion).
    return [SearchDocumentsResult(**row) for row in r.json()]


async def keyword_only_search(
    openai_client: AsyncOpenAI,  # noqa: ARG001 — accepted for signature parity
    http: httpx.AsyncClient,
    supabase_url: str,
    supabase_headers: dict[str, str],
    query: str,
    top_k: int = DEFAULT_TOP_K,
    filters: MetadataFilters | None = None,
    workspace_id: str | None = None,
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
        workspace_id=workspace_id,
    )


# US-115: features for `predict_alpha`. A token is "identifier-shaped" when it
# looks like something a user would expect to match a chunk *verbatim* rather
# than semantically — the cases the OR-fallback lexical leg (US-114) is best at:
#   * snake_case / UPPER_SNAKE constants   (WEBHOOK_RETRY_MAX, retry_max)
#   * code-like digit+symbol mixes         (CAT-1234, ERR-4102, v2.1, 5xx)
#   * intra-token camelCase                (getUserById)
# These are matched on tokens with surrounding sentence/quote punctuation
# stripped, but with internal separators (`_`, `-`, `.`, `:`, `/`) preserved.
_TOKEN_SPLIT_RE = re.compile(r"\s+")
_EDGE_PUNCT = "\"'“”‘’.,;:?!()[]{}"
_QUOTED_PHRASE_RE = re.compile(r"[\"“][^\"”]+[\"”]")
_HAS_LETTER_RE = re.compile(r"[A-Za-z]")
_HAS_DIGIT_RE = re.compile(r"[0-9]")
_CAMEL_RE = re.compile(r"[a-z][A-Z]")


def _is_identifier_token(tok: str) -> bool:
    """True when `tok` (edge-punctuation already stripped) looks like a literal
    identifier a user would expect to match verbatim, not paraphrased."""
    if not tok:
        return False
    has_letter = bool(_HAS_LETTER_RE.search(tok))
    has_digit = bool(_HAS_DIGIT_RE.search(tok))
    # snake_case / UPPER_SNAKE (an internal underscore is a strong signal).
    if "_" in tok:
        return True
    # code-like digit+symbol mixes: a digit next to a structural separator, or a
    # letter+digit blend (v2, ERR4102, 5xx). Bare integers ("2024") are left out
    # on purpose — they read as prose numbers, and digit_density already accounts
    # for them in aggregate.
    if has_digit and any(sep in tok for sep in ("-", ".", ":", "/")):
        return True
    if has_letter and has_digit:
        return True
    # intra-token camelCase (getUserById).
    if _CAMEL_RE.search(tok):
        return True
    return False


def predict_alpha(query: str) -> float:
    """Deterministic per-query vector-leg weight in [ALPHA_MIN, ALPHA_MAX].

    A pure feature function (no I/O, no model call — ADR-0003's "deterministic
    control flow, never a model decision"), mirroring aimee's dynamic alpha. It
    reads four cheap features off the raw query — quoted-phrase presence,
    identifier-shaped token count, digit density, and token count — and returns
    the weight the *vector* leg should carry in weighted RRF (the keyword leg
    gets `1 - alpha`).

    Neutral prose carries no lexical signal and returns exactly `ALPHA_NEUTRAL`
    (0.5) — equal weight, i.e. legacy behavior. As the query gets more
    identifier-dense the weight slides down toward `ALPHA_MIN` (0.3), tilting
    fusion toward the lexical leg where exact-token lookups live. It never rises
    above 0.5 today; the symmetric upper clamp `ALPHA_MAX` is a defensive bound.

    The clamp is load-bearing, not cosmetic: keyword-only rows carry no cosine,
    so an unclamped keyword-heavy top-k could starve the escalation gate's cosine
    list (US-046). This story only *defines* the function — nothing calls it yet
    (US-116 wires it), so `hybrid_search` behavior is unchanged.
    """
    raw_tokens = [t for t in _TOKEN_SPLIT_RE.split(query.strip()) if t]
    n_tokens = len(raw_tokens)
    if n_tokens == 0:
        return ALPHA_NEUTRAL

    tokens = [t.strip(_EDGE_PUNCT) for t in raw_tokens]
    n_identifiers = sum(1 for t in tokens if _is_identifier_token(t))
    identifier_ratio = n_identifiers / n_tokens

    non_space = "".join(query.split())
    digit_density = (
        sum(1 for c in non_space if c.isdigit()) / len(non_space) if non_space else 0.0
    )
    has_quoted_phrase = bool(_QUOTED_PHRASE_RE.search(query))

    # Combine into a lexical-preference score in [0, 1]; 0 == neutral prose. Each
    # term is a lexical cue, weighted by how strongly it implies verbatim intent.
    lexical_score = (
        0.60 * identifier_ratio
        + 0.30 * min(1.0, digit_density / 0.15)
        + (0.40 if has_quoted_phrase else 0.0)
    )
    lexical_score = min(1.0, lexical_score)

    # Slide the vector weight down from the neutral midpoint as lexical cues grow.
    alpha = ALPHA_NEUTRAL - (ALPHA_NEUTRAL - ALPHA_MIN) * lexical_score
    return min(ALPHA_MAX, max(ALPHA_MIN, alpha))


def _rrf_fuse(
    rankings: list[list[SearchDocumentsResult]],
    top_k: int,
    k: int,
    weights: tuple[float, ...] | None = None,
) -> list[SearchDocumentsResult]:
    """Reciprocal Rank Fusion over multiple rankings of SearchDocumentsResult.

    For each ranking, item at rank r (1-indexed) contributes `1/(k+r)` to its
    fused score. Duplicate items across rankings have their scores summed —
    this is the deduplication path required by US-021. The returned list
    carries the fused score in `similarity`, sorted descending. Ties broken
    by chunk id for deterministic ordering across runs.

    US-115: an optional per-ranking `weights` tilts the fusion. Ranking `i`
    contributes `2 * w_i / (k + r)`, so equal weights `(0.5, 0.5)` reproduce the
    unweighted `1 / (k + r)` byte-for-byte (2 * 0.5 == 1.0 exactly), and
    `weights=None` takes the legacy expression verbatim. Weights are a fusion-
    ranking artifact only — they never touch per-row `cosine_similarity`, which
    the escalation gate reads raw (US-046). `weights` length must match
    `rankings`.
    """
    if weights is not None and len(weights) != len(rankings):
        raise ValueError(
            f"weights length {len(weights)} must match rankings length {len(rankings)}"
        )
    scores: dict[str, float] = {}
    by_id: dict[str, SearchDocumentsResult] = {}
    # US-046: the fused row overwrites `similarity` with the RRF score, which
    # would otherwise bury the raw cosine. Carry the cosine separately, taking
    # the first non-None value seen — the vector ranking is iterated first, so
    # its cosine wins; a chunk that surfaces only in the keyword ranking has no
    # cosine and stays None.
    cosine_by_id: dict[str, float | None] = {}
    for idx, ranked in enumerate(rankings):
        for rank, item in enumerate(ranked, start=1):
            # weights=None preserves the exact legacy expression; equal weights
            # (0.5, 0.5) collapse to the same value since 2 * 0.5 == 1.0.
            contrib = (
                1.0 / (k + rank)
                if weights is None
                else 2.0 * weights[idx] / (k + rank)
            )
            scores[item.id] = scores.get(item.id, 0.0) + contrib
            # First-seen wins for the row payload — both rankings carry the
            # same content/filename/etc for a given chunk id, so this is
            # really just picking one to surface. Vector ranking is iterated
            # first (caller's responsibility) to keep its filename casing.
            by_id.setdefault(item.id, item)
            if cosine_by_id.get(item.id) is None and item.cosine_similarity is not None:
                cosine_by_id[item.id] = item.cosine_similarity
    ordered_ids = sorted(scores.keys(), key=lambda i: (-scores[i], i))
    fused: list[SearchDocumentsResult] = []
    for chunk_id in ordered_ids[:top_k]:
        row = by_id[chunk_id]
        fused.append(
            row.model_copy(
                update={
                    "similarity": scores[chunk_id],
                    "cosine_similarity": cosine_by_id.get(chunk_id),
                }
            )
        )
    return fused


async def hybrid_search(
    openai_client: AsyncOpenAI,
    http: httpx.AsyncClient,
    supabase_url: str,
    supabase_headers: dict[str, str],
    query: str,
    top_k: int = DEFAULT_TOP_K,
    filters: MetadataFilters | None = None,
    workspace_id: str | None = None,
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

    `workspace_id` (US-070) is forwarded to BOTH legs so the optional
    non-security active-workspace narrowing applies to the whole fused result.
    `None` (the default, /api/chat) is a no-op on both legs.

    US-116: fusion is query-adaptive. The vector-leg weight `alpha` comes from
    `predict_alpha(query)` under the default `HYBRID_FUSION_ALPHA=auto`, tilting
    identifier-dense queries toward the lexical leg and leaving neutral prose at
    the legacy 0.5 midpoint. Weights are `(alpha, 1 - alpha)` — vector ranking
    first, matching the fixed argument order below. A fixed `HYBRID_FUSION_ALPHA`
    float pins every query (`0.5` reproduces legacy equal-weight RRF exactly).
    The [0.3, 0.7] clamp on `predict_alpha` is load-bearing: keyword-only rows
    carry no cosine, so an unclamped keyword-heavy top-k could starve the
    escalation gate's cosine list (US-046 / ADR-0010). The deflection pipeline
    (`escalation.run_deflection_pipeline`) calls this directly and inherits alpha
    by design.
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
        workspace_id=workspace_id,
    )
    keyword_task = keyword_search(
        http=http,
        supabase_url=supabase_url,
        supabase_headers=supabase_headers,
        query=query,
        top_k=pool_size,
        filters=filters,
        workspace_id=workspace_id,
    )
    vector_results, keyword_results = await asyncio.gather(vector_task, keyword_task)

    policy = get_hybrid_fusion_alpha()
    alpha = predict_alpha(query) if policy == "auto" else policy
    return _rrf_fuse(
        [vector_results, keyword_results],
        top_k=top_k,
        k=get_rrf_k(),
        weights=(alpha, 1.0 - alpha),
    )


# -----------------------------------------------------------------------------
# list_documents: filename-level discovery. search_documents matches on chunk
# content only, so content-free queries like "summarize 090725.txt" return
# nothing. This tool lets the agent enumerate the caller's docs by filename /
# title so it can pick the right document_id for spawn_document_agent.
# -----------------------------------------------------------------------------

LIST_DOCUMENTS_MAX_LIMIT = 100
LIST_DOCUMENTS_DEFAULT_LIMIT = 25


class ListDocumentsInput(BaseModel):
    limit: int = Field(
        default=LIST_DOCUMENTS_DEFAULT_LIMIT,
        ge=1,
        le=LIST_DOCUMENTS_MAX_LIMIT,
        description=(
            f"Max documents to return (1..{LIST_DOCUMENTS_MAX_LIMIT}). "
            f"Defaults to {LIST_DOCUMENTS_DEFAULT_LIMIT}, ordered newest first."
        ),
    )


class ListDocumentsItem(BaseModel):
    document_id: str
    filename: str
    title: str | None = None
    chunks_count: int | None = None
    status: str | None = None
    uploaded_at: str | None = None


async def list_documents(
    http: httpx.AsyncClient,
    supabase_url: str,
    supabase_headers: dict[str, str],
    limit: int = LIST_DOCUMENTS_DEFAULT_LIMIT,
) -> list[ListDocumentsItem]:
    """Return the caller's ready documents, newest first, under their JWT.

    Soft-deleted rows are excluded server-side. `status` is included so the
    agent can tell the user when a doc is still ingesting (and not silently
    pretend it's missing).
    """
    r = await http.get(
        f"{supabase_url}/rest/v1/documents",
        params={
            "select": "id,filename,chunks_count,status,uploaded_at,metadata",
            "deleted_at": "is.null",
            "order": "uploaded_at.desc",
            "limit": str(min(max(limit, 1), LIST_DOCUMENTS_MAX_LIMIT)),
        },
        headers=supabase_headers,
    )
    r.raise_for_status()
    out: list[ListDocumentsItem] = []
    for row in r.json():
        meta = row.get("metadata") or {}
        out.append(
            ListDocumentsItem(
                document_id=row["id"],
                filename=row.get("filename") or "",
                title=meta.get("title") if isinstance(meta, dict) else None,
                chunks_count=row.get("chunks_count"),
                status=row.get("status"),
                uploaded_at=row.get("uploaded_at"),
            )
        )
    return out


def list_documents_tool_schema() -> dict[str, Any]:
    """Chat Completions `tools[]` entry for the list_documents tool."""
    return {
        "type": "function",
        "function": {
            "name": "list_documents",
            "description": (
                "List the caller's ingested documents (newest first) with "
                "`document_id`, `filename`, `title`, `chunks_count`, and "
                "`status`. Use this when the user names a file by filename "
                "(e.g. '090725.txt', 'youtube_transcript_0526.txt') or asks "
                "what's been uploaded. Pick the matching `document_id` and "
                "pass it to `spawn_document_agent` for full-document tasks, "
                "or use it to inform a follow-up `search_documents` query."
            ),
            "parameters": ListDocumentsInput.model_json_schema(),
        },
    }
