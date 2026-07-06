"""US-107 validation test: content-anchor gold-label resolver.

Two layers, both offline (no DB, no network, no backend-heavy import):

  * UNIT — the pure resolver in `evals.retrieval.content_anchors`: whitespace
    normalization, single-chunk resolution, a straddling span resolving to BOTH
    overlapping chunks, the load-bearing zero-resolve HARD ERROR (naming the
    question id + anchor text), a content edit breaking the anchor, stable-
    document scoping, multi-anchor union, and the `gold_anchors` structural
    validator's rejections.

  * CORPUS — the PRD US-107 Validation Test end to end, using the REAL
    production chunker (`backend.chunking.chunk_text`) over the shipped 7-doc
    corpus (no DB): seed at 500/50 and resolve the shipped `retrieval_gold.yaml`
    (every anchor resolves; the q07 straddle yields two stable_ids); RE-SEED at
    a different chunk_size and re-resolve the UNCHANGED golden set (zero
    re-labeling); and an anchor whose text appears in no chunk fails loud. Skips
    cleanly if tiktoken / the corpus is unavailable.

Run:
    python -m evals.retrieval.test_content_anchors
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import yaml

from evals.retrieval.content_anchors import (
    ContentAnchorResolver,
    ZeroResolveError,
    normalize_for_match,
    parse_gold_anchors,
)

ROOT = Path(__file__).resolve().parents[2]
RETRIEVAL_GOLD = Path(__file__).resolve().parent / "retrieval_gold.yaml"
CORPUS_DIR = ROOT / "db_seed" / "corpus"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _expect_zero_resolve(resolver: ContentAnchorResolver, qid: str, text: str,
                         doc: str | None = None) -> ZeroResolveError:
    """Assert resolving (qid, text, doc) raises ZeroResolveError; return it."""
    try:
        resolver.resolve_anchor(qid, text, doc)
    except ZeroResolveError as e:
        return e
    raise AssertionError(f"expected ZeroResolveError for {qid}: {text!r}")


def _expect_parse_error(question: dict, needle: str) -> None:
    """Assert parse_gold_anchors rejects `question` with a message containing needle."""
    try:
        parse_gold_anchors(question)
    except RuntimeError as e:
        assert needle in str(e), f"message {str(e)!r} missing {needle!r}"
        return
    raise AssertionError(f"expected RuntimeError containing {needle!r} for {question!r}")


# ---------------------------------------------------------------------------
# UNIT layer — pure resolver
# ---------------------------------------------------------------------------


def test_normalize_collapses_whitespace_preserves_content() -> None:
    assert normalize_for_match("a   b\n\tc") == "a b c"
    assert normalize_for_match("  leading and trailing  ") == "leading and trailing"
    # Case and punctuation are preserved — this tolerates layout, not edits.
    assert normalize_for_match("The $14.95 Fee — 5–20 lbs.") == "The $14.95 Fee — 5–20 lbs."
    print("ok: normalize_for_match collapses whitespace, preserves case/punctuation")


def test_single_chunk_resolves_to_one() -> None:
    resolver = ContentAnchorResolver(
        {
            "doc:0": "The standard shipping window is 3-5 business days.",
            "doc:1": "Expedited shipping is 1-2 business days.",
        }
    )
    assert resolver.resolve_anchor("q", "standard shipping window is 3-5 business days", None) == [
        "doc:0"
    ]
    print("ok: a span unique to one chunk resolves to exactly that stable_id")


def test_straddling_span_resolves_to_both() -> None:
    # The chunker carries a trailing block into the next chunk as overlap, so
    # the overlap paragraph is a verbatim substring of BOTH adjacent chunks.
    overlap = "Shared overlap paragraph carried across the boundary."
    resolver = ContentAnchorResolver(
        {
            "doc:0": f"Alpha content unique to zero.\n\n{overlap}",
            "doc:1": f"{overlap}\n\nBeta content unique to one.",
        }
    )
    assert resolver.resolve_anchor("q", "Shared overlap paragraph carried", None) == [
        "doc:0",
        "doc:1",
    ]
    # Each unique span still resolves to just its own chunk.
    assert resolver.resolve_anchor("q", "Alpha content unique to zero", None) == ["doc:0"]
    assert resolver.resolve_anchor("q", "Beta content unique to one", None) == ["doc:1"]
    print("ok: a straddling (overlap-region) span resolves to BOTH stable_ids")


def test_zero_resolve_is_hard_error_naming_qid_and_text() -> None:
    resolver = ContentAnchorResolver({"doc:0": "some corpus text"})
    err = _expect_zero_resolve(resolver, "qBAD", "text that appears nowhere")
    msg = str(err)
    assert "qBAD" in msg, msg
    assert "text that appears nowhere" in msg, msg
    assert err.question_id == "qBAD" and err.anchor_text == "text that appears nowhere"
    print("ok: a zero-resolve anchor raises ZeroResolveError naming the qid + anchor text")


def test_whitespace_normalized_match_across_line_wrap() -> None:
    # Corpus wraps the span across a newline + double space; the anchor is on
    # one line. Normalization makes them match — layout, not a content edit.
    resolver = ContentAnchorResolver(
        {"doc:0": "the answer is\n   forty two and change"}
    )
    assert resolver.resolve_anchor("q", "the answer is forty two", None) == ["doc:0"]
    print("ok: an anchor authored on one line matches a span the corpus wraps")


def test_content_edit_breaks_anchor_no_fuzzy_match() -> None:
    original = ContentAnchorResolver({"doc:0": "the answer is 42 exactly"})
    assert original.resolve_anchor("q", "the answer is 42", None) == ["doc:0"]
    # Edit the document content: 42 -> 43. The anchor must fail loud, NOT
    # fuzzy-match around the edit.
    edited = ContentAnchorResolver({"doc:0": "the answer is 43 exactly"})
    _expect_zero_resolve(edited, "q", "the answer is 42")
    print("ok: editing the source content breaks the anchor (no fuzzy match) — fail loud")


def test_doc_scope_narrows_resolution() -> None:
    resolver = ContentAnchorResolver(
        {
            "shipping-faq:0": "The magic phrase lives here.",
            "warranty-terms:0": "The magic phrase lives here too.",
        }
    )
    # Unscoped: the phrase is in both documents.
    assert resolver.resolve_anchor("q", "The magic phrase lives here", None) == [
        "shipping-faq:0",
        "warranty-terms:0",
    ]
    # Scoped to one document: only that document's chunk.
    assert resolver.resolve_anchor(
        "q", "The magic phrase lives here", "shipping-faq"
    ) == ["shipping-faq:0"]
    # Scoped to a document that does not contain it: zero-resolve, fail loud.
    err = _expect_zero_resolve(resolver, "q", "The magic phrase lives here", "loyalty-program")
    assert "loyalty-program" in str(err)
    print("ok: `doc` scope narrows resolution and a mis-scoped anchor fails loud")


def test_multi_anchor_question_unions_and_injects() -> None:
    resolver = ContentAnchorResolver(
        {
            "loyalty-program:0": "Gold members get a 10% loyalty discount.",
            "warranty-terms:1": "We ship the replacement before receiving the item.",
            "shipping-faq:0": "unrelated filler chunk",
        }
    )
    question = {
        "id": "q21",
        "gold_anchors": [
            "Gold members get a 10% loyalty discount",
            "ship the replacement before receiving",
        ],
    }
    assert resolver.resolve_question(question) == ["loyalty-program:0", "warranty-terms:1"]
    # resolve_all injects gold_stable_ids in place for the whole set.
    resolver.resolve_all([question])
    assert question["gold_stable_ids"] == ["loyalty-program:0", "warranty-terms:1"]
    print("ok: a multi-anchor question resolves to the union and injects gold_stable_ids")


def test_parse_gold_anchors_accepts_string_and_mapping() -> None:
    anchors = parse_gold_anchors(
        {
            "id": "q",
            "gold_anchors": ["a bare span", {"text": "scoped span", "doc": "shipping-faq"}],
        }
    )
    assert anchors == [("a bare span", None), ("scoped span", "shipping-faq")]
    print("ok: parse_gold_anchors accepts a bare string and a {text, doc} mapping")


def test_parse_gold_anchors_rejects_malformed() -> None:
    _expect_parse_error({"id": "q"}, "gold_anchors must be a non-empty list")
    _expect_parse_error({"id": "q", "gold_anchors": []}, "non-empty list")
    _expect_parse_error({"id": "q", "gold_anchors": "not a list"}, "non-empty list")
    _expect_parse_error({"id": "q", "gold_anchors": [123]}, "must be a string or a")
    _expect_parse_error({"id": "q", "gold_anchors": [{"doc": "d"}]}, "missing a string `text`")
    _expect_parse_error({"id": "q", "gold_anchors": [{"text": "   "}]}, "empty/blank text")
    _expect_parse_error(
        {"id": "q", "gold_anchors": [{"text": "x", "bogus": 1}]}, "unknown key"
    )
    _expect_parse_error(
        {"id": "q", "gold_anchors": [{"text": "x", "doc": ""}]}, "`doc` must be a non-empty string"
    )
    print("ok: parse_gold_anchors rejects every malformed gold_anchors shape")


# ---------------------------------------------------------------------------
# CORPUS layer — the PRD validation test against the real chunker (no DB)
# ---------------------------------------------------------------------------


def _corpus_chunk_contents(size: int | None, overlap: int | None) -> dict[str, str]:
    """Build `{stable_id: content}` by chunking the shipped corpus offline.

    Mirrors `db_seed.corpus_seed`: the production `chunk_text` + the same
    `{filename_slug}:{chunk_index}` stable_id, so this is byte-identical to what
    the runner would read back from a freshly-seeded DB — without one.
    """
    sys.path.insert(0, str(ROOT / "backend"))
    from chunking import chunk_text  # noqa: E402  (backend import, like runner.py)

    contents: dict[str, str] = {}
    for md in sorted(CORPUS_DIR.glob("*.md")):
        slug = _SLUG_RE.sub("-", md.stem.lower()).strip("-")
        for idx, chunk in enumerate(chunk_text(md.read_text(encoding="utf-8"), size, overlap)):
            contents[f"{slug}:{idx}"] = chunk
    return contents


def _load_shipped_questions() -> list[dict]:
    data = yaml.safe_load(RETRIEVAL_GOLD.read_text(encoding="utf-8"))
    return data["questions"]


def test_shipped_golden_set_resolves_at_default_seed() -> None:
    """Step 1: seed at 500/50; every anchor resolves; q07 straddles to two."""
    contents = _corpus_chunk_contents(500, 50)
    assert len(contents) == 14, f"expected 14 chunks at 500/50, got {len(contents)}"
    resolver = ContentAnchorResolver(contents)
    questions = _load_shipped_questions()
    assert len(questions) == 50

    by_id: dict[str, list[str]] = {}
    for q in questions:
        gold = resolver.resolve_question(q)  # raises on any zero-resolve
        assert gold, f"{q['id']}: resolved to empty gold"
        by_id[q["id"]] = gold

    # The shipped straddle case: the return-fee sentence is in the overlap and
    # resolves to BOTH returns-process chunks.
    assert by_id["q07"] == ["returns-process:0", "returns-process:1"], by_id["q07"]
    # A representative single-chunk anchor resolves to exactly one.
    assert by_id["q02"] == ["warranty-terms:0"], by_id["q02"]
    print(
        f"ok: all 50 shipped anchors resolve at 500/50 (q07 straddles -> "
        f"{by_id['q07']})"
    )


def test_reseed_requires_no_relabeling() -> None:
    """Step 2 / AC6: re-seed at a different chunk_size; the UNCHANGED golden set
    re-resolves with zero errors (the anchor re-points to new chunk indices)."""
    contents = _corpus_chunk_contents(300, 30)
    # A smaller chunk_size yields more chunks — different indices entirely.
    assert len(contents) > 14, f"expected a re-chunk to change chunk count, got {len(contents)}"
    resolver = ContentAnchorResolver(contents)
    questions = _load_shipped_questions()
    for q in questions:
        gold = resolver.resolve_question(q)  # must not raise
        assert gold, f"{q['id']}: resolved to empty gold after re-seed"
    print(
        f"ok: the unchanged golden set re-resolves after a re-seed to "
        f"{len(contents)} chunks — zero re-labeling"
    )


def test_nonmatching_anchor_fails_loud_against_corpus() -> None:
    """Step 3: an anchor whose text appears in no chunk raises + names it."""
    contents = _corpus_chunk_contents(500, 50)
    resolver = ContentAnchorResolver(contents)
    bogus = {
        "id": "q99",
        "gold_anchors": ["Acme Co accepts Dogecoin for all international orders"],
    }
    try:
        resolver.resolve_question(bogus)
    except ZeroResolveError as e:
        assert "q99" in str(e) and "Dogecoin" in str(e), str(e)
        print("ok: an anchor matching no corpus chunk fails loud (ZeroResolveError)")
        return
    raise AssertionError("expected ZeroResolveError for a non-matching anchor")


def _corpus_available() -> bool:
    if not CORPUS_DIR.is_dir() or not list(CORPUS_DIR.glob("*.md")):
        return False
    try:
        import tiktoken  # noqa: F401
    except Exception:
        return False
    return True


def main() -> None:
    # Unit layer — always runs.
    test_normalize_collapses_whitespace_preserves_content()
    test_single_chunk_resolves_to_one()
    test_straddling_span_resolves_to_both()
    test_zero_resolve_is_hard_error_naming_qid_and_text()
    test_whitespace_normalized_match_across_line_wrap()
    test_content_edit_breaks_anchor_no_fuzzy_match()
    test_doc_scope_narrows_resolution()
    test_multi_anchor_question_unions_and_injects()
    test_parse_gold_anchors_accepts_string_and_mapping()
    test_parse_gold_anchors_rejects_malformed()

    # Corpus layer — the PRD validation test; skips cleanly without the chunker.
    if _corpus_available():
        test_shipped_golden_set_resolves_at_default_seed()
        test_reseed_requires_no_relabeling()
        test_nonmatching_anchor_fails_loud_against_corpus()
        print("\nPASS: 13 content-anchor resolver (US-107) test groups")
    else:
        print(
            "\nSKIP corpus layer: tiktoken or db_seed/corpus unavailable "
            "(unit layer passed)"
        )
        print("PASS: 10 content-anchor resolver (US-107) unit test groups")


if __name__ == "__main__":
    # Allow `python -m evals.retrieval.test_content_anchors` from the repo root.
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    main()
