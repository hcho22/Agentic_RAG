"""US-108 validation test: the ONE layered golden set.

Pins the layered-golden-set contract end to end, entirely offline (no DB, no
network) — the corpus is chunked in-process with the REAL production chunker
(`backend.chunking.chunk_text`), exactly as `test_content_anchors.py` does, so
the resolved gold is byte-identical to a freshly-seeded DB without one.

The three layers (and who authors them):

  * BASE (every buyer) — `question -> gold content-anchor labels + category`.
    Authored in `retrieval_gold.yaml`; loaded by `runner.load_questions`.
  * DERIVED FOR FREE (zero extra authoring) — the three E4 viewer setups
    (`full_access` / `partial_access = gold ∪ N filler` / `no_access =
    all_non_gold`) AND the E7 P1b no-access population, both constructed by
    `runner.compute_visible_stable_ids` from the gold labels. The buyer
    hand-writes neither a permission test nor a P1b case.
  * SUPPORT-FACE (support buyers only) — one OPTIONAL `escalation` label per
    question. Authored in `escalation_gold.yaml`; loaded by
    `e7.load_escalation_questions`. A base-only set omits it and still runs the
    base + derived layers without error; a support set additionally runs the
    escalation suite.

Encodes the PRD validation test:
  1. The base-only set loads and its three viewer setups + the P1b population are
     DERIVED — with NO hand-authored permission/P1b entries.
  2. The support set's per-question `escalation` labels load and read; its P1b
     population is derived from a P2 row (never hand-authored).
  3. The base-only set does NOT error on the absent support layer.

Failure indicator (asserted absent): a buyer must hand-author the viewer matrix
or a P1b case, or a non-support golden set fails because it lacks escalation
labels.

Run:
    python -m evals.retrieval.test_us108_layered_golden_set
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import yaml

from evals.retrieval.e7 import (
    ESCALATION_GOLD,
    ESCALATION_LABELS,
    POPULATION_BY_LABEL,
    load_escalation_questions,
    resolve_escalation_gold,
)
from evals.retrieval.runner import (
    compute_visible_stable_ids,
    has_support_layer,
    load_questions,
    resolve_gold_anchors,
)

ROOT = Path(__file__).resolve().parents[2]
RETRIEVAL_GOLD = Path(__file__).resolve().parent / "retrieval_gold.yaml"
CORPUS_DIR = ROOT / "db_seed" / "corpus"
_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Fields that would mean the buyer hand-authored a permission matrix or a P1b
# case — the layered format forbids them (all derived/resolved). `gold_stable_ids`
# is the RESOLVED internal representation (US-107): never authored, only injected.
_HAND_AUTHORED_PERMISSION_KEYS = (
    "gold_stable_ids",
    "visible",
    "visible_stable_ids",
    "viewer",
    "viewers",
    "acl",
    "permissions",
    "p1b",
)


def _corpus_chunk_contents(size: int, overlap: int) -> dict[str, str]:
    """`{stable_id: content}` from chunking the shipped corpus offline (no DB)."""
    sys.path.insert(0, str(ROOT / "backend"))
    from chunking import chunk_text  # noqa: E402  (backend import, like runner.py)

    contents: dict[str, str] = {}
    for md in sorted(CORPUS_DIR.glob("*.md")):
        slug = _SLUG_RE.sub("-", md.stem.lower()).strip("-")
        for idx, chunk in enumerate(chunk_text(md.read_text(encoding="utf-8"), size, overlap)):
            contents[f"{slug}:{idx}"] = chunk
    return contents


# ---------------------------------------------------------------------------
# Layer 1 (base) + Layer 2 (derived-for-free): the E4 viewer matrix
# ---------------------------------------------------------------------------


def test_base_only_set_has_no_support_layer_and_does_not_error() -> None:
    """Step 3 / failure indicator: the shipped base retrieval gold carries NO
    `escalation` labels, loads without error, and `has_support_layer` is False —
    a knowledge-assistant-only buyer never authors the support layer."""
    questions, viewer_construction = load_questions(RETRIEVAL_GOLD)
    assert questions, "base golden set must be non-empty"
    assert has_support_layer(questions) is False, (
        "the base retrieval gold must carry NO support-face labels"
    )
    # The base loader recognizes the OPTIONAL layer — a base-only set simply omits
    # it — so loading must not have raised for a missing `escalation` field.
    assert all("escalation" not in q for q in questions)
    # The derived-for-free viewer rule is authored ONCE at the top level, not
    # per question — the audit trail for what each viewer saw.
    assert set(viewer_construction) == {"full_access", "partial_access", "no_access"}
    print(f"ok: base-only set ({len(questions)} q) loads, no support layer, no error")


def test_no_hand_authored_permission_or_p1b_entries() -> None:
    """The load-bearing derived-for-free property: NEITHER golden set hand-authors
    a viewer matrix, a per-question visible set, or a P1b case. Every permission
    setup is DERIVED from the gold labels; `gold_stable_ids` is resolved, never
    authored."""
    base = yaml.safe_load(RETRIEVAL_GOLD.read_text(encoding="utf-8"))["questions"]
    support = yaml.safe_load(ESCALATION_GOLD.read_text(encoding="utf-8"))["questions"]
    for label, rows in (("retrieval_gold", base), ("escalation_gold", support)):
        for q in rows:
            for key in _HAND_AUTHORED_PERMISSION_KEYS:
                assert key not in q, (
                    f"{label} {q.get('id')!r} hand-authors {key!r} — the viewer "
                    f"matrix / P1b population must be DERIVED, never authored"
                )
    # No support row is a hand-authored `p1b` label (P1b is the derived no-access
    # case, US-057) — the loader rejects it, pinned here at the data layer too.
    assert all(q.get("escalation") != "p1b" for q in support)
    print("ok: no hand-authored permission/P1b entries in either golden set")


def test_labeling_gold_once_derives_the_three_viewer_setups() -> None:
    """Layer 2: labeling gold ONCE auto-generates the full/partial/no_access
    viewer matrix via `compute_visible_stable_ids` — the exact rule the E4 sweep
    uses. full = all; no_access = all ∖ gold; partial = gold ∪ N filler."""
    contents = _corpus_chunk_contents(500, 50)
    all_ids = sorted(contents)
    questions, vc = load_questions(RETRIEVAL_GOLD)
    resolve_gold_anchors(questions, contents)  # inject resolved gold_stable_ids

    for q in questions:
        gold = set(q["gold_stable_ids"])
        assert gold, f"{q['id']}: resolved to empty gold"
        full = compute_visible_stable_ids("full_access", q, all_ids, vc)
        partial = compute_visible_stable_ids("partial_access", q, all_ids, vc)
        no_access = compute_visible_stable_ids("no_access", q, all_ids, vc)
        # full = every chunk; no_access = everything EXCEPT the gold (the E4
        # zero-leak construction); partial = gold ∪ N random non-gold.
        assert full == set(all_ids), f"{q['id']}: full_access must see all chunks"
        assert no_access == set(all_ids) - gold, f"{q['id']}: no_access must be all_non_gold"
        assert gold <= partial, f"{q['id']}: partial_access must include the gold"
        assert not (gold & no_access), f"{q['id']}: no gold may be visible to no_access"
    print(f"ok: gold labeled once -> the 3 viewer setups derived for {len(questions)} q")


# ---------------------------------------------------------------------------
# Layer 3 (support-face) + its derived P1b
# ---------------------------------------------------------------------------


def test_support_set_reads_per_question_escalation_labels() -> None:
    """Step 2: the support golden set's per-question `escalation` labels load and
    read; all three populations (P1a/P2/P3) are represented, so the escalation
    suite has something to score for each. This is the ONLY support-only authoring
    step."""
    rows = load_escalation_questions(ESCALATION_GOLD)
    assert rows, "support golden set must be non-empty"
    assert has_support_layer(rows) is True, "every support row carries an escalation label"
    seen = {r["escalation"] for r in rows}
    assert seen == set(ESCALATION_LABELS), (
        f"support set must cover every population, missing "
        f"{set(POPULATION_BY_LABEL) - seen}"
    )
    print(f"ok: support set reads {len(rows)} per-question escalation labels ({sorted(seen)})")


def test_support_gold_anchors_resolve_and_p1b_is_derived() -> None:
    """Layer 3 built on the base content-anchor primitive + its DERIVED P1b: the
    P2/P3 `gold_anchors` resolve against the live corpus (P1a stays empty), and the
    P1b no-access population is DERIVED from a P2 row via the SAME
    `compute_visible_stable_ids` the base uses — never hand-authored."""
    contents = _corpus_chunk_contents(500, 50)
    all_ids = sorted(contents)
    rows = load_escalation_questions(ESCALATION_GOLD)
    resolve_escalation_gold(rows, contents)  # fail-loud on any zero-resolve

    for r in rows:
        if r["escalation"] == "no_context":
            assert r["gold_stable_ids"] == [], f"{r['id']}: P1a must resolve to no gold"
        else:
            assert r["gold_stable_ids"], f"{r['id']}: {r['escalation']} must resolve to gold"

    # P1b (US-057) = a P2 row replayed under the no_access viewer. Derived from the
    # SAME construction as the base E4 no_access cell — no hand-authored P1b case.
    p2_rows = [r for r in rows if r["escalation"] == "answerable_faithful"]
    assert p2_rows, "the support set must carry P2 rows to derive P1b from"
    for p2 in p2_rows:
        gold = set(p2["gold_stable_ids"])
        p1b_visible = compute_visible_stable_ids("no_access", p2, all_ids, {})
        assert p1b_visible == set(all_ids) - gold, f"{p2['id']}: P1b must be all_non_gold"
        assert not (gold & p1b_visible), (
            f"{p2['id']}: the P1b no-access viewer must see NONE of the gold"
        )
    print(f"ok: support gold_anchors resolve; P1b derived from {len(p2_rows)} P2 rows")


def test_one_layered_schema_loads_under_both_loaders() -> None:
    """The 'ONE layered golden set': a single question carrying the BASE layer
    (category + gold_anchors) AND the SUPPORT layer (escalation + reference) is
    valid under BOTH the base loader and the support loader — one schema, optional
    top layer, no second gold primitive."""
    import tempfile

    layered = """\
