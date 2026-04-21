"""US-016: LLM-extracted document metadata via OpenAI structured outputs.

Runs once per ingestion (after chunking, before the document is marked
`ready`). A failure here is non-fatal: the caller logs a warning and leaves
`documents.metadata` NULL so the document is still searchable — metadata is
an enhancement, not a gate.

The Pydantic schema is pinned by the PRD (US-016 acceptance criterion) so
US-017's metadata-filtered retrieval can rely on a stable shape both at the
SQL layer (match_chunks filter parameters) and in the agent tool schema.
"""

from __future__ import annotations

import logging
import os
from datetime import date

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

log = logging.getLogger("agentic_rag.metadata")

# Keep the sample bounded — 8000 chars is ~2000 tokens for English prose,
# well under every structured-output-capable OpenAI model's context window
# and cheap enough that extraction doesn't meaningfully add to ingest cost.
DEFAULT_SAMPLE_CHARS = 8000


class DocumentMetadata(BaseModel):
    """Structured metadata extracted from a document.

    All fields are required (OpenAI strict structured outputs disallow
    optional keys). Empty string / empty list / None are the "no signal"
    sentinels — the extractor is instructed to use them instead of guessing.
    """

    title: str = Field(
        ...,
        description=(
            "The document's title. Empty string if the document has no clear "
            "title or one cannot be determined from the text."
        ),
    )
    authors: list[str] = Field(
        ...,
        description=(
            "List of author names as they appear in the document. Empty list "
            "if no authors are named."
        ),
    )
    topics: list[str] = Field(
        ...,
        description=(
            "2-6 short lowercase topic tags describing the subject matter "
            "(e.g. 'machine learning', 'finance', 'biology'). Empty list if "
            "the content is too generic to categorize."
        ),
    )
    published_date: date | None = Field(
        ...,
        description=(
            "Publication date as ISO-8601 YYYY-MM-DD if explicitly stated in "
            "the document. Null if no date is stated — do not guess."
        ),
    )
    document_type: str = Field(
        ...,
        description=(
            "One of 'paper', 'article', 'report', 'book', 'notes', 'email', "
            "'documentation', 'other'."
        ),
    )


DEFAULT_METADATA_MODEL = "gpt-4o-mini"

SYSTEM_PROMPT = (
    "You extract structured metadata from documents. Read the document text "
    "and fill in every field of the schema. Rules:\n"
    "- title: short, just the title (not the first sentence). Use \"\" if "
    "there's no clear title.\n"
    "- authors: only names explicitly presented as authors. [] if none.\n"
    "- topics: 2-6 short lowercase tags. [] only if you truly cannot pick any.\n"
    "- published_date: ISO YYYY-MM-DD only if the document states a "
    "publication date. null if it doesn't — do not infer from filename.\n"
    "- document_type: one of 'paper', 'article', 'report', 'book', 'notes', "
    "'email', 'documentation', 'other'.\n"
    "Never invent facts. Prefer empty/null to a guess."
)


def get_metadata_model() -> str:
    """Model used for metadata extraction.

    Falls back to OPENAI_MODEL so single-model setups keep working; override
    with METADATA_MODEL when you want a cheaper model just for extraction.
    """
    return (
        os.environ.get("METADATA_MODEL")
        or os.environ.get("OPENAI_MODEL")
        or DEFAULT_METADATA_MODEL
    )


def _sample_text(text: str, max_chars: int = DEFAULT_SAMPLE_CHARS) -> str:
    """Head+tail sample so title/authors (usually near the top) and
    conclusion/date lines (often near the bottom) are both visible."""
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return f"{text[:half]}\n\n[...truncated...]\n\n{text[-half:]}"


async def extract_document_metadata(
    client: AsyncOpenAI,
    text: str,
    filename: str,
) -> DocumentMetadata | None:
    """Extract metadata for a document. Returns None on any failure.

    Callers treat None as non-fatal: log a warning, leave `metadata` NULL on
    the row, and continue ingestion (US-016 acceptance: "Extraction failures
    do not block ingestion").
    """
    if not text or not text.strip():
        return None
    sample = _sample_text(text)
    try:
        completion = await client.chat.completions.parse(
            model=get_metadata_model(),
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Filename: {filename}\n\n{sample}",
                },
            ],
            response_format=DocumentMetadata,
        )
    except Exception as e:  # noqa: BLE001 — any SDK/API failure is non-fatal
        log.warning("metadata extraction failed for %s: %s", filename, e)
        return None
    if not completion.choices:
        log.warning("metadata extraction returned no choices for %s", filename)
        return None
    message = completion.choices[0].message
    if getattr(message, "refusal", None):
        log.warning(
            "metadata extraction refused for %s: %s", filename, message.refusal
        )
        return None
    parsed = getattr(message, "parsed", None)
    if parsed is None:
        log.warning("metadata extraction returned no parsed payload for %s", filename)
        return None
    return parsed
