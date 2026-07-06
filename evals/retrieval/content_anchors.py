"""US-107: content-anchor gold-label resolver (fail-loud on zero-resolve).

A gold label is authored as an **answer-bearing text span** — a quoted string
that actually appears in the corpus — not as a `{filename_slug}:{chunk_index}`
chunk-index primitive. At eval time the resolver maps each anchor to whichever
chunk `stable_id`(s) currently *contain* its text, so a buyer can sweep
`chunk_size` / overlap / docling with **zero re-labeling**: the same anchor
re-resolves to the new chunk indices after a re-seed.

Design (why this shape):

- **Exact substring, whitespace-normalized, never fuzzy.** An anchor resolves
  to a chunk iff the anchor's text is a substring of the chunk's text after
  collapsing runs of whitespace to a single space (so a span that wraps across
  a Markdown line break in the corpus still matches an anchor authored on one
  line). Case and punctuation are preserved — normalization tolerates *layout*,
  never a *content edit*. Editing the document so the quoted words no longer
  appear breaks the anchor by design (zero-resolve → hard error); the resolver
  does not fuzzy-match around the edit.

- **A straddling span resolves to BOTH chunks.** `backend.chunking.chunk_text`
  carries whole trailing blocks of the previous chunk into the next as overlap,
  so a paragraph in the overlap region is a verbatim substring of both adjacent
  chunks — a plain substring match returns both stable_ids with no special
  straddle logic. The recall scorer's existing multi-gold partial credit
  (`recall_at_k` divides by `|gold|`) handles the two-chunk gold.

- **Zero-resolve is a HARD ERROR (the load-bearing assertion).** An anchor that
  matches no current chunk raises `ZeroResolveError` naming the question id and
  the offending anchor text, and fails the run — never a silent `recall=0`.

- **Optional stable-document scope.** An anchor may be `{text, doc}` where `doc`
  is a `filename_slug` (the stable document identity that survives re-chunking);
  resolution is then restricted to that document's chunks. The scope IS the
  "locator" — resolution intentionally returns *all* matching chunks within it
  (multi-gold), so a per-occurrence locator is unnecessary. A bare string is the
  shorthand for an unscoped anchor.

The `{filename_slug}:{chunk_index}` stable_id survives only as the *resolved
internal* representation the scorer consumes; it is never authored.
"""

from __future__ import annotations

import re
from typing import Any

import asyncpg

# Collapse every run of whitespace (spaces, tabs, newlines) to a single space.
_WHITESPACE_RE = re.compile(r"\s+")


class ZeroResolveError(RuntimeError):
    """An anchor matched no current chunk. Fails the run — never a silent 0.

    Carries the question id and the offending anchor text so the failure names
    exactly which label to fix (a stale span after a content edit, a typo, or a
    mis-scoped `doc`).
    """

    def __init__(
        self, question_id: str, anchor_text: str, doc: str | None = None
    ) -> None:
        self.question_id = question_id
        self.anchor_text = anchor_text
        self.doc = doc
        scope = f" (doc={doc!r})" if doc else ""
        super().__init__(
            f"{question_id}: content anchor resolved to ZERO chunks{scope}: "
            f"{anchor_text!r}. The quoted span appears in no current chunk — "
            f"fix the anchor text or its `doc` scope (the resolver does not "
            f"fuzzy-match around a content edit)."
        )


def normalize_for_match(text: str) -> str:
    """Collapse whitespace runs to a single space and strip the ends.

    Applied identically to anchor text and chunk content so an anchor authored
    on one line still matches a span the corpus wraps across lines. Case and
    punctuation are preserved on purpose — this tolerates *layout*, not a
    *content edit*.
    """
    return _WHITESPACE_RE.sub(" ", text).strip()


