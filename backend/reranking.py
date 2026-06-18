"""US-022: cross-encoder / LLM reranking on top of hybrid retrieval.

Hybrid search (US-021) is fast but coarse — RRF fuses rank positions, so it
can't tell whether the #2 keyword match is actually more relevant than the
#1 vector match for the user's specific phrasing. This module bolts a
stronger relevance signal on top: a hosted cross-encoder (Cohere, Voyage)
or an LLM-as-reranker reorders the top-N hybrid candidates and trims to the
final top-k handed to the agent.

`RERANKER=none|cohere|voyage|llm` selects the backend (default `none` so
this is opt-in — no extra latency, no extra API key requirement). A 2s
latency warning surfaces regressions in production logs without paging.
Reranker errors are non-fatal: we log and fall back to the input ordering
rather than blocking the user's whole turn over a refinement step.
"""

from __future__ import annotations

import json
import logging
import os
import time
from abc import ABC, abstractmethod
from typing import Literal

import httpx
from openai import AsyncOpenAI

from retrieval import SearchDocumentsResult

log = logging.getLogger("agentic_rag.backend.reranking")

RerankerName = Literal["cohere", "voyage", "llm", "none"]

# Per PRD: reranker takes top 20 from hybrid, returns top 5 to the agent.
DEFAULT_RERANK_INPUT_K = 20

# Latency over this threshold logs a warning (PRD acceptance criterion).
RERANK_LATENCY_WARN_SECONDS = 2.0

DEFAULT_COHERE_MODEL = "rerank-english-v3.0"
DEFAULT_VOYAGE_MODEL = "rerank-2"


def get_reranker_name() -> RerankerName:
    """`RERANKER` env: `none` (default) | `cohere` | `voyage` | `llm`."""
    raw = (os.environ.get("RERANKER") or "none").strip().lower()
    if raw not in ("cohere", "voyage", "llm", "none"):
        raise ValueError(
            f"RERANKER must be one of cohere|voyage|llm|none, got {raw!r}"
        )
    return raw  # type: ignore[return-value]


def get_rerank_input_k() -> int:
    """Candidate pool size handed to the reranker (`RERANK_INPUT_K`, default 20)."""
    raw = os.environ.get("RERANK_INPUT_K")
    if raw is None or raw == "":
        return DEFAULT_RERANK_INPUT_K
    try:
        v = int(raw)
    except ValueError as e:
        raise ValueError(f"RERANK_INPUT_K must be an int, got {raw!r}") from e
    if v < 1:
        raise ValueError(f"RERANK_INPUT_K must be >= 1, got {v}")
    return v


class Reranker(ABC):
    """Reranks candidate chunks against a query.

    Implementations MUST tolerate empty `candidates` (return `[]`) and MUST
    NOT mutate the input list. Returned rows carry the reranker's relevance
    score in `similarity` — magnitudes differ across backends (Cohere/Voyage
    typically [0,1], LLM-as-reranker is whatever the prompt asks for), so
    only ordering is comparable across reranker choices.
    """

    name: str

    @abstractmethod
    async def rerank(
        self,
        query: str,
        candidates: list[SearchDocumentsResult],
        top_k: int,
    ) -> list[SearchDocumentsResult]:
        ...


class NullReranker(Reranker):
    """Pass-through. Used when RERANKER=none so callers can always invoke a
    reranker uniformly without `if reranker_name == 'none'` branches."""

    name = "none"

    async def rerank(
        self,
        query: str,
        candidates: list[SearchDocumentsResult],
        top_k: int,
    ) -> list[SearchDocumentsResult]:
        return candidates[:top_k]


