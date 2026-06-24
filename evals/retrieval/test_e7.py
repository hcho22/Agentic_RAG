"""US-051 validation test: E7 escalation golden-set schema + loader.

Pure / offline — no DB, no network, no backend import — like the offline half of
`test_e6.py`. Exercises `evals.retrieval.e7.load_escalation_questions` against
the shipped `escalation_gold.yaml` and against tmp YAML fixtures.

Covers the PRD validation test:
  * a golden YAML with one P1a, one P2, one P3 row loads with its labels;
  * a row with an invalid `escalation` value raises a clear loader error naming
    the allowed enum;
plus the failure indicators:
  * an out-of-enum label (including a hand-authored `p1b`) is rejected, never
    silently accepted, and the message names the allowed set;
  * the content-anchor rules: a no_context (P1a) row may NOT carry gold; an
    answerable_faithful/should_escalate row MUST carry a non-empty gold list;
  * the reference rule (US-053/054): an answerable_faithful (P2) AND a
    should_escalate (P3) row MUST each carry a non-empty `reference` answer (the
    offline judge's gold); only no_context (P1a) needs none;
  * the shipped golden set is internally consistent and contains NO `p1b` row.

Run:
    python -m evals.retrieval.test_e7
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from evals.retrieval.e7 import (
    ESCALATION_GOLD,
    ESCALATION_LABELS,
    POPULATION_BY_LABEL,
    load_escalation_questions,
)

# A minimal valid trio: one of each hand-authored population, anchored to real
# corpus chunks for the content-anchored labels.
_VALID_TRIO = """\
questions:
  - id: e7-p1a-x
    escalation: no_context
    question: What is Acme Co's stock ticker symbol?
  - id: e7-p2-x
    escalation: answerable_faithful
    question: How long is the electronics warranty?
    gold_stable_ids: [warranty-terms:0]
    reference: Electronics carry a 12-month limited warranty from shipped_at.
  - id: e7-p3-x
    escalation: should_escalate
    question: What is the warranty period for jewelry?
    gold_stable_ids: [warranty-terms:0]
    reference: The policy does not cover jewelry, so no warranty period can be grounded; escalate to a human.
"""


def _load_str(text: str) -> list[dict]:
    """Write `text` to a tmp YAML and load it through the real loader."""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(text)
        p = Path(f.name)
    try:
        return load_escalation_questions(p)
    finally:
        p.unlink()


def _expect_error(text: str, *must_contain: str) -> None:
    """Assert loading `text` raises RuntimeError whose message contains each
    `must_contain` substring (so the error is actionable, not just present)."""
    try:
        _load_str(text)
    except RuntimeError as e:
        msg = str(e)
        for sub in must_contain:
            assert sub in msg, f"error should mention {sub!r}, got: {msg!r}"
        return
    raise AssertionError(f"expected RuntimeError, but the YAML loaded:\n{text}")


def test_prd_valid_trio_loads() -> None:
    """PRD: one P1a + one P2 + one P3 load with their labels."""
    rows = _load_str(_VALID_TRIO)
    assert len(rows) == 3, f"expected 3 rows, got {len(rows)}"
    labels = [r["escalation"] for r in rows]
    assert labels == ["no_context", "answerable_faithful", "should_escalate"], labels
    # P1a has no anchor; the anchored rows do.
    by_id = {r["id"]: r for r in rows}
    assert "gold_stable_ids" not in by_id["e7-p1a-x"]
    assert by_id["e7-p2-x"]["gold_stable_ids"] == ["warranty-terms:0"]
    # US-053/054: the P2 AND P3 rows carry the reference answer the offline judge
    # scores against (the P3 reference encodes the should-escalate expectation).
    assert by_id["e7-p2-x"]["reference"].startswith("Electronics carry a 12-month")
    assert "escalate" in by_id["e7-p3-x"]["reference"].lower()
    print("ok: a valid P1a/P2/P3 trio loads with its labels")


def test_prd_invalid_label_rejected() -> None:
    """PRD core case: an out-of-enum `escalation` value raises a clear error that
    names the offending id AND the allowed enum (never silently accepted)."""
    bad = _VALID_TRIO + """\
  - id: e7-bogus
    escalation: maybe_escalate
    question: Should this be escalated?
    gold_stable_ids: [warranty-terms:0]
"""
    _expect_error(bad, "e7-bogus", "no_context", "answerable_faithful", "should_escalate")
    print("ok: an out-of-enum escalation label is rejected, naming the allowed enum")


def test_p1b_label_rejected() -> None:
    """Failure indicator: a hand-authored `p1b` row must NOT be accepted — P1b is
    the derived no-access case (US-057), not a label. It fails as out-of-enum."""
    text = """\
questions:
  - id: e7-p1b-hand
    escalation: p1b
    question: A viewer-specific no-access question someone tried to hand-author.
    gold_stable_ids: [warranty-terms:0]
"""
    _expect_error(text, "e7-p1b-hand", "escalation must be one of")
    print("ok: a hand-authored p1b label is rejected (P1b is derived, not authored)")


def test_no_context_with_gold_rejected() -> None:
    """A no_context (P1a) row carrying gold contradicts 'genuinely-no-context'."""
    text = """\
questions:
  - id: e7-bad-p1a
    escalation: no_context
    question: A no-context question that wrongly names a gold chunk.
    gold_stable_ids: [warranty-terms:0]
