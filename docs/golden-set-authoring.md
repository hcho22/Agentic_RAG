# Authoring your own golden set

This guide is for a buyer replacing the shipped e-commerce golden set with one built on their own corpus.
It teaches the one idea that makes a permissions-aware eval trustworthy - **exhaustive gold labeling is the correctness contract, not a recall nicety** - and states plainly where a lenient configuration stops being proof.

Read `docs/evals.md` §2.2 first for the format reference.
This guide is the *why*: what to author, why completeness is load-bearing, and how to keep the eval an honest signal you can show a client.

The shipped golden set (`evals/retrieval/retrieval_gold.yaml` + `evals/retrieval/escalation_gold.yaml`) is a **worked example to learn the format from, not a survives-the-swap artifact**.
Its content anchors are quoted from the shipped corpus, so pointing the eval at your own docs makes those anchors fail loud (US-107, below) - by design.
"Replace the corpus" and "author a new golden set" are the same step.

---

## 1. What you author: the base layer

Every buyer authors exactly one primitive per question:

```yaml
- id: q01
  category: single_chunk          # single_chunk | multi_hop | adversarial | paraphrase | lexical
  question: What's the standard shipping window for domestic orders?
  gold_anchors:
    - Standard shipping is 3–5 business days within the contiguous US
```

- **`question`** - the natural-language query passed to retrieval.
- **`category`** - one of `single_chunk`, `multi_hop`, `adversarial`, `paraphrase`, `lexical`; drives the per-category aggregate only, not scoring.
- **`gold_anchors`** - one or more **content anchors** (§2): the answer-bearing spans that a *correct* retrieval must surface for this question.

That is the whole base layer.
The E4 permission/leak matrix and the E7 P1b no-access replay are then **derived for free** from these labels (§4) - you never hand-author a permission test or a P1b case.

---

## 2. Content anchoring: author answer-bearing text, never chunk indices (US-107)

A gold label is a **quoted span that actually appears in the corpus**, not a `{filename_slug}:{chunk_index}` pointer.
At eval time `evals/retrieval/content_anchors.py` resolves each anchor to whichever chunk `stable_id`(s) currently *contain* its text and hands the resolved list to the scorer.
The chunk index survives only as that resolved internal representation; you never type it.

An anchor is either a bare string or a `{text, doc}` mapping that restricts resolution to one document by `filename_slug`:

```yaml
gold_anchors:
  - Customers may request a refund within 30 days of the order's `shipped_at` date
  - text: "$14.95 for returns 5–20 lbs"
    doc: returns-process
```

Three properties follow, and all three are the point:

1. **A re-chunk needs zero re-labeling.**
   Sweep `chunk_size`, overlap, or the docling parser and re-run - the same anchor re-resolves to the new chunk indices.
   This is what lets the eval drive your chunking iteration loop instead of forcing a "re-label everything after re-chunking" step.

2. **A span in the overlap region resolves to both chunks.**
   The chunker carries a trailing block from one chunk into the next, so a paragraph in the overlap is a verbatim substring of *both* adjacent chunks.
   A plain substring match returns both `stable_id`s; the recall scorer's multi-gold partial credit (`recall_at_k` divides by `|gold|`) handles it.
   The shipped `q07` return-fee anchor is exactly this straddle case.

3. **Zero-resolve is a hard error - never a silent `recall=0`.**
   An anchor that matches no current chunk raises `ZeroResolveError` naming the question id and the offending span, and fails the whole run.
   Matching is whitespace-normalized but otherwise **exact**: case, punctuation, and en/em dashes must match the corpus verbatim.
   The resolver does **not** fuzzy-match around a content edit - editing the source so the quoted words no longer appear breaks the anchor on purpose, which is how you find out a label went stale instead of shipping a green run over a broken one.

**Authoring an anchor:** copy the span verbatim from the seeded chunk, then confirm it resolves before you commit.
A span that reads well in your head but is not a byte-for-byte substring of a chunk (a smart-quote, an em dash you typed as a hyphen, a word you paraphrased) fails loud on the first run.
That loudness is the feature.

---

## 3. The completeness contract: under-labeling is a **security** defect, not a recall miss

This is the load-bearing section.
Read it even if you skip the rest.

The eval demonstrates a security claim - *a viewer with no access to a document's answer never retrieves it* - by **deriving** the permission matrix from your gold labels.
For each question the runner constructs three viewers from the resolved gold (`runner.compute_visible_stable_ids`):

| Viewer | Visible set | Formula |
|---|---|---|
| `full_access` | every corpus chunk (owner) | `all_corpus` |
| `partial_access` | gold plus N random filler | `gold ∪ N non-gold` |
| `no_access` | **everything except gold** | `all_corpus ∖ gold` |