def _parse_anchor(question_id: str, entry: Any) -> tuple[str, str | None]:
    """Normalize one `gold_anchors` entry to `(text, doc)`.

    Accepts a bare string (unscoped) or a mapping `{text, doc?}`. Raises a
    clear `RuntimeError` on a blank/missing span or an unknown key.
    """
    if isinstance(entry, str):
        text: str = entry
        doc: str | None = None
    elif isinstance(entry, dict):
        unknown = set(entry) - {"text", "doc"}
        if unknown:
            raise RuntimeError(
                f"{question_id}: gold_anchors entry has unknown key(s) "
                f"{sorted(unknown)}; allowed keys are 'text' and 'doc'"
            )
        raw_text = entry.get("text")
        if not isinstance(raw_text, str):
            raise RuntimeError(
                f"{question_id}: gold_anchors entry is missing a string `text`: {entry!r}"
            )
        text = raw_text
        raw_doc = entry.get("doc")
        if raw_doc is not None and (not isinstance(raw_doc, str) or not raw_doc.strip()):
            raise RuntimeError(
                f"{question_id}: gold_anchors `doc` must be a non-empty string "
                f"(a filename_slug), got {raw_doc!r}"
            )
        doc = raw_doc.strip() if isinstance(raw_doc, str) else None
    else:
        raise RuntimeError(
            f"{question_id}: each gold_anchors entry must be a string or a "
            f"{{text, doc?}} mapping, got {type(entry).__name__}: {entry!r}"
        )
    if not normalize_for_match(text):
        raise RuntimeError(
            f"{question_id}: gold_anchors entry has empty/blank text: {entry!r}"
        )
    return text, doc


def parse_gold_anchors(question: dict[str, Any]) -> list[tuple[str, str | None]]:
    """Structural validation of a question's `gold_anchors` (no DB / no corpus).

    Returns the parsed `(text, doc)` anchors; raises a clear `RuntimeError` if
    the field is missing, not a non-empty list, or holds a malformed entry. This
    is the DB-free half of the contract — `load_questions` can validate the
    golden set's shape before any chunk content is fetched.
    """
    qid = question.get("id", "<no id>")
    anchors = question.get("gold_anchors")
    if not isinstance(anchors, list) or not anchors:
        raise RuntimeError(
            f"{qid}: gold_anchors must be a non-empty list of content anchors "
            f"(answer-bearing spans), got {anchors!r}"
        )
    return [_parse_anchor(str(qid), entry) for entry in anchors]


def _doc_of(stable_id: str) -> str:
    """The `filename_slug` half of a `{filename_slug}:{chunk_index}` stable_id."""
    return stable_id.rsplit(":", 1)[0]


class ContentAnchorResolver:
    """Resolves content anchors against a fixed snapshot of chunk contents.

    Normalizes every chunk's content once at construction so resolving a whole
    golden set is O(anchors x chunks) substring checks over the pre-normalized
    text (the corpus is tiny; this stays linear and predictable even on a
    buyer's larger set).
    """

    def __init__(self, chunk_contents: dict[str, str]) -> None:
        # stable_id -> normalized content
        self._normalized: dict[str, str] = {
            sid: normalize_for_match(content) for sid, content in chunk_contents.items()
        }

    def resolve_anchor(self, question_id: str, text: str, doc: str | None) -> list[str]:
        """All stable_ids whose content contains `text` (within `doc` scope).

        Raises `ZeroResolveError` (naming the question + anchor) if none match.
        """
        needle = normalize_for_match(text)
        hits = [
            sid
            for sid, content in self._normalized.items()
            if (doc is None or _doc_of(sid) == doc) and needle in content
        ]
        if not hits:
            raise ZeroResolveError(question_id, text, doc)
        return sorted(hits)

    def resolve_question(self, question: dict[str, Any]) -> list[str]:
        """Resolve every anchor on `question` to the union of matching stable_ids.

        Each anchor must resolve to >= 1 chunk (else `ZeroResolveError`), so the
        union is always non-empty — satisfying every downstream consumer's
        `gold_stable_ids` non-empty-list contract.
        """
        qid = str(question.get("id", "<no id>"))
        anchors = parse_gold_anchors(question)
        gold: set[str] = set()
        for text, doc in anchors:
            gold.update(self.resolve_anchor(qid, text, doc))
        return sorted(gold)

    def resolve_all(self, questions: list[dict[str, Any]]) -> None:
        """Resolve every question in place, injecting `gold_stable_ids`.

        Mutates each question dict so the rest of the runner (viewer
        construction, recall scoring, E6) reads `gold_stable_ids` exactly as it
        did when the field was authored — the anchor layer is invisible past
        this point. Any zero-resolve fails the whole run loudly.
        """
        for question in questions:
            question["gold_stable_ids"] = self.resolve_question(question)


async def fetch_chunk_contents(database_url: str) -> dict[str, str]:
    """Return `{chunk.stable_id: chunk.content}` for all corpus chunks.

    The resolver's source of truth — the chunk text as actually seeded — so the
    anchors resolve against the live chunking, not a re-derivation.
    """
    conn = await asyncpg.connect(database_url)
    try:
        rows = await conn.fetch(
            "select stable_id, content from public.chunks where stable_id is not null"
        )
    finally:
        await conn.close()
    return {r["stable_id"]: r["content"] for r in rows}
