"""OpenAI embeddings with batching + exponential-backoff retry (US-009).

Chunks are embedded at ingestion time. Default model is
text-embedding-3-small (1536 dims, matches the migration); `EMBEDDING_MODEL`
overrides. Batches are capped at 100 inputs per API call (PRD acceptance
criterion) and each batch is retried up to `EMBEDDING_MAX_RETRIES` times
with exponential backoff before propagating the error.

The `openai_client` passed in is already wrapped with LangSmith's
`wrap_openai`, so every embeddings call shows up as its own span.
"""

from __future__ import annotations

import asyncio
import logging
import os

from openai import AsyncOpenAI

log = logging.getLogger("agentic_rag.embeddings")

DEFAULT_MODEL = "text-embedding-3-small"
# PRD: "up to 100 per API call". Anything above is clamped to this ceiling.
MAX_BATCH_SIZE = 100
DEFAULT_MAX_RETRIES = 3
DEFAULT_INITIAL_DELAY_S = 1.0


def get_embedding_model() -> str:
    return os.environ.get("EMBEDDING_MODEL") or DEFAULT_MODEL


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as e:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from e
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")
    return value


def _get_batch_size() -> int:
    return min(_env_int("EMBEDDING_BATCH_SIZE", MAX_BATCH_SIZE), MAX_BATCH_SIZE)


def _get_max_retries() -> int:
    return _env_int("EMBEDDING_MAX_RETRIES", DEFAULT_MAX_RETRIES)


async def _embed_batch_with_retry(
    client: AsyncOpenAI,
    texts: list[str],
    model: str,
    max_retries: int,
) -> list[list[float]]:
    delay = DEFAULT_INITIAL_DELAY_S
    for attempt in range(1, max_retries + 1):
        try:
            resp = await client.embeddings.create(model=model, input=texts)
            # Defensive: the API returns items in input order, but the `index`
            # field is the authoritative mapping.
            ordered = sorted(resp.data, key=lambda d: d.index)
            return [d.embedding for d in ordered]
        except Exception as e:  # noqa: BLE001 — retry anything transient
            if attempt == max_retries:
                log.exception(
                    "embedding batch failed after %d attempts (size=%d)",
                    attempt, len(texts),
                )
                raise
            log.warning(
                "embedding batch attempt %d/%d failed: %s — retrying in %.1fs",
                attempt, max_retries, e, delay,
            )
            await asyncio.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")  # loop always returns or raises


async def embed_texts(
    client: AsyncOpenAI,
    texts: list[str],
) -> list[list[float]]:
    """Embed a list of strings, batching at MAX_BATCH_SIZE per API call."""
    if not texts:
        return []
    model = get_embedding_model()
    batch_size = _get_batch_size()
    max_retries = _get_max_retries()

    results: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        embeddings = await _embed_batch_with_retry(client, batch, model, max_retries)
        results.extend(embeddings)
    return results


def to_pgvector(values: list[float]) -> str:
    """Format a float list as a pgvector literal string ('[0.1,0.2,...]').

    PostgREST sends JSON; pgvector's input function parses this string shape
    via vector_in, so we keep the embedding as a text field on the insert
    payload and let Postgres coerce it.
    """
    return "[" + ",".join(repr(float(v)) for v in values) + "]"