questions:
  - id: q-layered
    category: single_chunk
    question: How long is the electronics warranty?
    gold_anchors:
      - Electronics carry a 12-month limited warranty against manufacturing defects
    escalation: answerable_faithful
    reference: Electronics carry a 12-month limited warranty from shipped_at.
"""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(layered)
        p = Path(f.name)
    try:
        base_qs, _ = load_questions(p)  # base loader: category + gold_anchors, escalation optional
        support_qs = load_escalation_questions(p)  # support loader: escalation + gold_anchors + reference
    finally:
        p.unlink()
    assert base_qs[0]["escalation"] == "answerable_faithful"
    assert support_qs[0]["gold_anchors"] == [
        "Electronics carry a 12-month limited warranty against manufacturing defects"
    ]
    assert has_support_layer(base_qs) is True
    print("ok: one layered question loads under BOTH the base and support loaders")


def main() -> None:
    test_base_only_set_has_no_support_layer_and_does_not_error()
    test_no_hand_authored_permission_or_p1b_entries()
    test_labeling_gold_once_derives_the_three_viewer_setups()
    test_support_set_reads_per_question_escalation_labels()
    test_support_gold_anchors_resolve_and_p1b_is_derived()
    test_one_layered_schema_loads_under_both_loaders()
    print("\nPASS: 6 US-108 layered-golden-set validation test groups")


if __name__ == "__main__":
    # Allow `python -m evals.retrieval.test_us108_layered_golden_set` from the repo root.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    main()
