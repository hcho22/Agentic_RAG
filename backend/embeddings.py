"""OpenAI embeddings with batching + exponential-backoff retry (US-009).

Chunks are embedded at ingestion time. Default model is
text-embedding-3-small (1536 dims, matches the migration); `EMBEDDING_MODEL`
overrides. Batches are capped at 100 inputs per API call (PRD acceptance
criterion) and each batch is retried up to `EMBEDDING_MAX_RETRIES` times
with exponential backoff before propagating the error.

The `client` passed in is the embedder role's client (US-022 — may differ from
the answerer's provider), already wrapped with LangSmith's `wrap_openai`, so
every embeddings call shows up as its own span.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import NamedTuple

from openai import AsyncOpenAI

log = logging.getLogger("agentic_rag.embeddings")

DEFAULT_MODEL = "text-embedding-3-small"
# PRD: "up to 100 per API call". Anything above is clamped to this ceiling.
MAX_BATCH_SIZE = 100
DEFAULT_MAX_RETRIES = 3
DEFAULT_INITIAL_DELAY_S = 1.0


def get_embedding_model() -> str:
    """The embedder role's model (per-call-site selection, ADR-0006/US-022).

    `EMBEDDER_MODEL` is the role-scoped selector; `EMBEDDING_MODEL` is kept as a
    back-compat fallback so existing single-provider deployments need no change.
    The embedder *client* is passed in by callers (the embedder `ProviderConfig`
    in main.py), so this module no longer assumes the answerer client.
    """
    return (
        os.environ.get("EMBEDDER_MODEL")
        or os.environ.get("EMBEDDING_MODEL")
        or DEFAULT_MODEL
    )


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


# ---------------------------------------------------------------------------
# US-027: fail-closed embedder-drift guard (probe-embed + pure comparison).
# ---------------------------------------------------------------------------

# A fixed, content-free string embedded once at startup purely to MEASURE the
# embedder's actual output dimension. The text is irrelevant — only the returned
# vector length is read — so this is provider-agnostic and works for an unknown
# model on any OpenAI-compatible endpoint.
PROBE_TEXT = "agentic-rag embedder dimension probe"


class EmbeddingStamp(NamedTuple):
    """The single-row `embedding_config` corpus stamp (US-026): the embedder
    model name + the vector dim the corpus was indexed under. `dim` equals the
    `chunks.embedding` column dim by the US-026 stamping invariant — pgvector
    rejects a wrong-length insert, so a writer can only ever stamp the column
    dim alongside the one thing the column can't store: the model name."""

    model: str
    dim: int


async def probe_embed_dim(client: AsyncOpenAI) -> int:
    """Embed one fixed string with the configured embedder and return the ACTUAL
    returned vector length (US-027).

    Used by the startup drift guard to measure the live embedder's output dim
    without trusting a hardcoded number, so a drift against the corpus stamp is
    detectable even for an unknown model on an arbitrary compatible endpoint.
    """
    vectors = await embed_texts(client, [PROBE_TEXT])
    if not vectors or not vectors[0]:
        raise RuntimeError(
            "embedder probe returned no vector — the configured embedder "
            f"({get_embedding_model()!r}) is not returning embeddings"
        )
    return len(vectors[0])


def _drift_remedy(
    configured_model: str,
    measured_dim: int,
    stamp: EmbeddingStamp,
    dim_drift: bool,
) -> str:
    """Build the startup error: name the stamped vs configured model/dim and the
    exact re-index remedy (a re-EMBED — UUIDs + grants preserved — plus a column
    migration only when the dimension itself changed)."""
    if dim_drift:
        headline = (
            f"embedder DIMENSION drift: the configured embedder {configured_model!r} "
            f"produces {measured_dim}-dim vectors, but the corpus was indexed at "
            f"{stamp.dim} dims under model {stamp.model!r}. Retrieval is structurally "
            "broken (query and chunk vectors no longer share a space)."
        )
    else:
        headline = (
            f"embedder MODEL drift: the corpus was indexed with {stamp.model!r}, but "
            f"the configured embedder is {configured_model!r} (both {measured_dim}-dim, "
            "so nothing errors — query and chunk vectors are in different 'languages' "
            "and recall silently degrades). This is the dangerous same-dims case."
        )
    remedy = (
        " Refusing to start. Remedy: re-embed the corpus with the configured embedder "
        f"({configured_model!r}). A re-EMBED recomputes every chunk vector in place, "
        "preserving chunk UUIDs and therefore the chunk_acl grants keyed on them — it "
        "is NOT a re-chunk. "
    )
    if dim_drift:
        remedy += (
            f"Because the dimension changes ({stamp.dim} -> {measured_dim}), first "
            "migrate the chunks.embedding vector(N) column to the new dim. "
        )
    remedy += (
        "The bulk re-index (service-role) then overwrites the embedding_config stamp "
        f"to match. To instead keep the existing corpus, revert the embedder to {stamp.model!r}."
    )
    return headline + remedy


def check_embedder_drift(
    configured_model: str,
    measured_dim: int,
    stamp: EmbeddingStamp | None,
) -> None:
    """US-027: fail-closed embedder-drift guard.

    Compares the *running* embedder against the corpus stamp written at index
    time (US-026) and raises `RuntimeError` (refuse to start) on any drift, so
    both failure modes are caught before a single query degrades silently:

      * **different dims** — the probe-embedded vector length no longer matches
        the dim the corpus was indexed under (== the `chunks.embedding` column
        dim, == `stamp.dim`). Retrieval is structurally broken.
      * **same dims, different model** — the dangerous silent case: e.g.
        text-embedding-3-small and text-embedding-ada-002 are both 1536-dim, so
        nothing errors, but query and chunk vectors are in different "languages"
        and recall rots with no signal. Caught by the model-name comparison.

    No-op (returns) when the corpus is empty (`stamp is None` — nothing indexed
    yet, so there is nothing to drift from) or when BOTH the model name and the
    measured dim match the stamp. Pure + I/O-free — the caller does the
    probe-embed and the stamp read — so it is unit-testable without a DB or a
    live embedder, mirroring the `resolve_chat_mode_default` precedent.
    """
    if stamp is None:
        return
    dim_drift = measured_dim != stamp.dim
    model_drift = configured_model != stamp.model
    if not dim_drift and not model_drift:
        return
    raise RuntimeError(_drift_remedy(configured_model, measured_dim, stamp, dim_drift))