class CohereReranker(Reranker):
    """Cohere Rerank API (v2). Returns top_n results sorted by relevance."""

    name = "cohere"
    ENDPOINT = "https://api.cohere.com/v2/rerank"

    def __init__(self, http: httpx.AsyncClient, api_key: str, model: str) -> None:
        self.http = http
        self.api_key = api_key
        self.model = model

    async def rerank(
        self,
        query: str,
        candidates: list[SearchDocumentsResult],
        top_k: int,
    ) -> list[SearchDocumentsResult]:
        if not candidates:
            return []
        r = await self.http.post(
            self.ENDPOINT,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "query": query,
                "documents": [c.content for c in candidates],
                "top_n": min(top_k, len(candidates)),
            },
        )
        r.raise_for_status()
        body = r.json()
        out: list[SearchDocumentsResult] = []
        for item in body.get("results", []):
            idx = item.get("index")
            score = item.get("relevance_score")
            if not isinstance(idx, int) or not 0 <= idx < len(candidates):
                continue
            row = candidates[idx]
            out.append(row.model_copy(update={"similarity": float(score)}))
        return out


class VoyageReranker(Reranker):
    """Voyage AI Rerank API. Same pattern as Cohere — different endpoint /
    field names. Voyage uses `top_k` (not `top_n`) and returns under `data`."""

    name = "voyage"
    ENDPOINT = "https://api.voyageai.com/v1/rerank"

    def __init__(self, http: httpx.AsyncClient, api_key: str, model: str) -> None:
        self.http = http
        self.api_key = api_key
        self.model = model

    async def rerank(
        self,
        query: str,
        candidates: list[SearchDocumentsResult],
        top_k: int,
    ) -> list[SearchDocumentsResult]:
        if not candidates:
            return []
        r = await self.http.post(
            self.ENDPOINT,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "query": query,
                "documents": [c.content for c in candidates],
                "top_k": min(top_k, len(candidates)),
            },
        )
        r.raise_for_status()
        body = r.json()
        out: list[SearchDocumentsResult] = []
        for item in body.get("data", []):
            idx = item.get("index")
            score = item.get("relevance_score")
            if not isinstance(idx, int) or not 0 <= idx < len(candidates):
                continue
            row = candidates[idx]
            out.append(row.model_copy(update={"similarity": float(score)}))
        return out


