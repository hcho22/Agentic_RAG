"""Token-based text chunking for US-008.

Splits text into overlapping chunks using tiktoken so sizing lines up with
downstream OpenAI embedding + completion models. Defaults (500/50) come from
the PRD; CHUNK_SIZE_TOKENS / CHUNK_OVERLAP_TOKENS override via env.

US-018: chunking now respects structural boundaries. Input is expected to be
Markdown-shaped (what docling's `export_to_markdown` produces, or plain text
with blank-line paragraph separators). We greedily pack blocks — where a
block is a heading-plus-paragraph group separated by blank lines — up to the
token budget, so a chunk never starts mid-heading or mid-paragraph. Blocks
larger than the budget fall back to the original sliding-window tokeniser.
"""

from __future__ import annotations

import os
import re

import tiktoken

DEFAULT_CHUNK_SIZE = 500
DEFAULT_CHUNK_OVERLAP = 50
# cl100k_base covers text-embedding-3-* and gpt-4o-*; good enough for a
# size heuristic regardless of which OpenAI model ends up embedding.
_ENCODING_NAME = "cl100k_base"

_encoder = tiktoken.get_encoding(_ENCODING_NAME)

# Markdown ATX heading — matched so we can keep it glued to the paragraph
# that follows rather than letting it hang at the end of a chunk alone.
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s")


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


def _token_count(text: str) -> int:
    return len(_encoder.encode(text))


def _sliding_window(text: str, size: int, overlap: int) -> list[str]:
    """Legacy token-level sliding window — used only for blocks that overflow
    the size budget on their own (e.g. a single heading-less paragraph longer
    than 500 tokens)."""
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


def _split_into_blocks(text: str) -> list[str]:
    """Split on blank-line boundaries, then glue standalone heading lines to
    the next block so a chunk can never end on an orphan heading.
    """
    raw_blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]

    merged: list[str] = []
    pending_heading: str | None = None
    for block in raw_blocks:
        lines = block.splitlines()
        # A block whose *only* non-empty line is a heading stays pending so we
        # can attach it to the next paragraph.
        if len(lines) == 1 and _HEADING_RE.match(lines[0]):
            pending_heading = (
                lines[0] if pending_heading is None else pending_heading + "\n" + lines[0]
            )
            continue
        if pending_heading is not None:
            merged.append(pending_heading + "\n\n" + block)
            pending_heading = None
        else:
            merged.append(block)
    if pending_heading is not None:
        merged.append(pending_heading)
    return merged


def chunk_text(
    text: str,
    size: int | None = None,
    overlap: int | None = None,
) -> list[str]:
    """Return a list of chunks that respect block (heading/paragraph) edges.

    Empty/whitespace-only input returns []. Short input (<= size tokens) is
    returned as a single chunk so we never persist rows with no content.

    Packing rule: blocks are concatenated with `\n\n` until adding the next
    block would exceed `size`. Overlap is implemented by carrying a trailing
    tail of the previous chunk (up to `overlap` tokens, rounded to the nearest
    block boundary) into the next chunk's prefix — so overlap too respects
    structure.
    """
    if not text or not text.strip():
        return []

    if size is None or overlap is None:
        cfg_size, cfg_overlap = get_chunk_config()
        size = cfg_size if size is None else size
        overlap = cfg_overlap if overlap is None else overlap

    if overlap >= size:
        raise ValueError("overlap must be smaller than size")

    if _token_count(text) <= size:
        return [text.strip()]

    blocks = _split_into_blocks(text)
    if not blocks:
        return []

    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0

    def flush() -> None:
        nonlocal current, current_tokens
        if current:
            chunks.append("\n\n".join(current))
            current = []
            current_tokens = 0

    for block in blocks:
        block_tokens = _token_count(block)
        if block_tokens > size:
            # Oversize block: flush, then fall back to the token-level window
            # just for this block. Structure can't help when a single paragraph
            # exceeds the budget.
            flush()
            chunks.extend(_sliding_window(block, size, overlap))
            continue
        # Account for the `\n\n` joiner cost when deciding whether the next
        # block fits. Two tokens is a safe upper bound for the encoder.
        joiner_cost = 2 if current else 0
        if current_tokens + joiner_cost + block_tokens > size:
            flush()
            # Carry overlap: pull trailing blocks of the previous chunk up to
            # `overlap` tokens so topic continuity survives chunk boundaries.
            if chunks and overlap > 0:
                prev_blocks = chunks[-1].split("\n\n")
                tail: list[str] = []
                tail_tokens = 0
                for pb in reversed(prev_blocks):
                    pb_tokens = _token_count(pb)
                    if tail_tokens + pb_tokens > overlap:
                        break
                    tail.insert(0, pb)
                    tail_tokens += pb_tokens
                current = list(tail)
                current_tokens = tail_tokens
        current.append(block)
        current_tokens += block_tokens + joiner_cost

    flush()
    return chunks
