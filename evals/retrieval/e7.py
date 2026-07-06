"""US-051 / US-108: E7 escalation golden-set schema + loader (ADR-0003 / E9).

E7 is the **support-face** layer of the one layered golden set: it scores the
deflection pipeline (the US-047 retrieval gate + the US-048 faithfulness gate)
instead of raw retrieval recall. Like E6, it is **additive** to the E4 retrieval
sweep — a separate golden set in `escalation_gold.yaml`, run by its own machinery
(US-052+), never touching the six-cell `full/partial/no_access × pre/post` sweep
in `runner.py`.

**The layered format (US-108).** The kit ships ONE layered golden-set format so a
buyer's authoring burden scales with the faces they ship:

* **Base** (every buyer) — `question → gold content-anchor labels + category`, the
  minimal primitive authored in `retrieval_gold.yaml` (US-107).
* **Derived for free** — the runner builds the three E4 viewer setups AND the E7
  P1b no-access replay from the gold labels; the buyer hand-writes neither a
  permission test nor a P1b case.
* **Support-face** (support buyers only) — one `escalation` label per question, the
  layer this module owns. It is **optional**: a knowledge-assistant-only buyer
  ships no escalation labels and runs the base + derived-for-free layers without
  error (`runner.load_questions` recognizes the layer as optional); a support
  buyer additionally runs this escalation suite.

This module owns the support-face **schema + loader + anchor resolver** (US-051 +
US-108). The P2/P3 gold labels are authored on the SAME US-107 content-anchor
primitive as the base layer — an answer-bearing `gold_anchors` span resolved to
the current chunk stable_id(s) at eval time (`content_anchors.py`), replacing the
legacy directly-authored `gold_stable_ids` chunk-index list. So a
`chunk_size`/overlap re-seed needs ZERO re-labeling here too, and an anchor that
matches no chunk is a HARD ERROR (never a silent miss). The populations are still
*derived from the existing gold anchor*, not authored as a parallel set: each
question carries exactly one `escalation` label.

The three hand-authored populations (E9 support-face layer) and their P-codes:

* ``no_context``          → **P1a** — genuinely-no-context: the answer is absent
  from the corpus, so the row has **no** gold chunks. Expected to escalate at
  the *retrieval* gate (weak cosine), with no draft/judge call (US-052).
* ``answerable_faithful`` → **P2**  — strong retrieval **and** a faithful
  grounded answer exists from the gold chunks. Expected to auto-resolve; the
  deflection rate is measured on this population (US-053).
* ``should_escalate``     → **P3**  — strong retrieval **but no** faithful
  grounded answer (the topic is on-corpus, the specific fact is not, or the doc
  defers to a human). Expected to clear retrieval yet fail the faithfulness gate
  and escalate (US-054). This is the moat case: a P3 that auto-resolves is a
  *false-resolve* (Risk #3), the safety metric the ceiling governs (US-055/059).

P2-vs-P3 is the *only* human judgment in authoring ("does a faithful answer
exist from these chunks?"); P1a-vs-the-rest follows mechanically from "are there
gold chunks?".

**P1b is deliberately NOT a label here.** P1b — a question that *is* answerable
in general but for which *this viewer* can see no gold chunk — is the derived,
viewer-parameterized case (US-057): the runner reconstructs it at run time from a
P2 row via `runner.compute_visible_stable_ids`'s ``no_access`` construction.
It is never hand-authored, so the schema has no `p1b` value and the loader
rejects one as out-of-enum.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from evals.retrieval.content_anchors import (
    ContentAnchorResolver,
    parse_gold_anchors,
)

# The default E7 golden set, sibling to the E4 `retrieval_gold.yaml`.
ESCALATION_GOLD = Path(__file__).resolve().parent / "escalation_gold.yaml"

# The three hand-authored escalation populations (US-051). The loader validates
# `escalation` is exactly one of these — `p1b` (the derived no-access case) and
# any typo are rejected.
ESCALATION_LABELS = ("no_context", "answerable_faithful", "should_escalate")

# Label → E9 P-code, for the runner's per-population reporting (US-052-055).
POPULATION_BY_LABEL: dict[str, str] = {
    "no_context": "P1a",
    "answerable_faithful": "P2",
    "should_escalate": "P3",
}

# The gold-chunk labels: a P2/P3 row MUST name the gold chunks that make
# retrieval strong, authored as US-107 `gold_anchors` content anchors (US-108;
# see the module docstring). A `no_context` (P1a) row MUST NOT — there is, by
# definition, no corpus chunk to anchor on.
_ANCHORED_LABELS = ("answerable_faithful", "should_escalate")

# Labels scored end-to-end against the OFFLINE cross-family Claude judge, which
# needs a hand-authored gold answer to score against — so a `reference` is
# REQUIRED non-empty for these. `answerable_faithful` (P2, US-053) carries the
# faithful gold answer; `should_escalate` (P3, US-054) carries the should-escalate
# gold (what the correct human-deferring response looks like) that the offline
# judge scores the drafted answer against. P1a (`no_context`) needs none.
_REFERENCED_LABELS = ("answerable_faithful", "should_escalate")


def load_escalation_questions(path: Path = ESCALATION_GOLD) -> list[dict[str, Any]]:
    """Load + validate the E7 escalation golden set (US-051).

    Mirrors the enum/dedup discipline of `runner.load_questions`: a top-level
    `questions` non-empty list, each row with a unique string `id`, a non-empty
    `question`, and exactly one `escalation` label in `ESCALATION_LABELS`. The
    gold anchor is **required and non-empty** for the gold-chunk labels
    (`answerable_faithful` / `should_escalate`) — a US-107 `gold_anchors` list of
    content anchors, structurally validated by `content_anchors.parse_gold_anchors`
    (bare string or `{text, doc}` mapping) — and **forbidden** for `no_context`
    (genuinely-no-context rows carry no gold). The two LLM-judged labels
    (`answerable_faithful` P2, US-053; `should_escalate` P3, US-054) each
    additionally **require** a non-empty `reference` answer — the gold the offline
    Claude judge scores the drafted answer against (for P3 the gold encodes the
    should-escalate / human-deferral expectation).

    Raises `RuntimeError` with a clear, id-prefixed message on any violation; an
    out-of-enum label (including a hand-authored `p1b`) names the allowed set, so
    a typo or a stray P1b row fails loudly instead of being silently scored.
    Returns the raw question dicts unchanged (the scoring runner — US-052+ —
    consumes them), so this loader stays pure: no DB, no network, no backend
    import. This validates only the anchor *structure*; RESOLVING each anchor to a
    current chunk stable_id (and the fail-loud zero-resolve check) is a
    scoring-time concern handled by `resolve_escalation_gold`, not here.
    """
    with path.open() as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or "questions" not in data:
        raise RuntimeError(f"no `questions` key in {path}")
    questions = data["questions"]
    if not isinstance(questions, list) or not questions:
        raise RuntimeError(f"`questions` in {path} must be a non-empty list")

    seen_ids: set[str] = set()
    for q in questions:
        if not isinstance(q, dict):
            raise RuntimeError(f"question must be a mapping, got {q!r}")
        qid = q.get("id")
        if not qid or not isinstance(qid, str):
            raise RuntimeError(f"question missing id: {q!r}")
        if qid in seen_ids:
            raise RuntimeError(f"duplicate question id: {qid}")
        seen_ids.add(qid)

        question_text = q.get("question")
        if not question_text or not isinstance(question_text, str):
            raise RuntimeError(f"{qid}: question must be a non-empty string")

        label = q.get("escalation")
        if label not in ESCALATION_LABELS:
            raise RuntimeError(
                f"{qid}: escalation must be one of {ESCALATION_LABELS}, got {label!r} "
                "(P1b is the derived no-access case rebuilt from a P2 row at run "
                "time — never a hand-authored label)"
            )

        gold = q.get("gold_anchors")
        if label in _ANCHORED_LABELS:
            if not isinstance(gold, list) or not gold:
                raise RuntimeError(
                    f"{qid}: a {label} ({POPULATION_BY_LABEL[label]}) row must carry a "
                    "non-empty gold_anchors list (US-107 content anchors on the chunks "
                    "that make retrieval strong)"
                )
            # Validate each anchor's shape (bare string or {text, doc} mapping),
            # reusing the same structural validator the base retrieval gold uses.
            parse_gold_anchors(q)
        elif gold:  # no_context — gold must be absent or empty
            raise RuntimeError(
                f"{qid}: a no_context (P1a) row must have NO gold_anchors "
                f"(genuinely-no-context), got {gold!r}"
            )

        if label in _REFERENCED_LABELS:
            reference = q.get("reference")
            if not reference or not isinstance(reference, str):
                raise RuntimeError(
                    f"{qid}: an {label} ({POPULATION_BY_LABEL[label]}) row must carry a "
                    "non-empty `reference` string — the gold answer the offline Claude "
                    "judge scores the drafted answer against (US-053)"
                )

    return questions


def resolve_escalation_gold(
    questions: list[dict[str, Any]],
    chunk_contents: dict[str, str],
) -> None:
    """Resolve each gold-bearing escalation row's content anchors in place (US-108).

    The P2/P3 gold labels carry US-107 `gold_anchors` content anchors; this
    resolves them against `chunk_contents` (the live corpus text the runner
    fetched from the DB) to the current chunk `stable_id`(s) and injects the
    resolved `gold_stable_ids` list the P1b no-access replay consumes — exactly as
    `runner.resolve_gold_anchors` does for the base retrieval gold. A `no_context`
    (P1a) row has no anchors and gets an empty `gold_stable_ids`.

    Any anchor matching zero current chunks raises `content_anchors.ZeroResolveError`
    (naming the question id + anchor text) and fails the run — never a silent miss.
    Pure: it mutates the dicts and touches no DB/network (the caller fetches
    `chunk_contents`), so the resolver stays unit-testable against an offline
    chunking of the corpus.
    """
    resolver = ContentAnchorResolver(chunk_contents)
    for q in questions:
        if q.get("gold_anchors"):
            q["gold_stable_ids"] = resolver.resolve_question(q)
        else:
            # no_context (P1a): genuinely no gold chunk to anchor on.
            q["gold_stable_ids"] = []