class LlmReranker(Reranker):
    """LLM-as-reranker. Cheaper to operate than Cohere/Voyage when the chat
    model is already on the hot path, but slower and noisier on score
    calibration. Useful as a no-extra-vendor fallback."""

    name = "llm"

    SYSTEM_PROMPT = (
        "You score document chunks by their relevance to a user query. For each "
        "chunk, return a relevance score from 0 (irrelevant) to 1 (highly "
        "relevant). Return ONLY valid JSON in the form "
        '{"results": [{"index": <int>, "score": <float>}, ...]} '
        "with one entry per chunk in the input. Do not include any prose."
    )

    def __init__(self, openai_client: AsyncOpenAI, model: str) -> None:
        self.openai_client = openai_client
        self.model = model

    async def rerank(
        self,
        query: str,
        candidates: list[SearchDocumentsResult],
        top_k: int,
    ) -> list[SearchDocumentsResult]:
        if not candidates:
            return []
        chunks_text = "\n\n".join(
            f"[{i}] {c.content}" for i, c in enumerate(candidates)
        )
        user_msg = (
            f"Query: {query}\n\nChunks:\n{chunks_text}\n\n"
            f"Return a JSON object as specified."
        )
        resp = await self.openai_client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        content = resp.choices[0].message.content or "{}"
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            log.warning(
                "llm_reranker.invalid_json content=%r — falling back to input order",
                content[:200],
            )
            return candidates[:top_k]
        items = parsed.get("results") if isinstance(parsed, dict) else parsed
        if not isinstance(items, list):
            log.warning(
                "llm_reranker.unexpected_shape parsed=%r — falling back to input order",
                parsed,
            )
            return candidates[:top_k]

        scored: list[tuple[int, float]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            try:
                scored.append((int(it["index"]), float(it["score"])))
            except (KeyError, TypeError, ValueError):
                continue
        # Sort by score desc, dedupe indices (model occasionally repeats).
        scored.sort(key=lambda p: -p[1])
        seen: set[int] = set()
        out: list[SearchDocumentsResult] = []
        for idx, score in scored:
            if idx in seen or not 0 <= idx < len(candidates):
                continue
            seen.add(idx)
            row = candidates[idx]
            out.append(row.model_copy(update={"similarity": score}))
            if len(out) >= top_k:
                break
        # Fallback: if the model dropped chunks (under-counted), top up from
        # input order so we always return min(top_k, len(candidates)) rows.
        if len(out) < top_k:
            for i, c in enumerate(candidates):
                if i in seen:
                    continue
                out.append(c)
                seen.add(i)
                if len(out) >= top_k:
                    break
        return out


def build_reranker(
    name: RerankerName,
    *,
    http: httpx.AsyncClient,
    openai_client: AsyncOpenAI,
) -> Reranker:
    """Factory matching `RERANKER` env to a concrete Reranker.

    Hosted backends raise on missing API keys at build time so configuration
    mistakes surface immediately rather than at first request.
    """
    if name == "none":
        return NullReranker()
    if name == "cohere":
        api_key = os.environ.get("COHERE_API_KEY")
        if not api_key:
            raise ValueError("RERANKER=cohere requires COHERE_API_KEY")
        model = os.environ.get("COHERE_RERANK_MODEL", DEFAULT_COHERE_MODEL)
        return CohereReranker(http=http, api_key=api_key, model=model)
    if name == "voyage":
        api_key = os.environ.get("VOYAGE_API_KEY")
        if not api_key:
            raise ValueError("RERANKER=voyage requires VOYAGE_API_KEY")
        model = os.environ.get("VOYAGE_RERANK_MODEL", DEFAULT_VOYAGE_MODEL)
        return VoyageReranker(http=http, api_key=api_key, model=model)
    if name == "llm":
        # OPENAI_RERANK_MODEL lets ops pick a cheaper/faster model than the
        # chat model — reranking only needs ordering, not generation quality.
        # US-023: this selects the *model* only. `openai_client` is the shared
        # answerer client passed by the caller — the llm reranker never opens
        # its own client and has no per-call base_url (one chat host per
        # deployment for all text generation; ADR-0006). The Cohere/Voyage
        # branches above are a SEPARATE provider axis, not the model surface.
        model = (
            os.environ.get("OPENAI_RERANK_MODEL")
            or os.environ.get("OPENAI_MODEL")
            or "gpt-4o-mini"
        )
        return LlmReranker(openai_client=openai_client, model=model)
    raise ValueError(f"unhandled reranker name: {name}")  # pragma: no cover


async def rerank_with_timing(
    reranker: Reranker,
    query: str,
    candidates: list[SearchDocumentsResult],
    top_k: int,
) -> list[SearchDocumentsResult]:
    """Run a reranker, log latency, warn if over the 2s threshold.

    Failures are non-fatal: we log and fall back to the input ordering.
    Hard-failing on rerank would block the user's whole turn over a
    refinement step that, by design, only changes the order of an already-
    relevant candidate set.
    """
    start = time.perf_counter()
    try:
        out = await reranker.rerank(query, candidates, top_k)
    except Exception as e:  # noqa: BLE001 — fall back rather than fail the turn
        elapsed = time.perf_counter() - start
        log.warning(
            "reranker.error name=%s elapsed_s=%.3f error=%r — falling back to input order",
            reranker.name,
            elapsed,
            e,
        )
        return candidates[:top_k]
    elapsed = time.perf_counter() - start
    if elapsed > RERANK_LATENCY_WARN_SECONDS:
        log.warning(
            "reranker.slow name=%s elapsed_s=%.3f threshold_s=%.1f input=%d output=%d",
            reranker.name,
            elapsed,
            RERANK_LATENCY_WARN_SECONDS,
            len(candidates),
            len(out),
        )
    else:
        log.info(
            "reranker.ok name=%s elapsed_s=%.3f input=%d output=%d",
            reranker.name,
            elapsed,
            len(candidates),
            len(out),
        )
    return out