Look hard at `no_access`.
It is defined as **`all_non_gold`** - the viewer legitimately holds an ACL grant for *every chunk you did not label gold*.
The security table then asserts that this viewer retrieves **0 gold** across every mode and filter, and a clean run reads `1.000`.

Now suppose you under-label.
A chunk `C` genuinely answers question `Q`, but you only anchored one of the two chunks that contain the answer and forgot `C`.
Because `C ∉ gold(Q)`, the `no_access` viewer for `Q` **is granted `C`** (it falls in the `all_non_gold` pool).
Retrieval for `Q` returns `C` - of course it does, `C` is relevant.
But recall is scored over labeled gold only (`|gold ∩ top_k| / |gold|`), so retrieving `C` contributes **0** to recall.
The no-access cell sees "0 gold retrieved," counts the run as clean, and the security table stays **`1.000` green**.

A viewer with no access to `Q`'s answer just retrieved a chunk containing `Q`'s answer, and the green checkmark hid it.

That is a **false security pass**.
It is invisible in the summary - the table is `1.000`, the run exits `0`, CI is green - because the gate can only reason about the gold you declared.
An un-labeled relevant chunk is indistinguishable, to the eval, from an irrelevant one: both live in the non-gold pool, both are handed to the `no_access` viewer, and only the labeled ones are checked for leakage.

> **The contract:** `no_access = all_non_gold` and `partial_access = gold ∪ N filler` mean that *every relevant chunk you fail to label is silently reclassified as safe-to-disclose.*
> Exhaustive gold labeling is therefore load-bearing for the **security** claim, not merely a recall concern.
> If a document contains the answer in three places, all three are gold; anchor all three.

This is a distinct failure from the re-chunking brittleness that content anchors (§2) solve.
Content anchors keep a *correct* label pointing at the right chunk across a re-seed.
The completeness contract is about *not omitting a label in the first place* - anchoring cannot save you from a chunk you never mentioned.
The two together are the whole discipline: anchor answer-bearing text (§2), and anchor **all** of it (§3).

**How to label exhaustively.**
For each question, ask "which chunks contain text that answers this?" and anchor a span from each - not just the single best chunk.
A multi-hop question authors one anchor per required chunk.
When you edit a document, re-run the eval: a stale anchor fails loud (§2), which is your prompt to re-check whether the answer moved or now lives in a new chunk you must also anchor.

---

## 4. Derived for free: the E4 matrix and E7 P1b (US-108)

The payoff of labeling gold once is that two eval populations are constructed with **zero extra authoring**:

- **The E4 viewer matrix** - the three viewers in §3, built by `runner.compute_visible_stable_ids` from the gold labels. You write no permission test.
- **The E7 P1b population** - the same `answerable_faithful` (P2) question replayed under a `no_access` viewer, rebuilt at run time by the E7 runner via the same `no_access` construction. P1b is never a label; the loader rejects a hand-authored `p1b`.

This is *why* completeness is non-negotiable: the security and disclosure populations are functions of your gold set.
Under-label, and you weaken a test you never see - because you never wrote it.

---

## 5. The support-face layer: one escalation label (US-108)

The support-face layer is the **only** support-only authoring step, and only if you ship the support widget.
Add one `escalation` label per question in `evals/retrieval/escalation_gold.yaml`, on the same content-anchor primitive as the base layer:

| Label | Population | Means | Gold anchors |
|---|---|---|---|
| `no_context` | P1a | Answer is absent from the corpus; expected to escalate at the retrieval gate. | none (by definition) |
| `answerable_faithful` | P2 | Strong retrieval **and** a faithful grounded answer exists; expected to deflect. | required |
| `should_escalate` | P3 | Strong retrieval **but** no faithful answer exists; must escalate. Auto-resolving here is a false-resolve - the safety metric. | required |

P2-vs-P3 is the single judgment that cannot be derived: "does a faithful answer actually exist from these chunks?" is a human call.
Everything else in the escalation suite (the P1b no-access replay, the leak checks) is derived from the gold you already labeled.

A knowledge-assistant-only buyer **omits this layer entirely**.
A base-only golden set with no `escalation` labels loads and runs the base plus derived-for-free layers without error; a support golden set additionally runs the escalation suite.
A present-but-typo'd label is still rejected fail-closed.

---

## 6. Recommended: cross-family corroboration (US-103)

The kit's default configuration judges quality with **two independent judges from different model families**, and you should keep it that way.