"""
    _expect_error(text, "e7-bad-p1a", "no_context", "NO gold_stable_ids")
    print("ok: a no_context row with gold_stable_ids is rejected")


def test_anchored_without_gold_rejected() -> None:
    """answerable_faithful and should_escalate are content-anchored: a missing or
    empty gold list is rejected (they must name the chunks proving retrieval)."""
    for label in ("answerable_faithful", "should_escalate"):
        missing = f"""\
questions:
  - id: e7-noanchor
    escalation: {label}
    question: A content-anchored row missing its gold anchor.
"""
        _expect_error(missing, "e7-noanchor", "non-empty gold_stable_ids")
        empty = f"""\
questions:
  - id: e7-emptyanchor
    escalation: {label}
    question: A content-anchored row with an empty gold anchor.
    gold_stable_ids: []
"""
        _expect_error(empty, "e7-emptyanchor", "non-empty gold_stable_ids")
    print("ok: P2/P3 rows without a non-empty gold anchor are rejected")


def test_referenced_labels_without_reference_rejected() -> None:
    """US-053/054: an answerable_faithful (P2) AND a should_escalate (P3) row MUST
    each carry a non-empty `reference` answer — the gold the offline Claude judge
    scores the drafted answer against. A missing or empty reference is rejected for
    BOTH labels (the gold anchor is present, so this isolates the reference rule).
    Only no_context (P1a) needs no reference."""
    for label in ("answerable_faithful", "should_escalate"):
        missing = f"""\
questions:
  - id: e7-noref
    escalation: {label}
    question: A content-anchored row missing its reference.
    gold_stable_ids: [warranty-terms:0]
"""
        _expect_error(missing, "e7-noref", "non-empty `reference`")
        empty = f"""\
questions:
  - id: e7-emptyref
    escalation: {label}
    question: A content-anchored row with an empty reference.
    gold_stable_ids: [warranty-terms:0]
    reference: ""
"""
        _expect_error(empty, "e7-emptyref", "non-empty `reference`")
    # P1a loads fine without a reference (only P2/P3 require one).
    ok = """\
questions:
  - id: e7-p1a-noref
    escalation: no_context
    question: What is Acme Co's stock ticker symbol?
"""
    rows = _load_str(ok)
    assert [r["id"] for r in rows] == ["e7-p1a-noref"], rows
    print("ok: a P2/P3 row without a reference is rejected; P1a needs none (US-053/054)")


def test_nonstring_gold_rejected() -> None:
    """gold_stable_ids must be non-empty strings (catches a YAML typo like an int
    chunk index or an empty entry)."""
    text = """\
questions:
  - id: e7-badgold
    escalation: answerable_faithful
    question: A row whose gold anchor is not a list of strings.
    gold_stable_ids: [123]
"""
    _expect_error(text, "e7-badgold", "non-empty strings")
    print("ok: non-string gold_stable_ids entries are rejected")


def test_structural_errors_rejected() -> None:
    """Dedup / missing-id / missing-question / empty-set guards (mirrors
    load_questions). Each fails loudly rather than scoring a malformed row."""
    _expect_error(
        _VALID_TRIO
        + """\
  - id: e7-p2-x
    escalation: no_context
    question: A duplicate id.
""",
        "duplicate question id",
        "e7-p2-x",
    )
    _expect_error(
        """\
questions:
  - escalation: no_context
    question: A row with no id.
""",
        "missing id",
    )
    _expect_error(
        """\
questions:
  - id: e7-noq
    escalation: no_context
""",
        "e7-noq",
        "question must be a non-empty string",
    )
    _expect_error("questions: []", "non-empty list")
    _expect_error("answers: {}", "no `questions` key")
    print("ok: duplicate/missing-id/missing-question/empty-set all rejected")


def test_shipped_golden_set_is_consistent() -> None:
    """The shipped `escalation_gold.yaml` loads, every row is in-enum, the
    content-anchor rules hold, there is NO p1b row, and all three populations are
    represented (so US-052-055 have something to score for each)."""
    rows = load_escalation_questions(ESCALATION_GOLD)
    assert rows, "shipped escalation gold must be non-empty"
    seen_labels: set[str] = set()
    for r in rows:
        label = r["escalation"]
        assert label in ESCALATION_LABELS, f"{r['id']}: out-of-enum label {label!r}"
        assert label != "p1b", "the shipped set must contain NO hand-authored p1b row"
        seen_labels.add(label)
        gold = r.get("gold_stable_ids")
        if label == "no_context":
            assert not gold, f"{r['id']}: P1a must carry no gold"
        else:
            assert gold, f"{r['id']}: {label} must carry a gold anchor"
    assert seen_labels == set(ESCALATION_LABELS), (
        f"shipped set must cover every population, missing "
        f"{set(POPULATION_BY_LABEL) - seen_labels}"
    )
    print(
        f"ok: shipped escalation_gold.yaml — {len(rows)} rows, "
        f"all in-enum, no p1b, all populations present"
    )


def main() -> None:
    test_prd_valid_trio_loads()
    test_prd_invalid_label_rejected()
    test_p1b_label_rejected()
    test_no_context_with_gold_rejected()
    test_anchored_without_gold_rejected()
    test_referenced_labels_without_reference_rejected()
    test_nonstring_gold_rejected()
    test_structural_errors_rejected()
    test_shipped_golden_set_is_consistent()
    print("\nPASS: 9 E7 schema/loader (US-051/053/054) test groups")


if __name__ == "__main__":
    # Allow `python -m evals.retrieval.test_e7` from the repo root.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    main()
