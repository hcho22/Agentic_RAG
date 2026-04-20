"""Token-based text chunking for US-008.

Splits text into overlapping chunks using tiktoken so sizing lines up with
downstream OpenAI embedding + completion models. Defaults (500/50) come from
the PRD; CHUNK_SIZE_TOKENS / CHUNK_OVERLAP_TOKENS override via env.
"""

from __future__ import annotations

import os

import tiktoken

DEFAULT_CHUNK_SIZE = 500
DEFAULT_CHUNK_OVERLAP = 50
# cl100k_base covers text-embedding-3-* and gpt-4o-*; good enough for a
# size heuristic regardless of which OpenAI model ends up embedding.
_ENCODING_NAME = "cl100k_base"

_encoder = tiktoken.get_encoding(_ENCODING_NAME)


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


def get_chunk_config() -> tuple[int, int]:
    size = _env_int("CHUNK_SIZE_TOKENS", DEFAULT_CHUNK_SIZE)
    overlap = _env_int("CHUNK_OVERLAP_TOKENS", DEFAULT_CHUNK_OVERLAP)
    if overlap >= size:
        raise ValueError(
            f"CHUNK_OVERLAP_TOKENS ({overlap}) must be smaller than "
            f"CHUNK_SIZE_TOKENS ({size})"
        )
    return size, overlap


def chunk_text(
    text: str,
    size: int | None = None,
    overlap: int | None = None,
) -> list[str]:
    """Return a list of overlapping text chunks.

    Empty/whitespace-only input returns []. Short input (<= size tokens) is
    returned as a single chunk so we never persist rows with no content.
    """
    if not text or not text.strip():
        return []

    if size is None or overlap is None:
        cfg_size, cfg_overlap = get_chunk_config()
        size = cfg_size if size is None else size
        overlap = cfg_overlap if overlap is None else overlap

    if overlap >= size:
        raise ValueError("overlap must be smaller than size")

    tokens = _encoder.encode(text)
    if len(tokens) <= size:
        return [text]

    step = size - overlap
    chunks: list[str] = []
    start = 0
    while start < len(tokens):
        window = tokens[start : start + size]
        chunks.append(_encoder.decode(window))
        if start + size >= len(tokens):
            break
        start += step
    return chunks