The generator is OpenAI (`gpt-4o-mini`) and the RAGAS judge is the same family - a judge can be systematically lenient toward outputs from its own family.
The kit corroborates every RAGAS Faithfulness / Answer Relevancy drop against an independent **cross-family Claude judge** (a different vendor, a different model, a different prompting technique), and only escalates to a red alert when *both* judges see the same drop in the same cell.

In moat terms: cross-family corroboration is **one extra judge pass on the weekly sweep, cents of spend**, and it turns "a number moved" into "**two independent judges from different families agree the number moved**."
That is the difference between a metric you tune internally and a result you can defend to a client.
It costs almost nothing and it is the single most credible thing the eval says.

The binding is declared in `evals/gate/gate.yaml` under `bindings.corroboration`:

```yaml
corroboration:
  generator_family: openai
  judge_family: anthropic          # a DIFFERENT family from the generator
  judge_cell: "full_access:pre_filter"
  judge_equivalent:
    faithfulness:       { judge_metric: faithfulness, drop: 0.3 }
    answer_relevancy:   { judge_metric: helpfulness,  drop: 0.2 }
```

The **detection algorithm is fixed** - rolling-median drift, cross-family corroboration, `single-judge-red`, severity, `auto_close_weeks`; you only point these *bindings* at your own cells, thresholds, and judge families without forking `evals/retrieval/ragas_gates.py`.

Corroboration is the one binding **not inherited when omitted**.
A custom `bindings:` block that drops the `corroboration:` sub-block - or sets `judge_family == generator_family` - runs single-family: every RAGAS drop degrades to `single-judge-red` (still red, tagged so a reader knows it rests on one judge, given a longer 2-week auto-close window).
That degradation is silent in the number but loud in the tag; keep the sub-block.

---

## 7. Single-family evals are a **weaker proof** - do not cite one as "proven"

State this to yourself before you state a score to a client.

A single-family eval - where the only judge is the same model family as the answer generator - **carries same-family bias**.
A judge is measurably prone to rating its own family's outputs as more faithful than an independent judge would.
So a lenient single-family faithfulness score is **a weaker proof** than the cross-family configuration the kit demonstrates (§6), and it is **not** evidence you may present as "proven."

Concretely:

- A `single-judge-red` finding rests on **one** judge; the tag exists precisely so a reader does not mistake it for a corroborated result.
- A green single-family faithfulness number means "our own family's judge did not flag our own family's output" - a claim about internal consistency, not independent verification.
- **Do not tell a client a single-family faithfulness score is "proven" or "independently verified."** It is neither. Say what it is: a same-family judge's read, useful for tracking your own regressions, not a cross-vendor guarantee.

If you run single-family for cost reasons, that is a legitimate trade - but label it honestly in any report, and understand you have opted out of the kit's strongest signal.
The cross-family configuration is cheap (§6); the reason to skip it is rarely worth the credibility you give up.

This is the eval-domain's capability-honesty line, alongside the F3 / P5 detection-latency gap recorded in `docs/evals.md` ("The accepted detection-latency gap (F3 / P5)") and the same-family-bias framing in `docs/evals.md` §"RAGAS comparison".
The kit's default demonstrates cross-family corroboration on purpose so the shipped result is defensible; a buyer who narrows to single-family owns the weaker claim.

---

## Authoring checklist

- [ ] Every gold label is an **answer-bearing content anchor** (a verbatim corpus span), never a chunk index (§2).
- [ ] Every anchor **resolves** - run the eval; a `ZeroResolveError` names any stale or typo'd span (§2).
- [ ] Gold is **exhaustive**: every chunk that contains a question's answer is anchored, not just the best one (§3, the completeness contract).
- [ ] You wrote **no** permission test and **no** P1b case - those derive from the gold (§4).
- [ ] Support buyers only: one `escalation` label per question (§5); knowledge-assistant-only buyers omit the layer.
- [ ] Keep `bindings.corroboration` cross-family (`judge_family != generator_family`) (§6).
- [ ] Any single-family score is labeled as a weaker, non-independent proof in every report (§7).

## Where things live

| File | What you author |
|---|---|
| `evals/retrieval/retrieval_gold.yaml` | The base layer: `question → gold_anchors + category`. |
| `evals/retrieval/escalation_gold.yaml` | Support-face only: one `escalation` label per question. |
| `evals/gate/gate.yaml` | Gate bindings: cells, thresholds, and the cross-family corroboration block. |
| `evals/retrieval/content_anchors.py` | The resolver (read-only reference - you do not edit it to author). |

Cross-references: `docs/evals.md` §2.2 (format reference), §6 (E7 escalation), §"RAGAS comparison" (cross-family judge); US-107 (content anchors), US-108 (layered format), US-103 (gate bindings).
