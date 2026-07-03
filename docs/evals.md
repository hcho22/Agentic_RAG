# Retrieval Evals: measuring vector, keyword, and hybrid search

## 1. Why this exists

The `search_documents` tool in `backend/retrieval.py` exposes three retrieval modes — vector-only (`match_chunks` over pgvector HNSW), keyword-only (Postgres full-text search through the `keyword_search` RPC), and hybrid (both, fused via Reciprocal Rank Fusion). They've been wired up since Module 6 (US-020 / US-021). What we did **not** have, until Module 10, was a way to say *how good* any of them are.

Without measurement, claims about retrieval quality are vibes. "Hybrid is better than vector-only" is not falsifiable until you can put a number on it. "Adding a reranker improves recall" is not falsifiable without a recall metric. And — most consequentially for the future modules — "swapping pgvector for Pinecone changes quality by X%" is not a comparison you can make if X is undefined.

Module 10 is the infrastructure that lets future modules ship with real numbers instead of "trust me, it works." This document describes what we measure, how, where the eval falls short, and what a regression looks like when CI catches one.

## 2. Methodology

### 2.1 Corpus

The eval runs against a fixed text corpus of 7 markdown documents committed to the repo under `db_seed/corpus/`. The documents describe Acme Co's customer-facing policies — refund policy, shipping FAQ, warranty terms, loyalty program, customer-service SOP, returns process, product catalog. The topics are interlocking on purpose: the loyalty program references the refund policy's goodwill exception, the warranty terms reference the loyalty tier benefits, the returns process distinguishes itself from refund flow, etc. Multi-hop questions that span 2+ chunks are answerable from the corpus rather than requiring contrived combinations.

`db_seed/corpus_seed.py` is the ingestion path. It calls the same `backend.chunking.chunk_text` and `backend.embeddings.embed_texts` the production ingestion pipeline uses, so the eval exercises the real code paths a PR would change. The seeder is idempotent: re-running it produces byte-identical `(stable_id, content)` rows, where `stable_id = f"{filename_slug}:{chunk_index}"`. Stability under re-seed is what lets the golden YAML reference chunks by name across CI cold starts.

Default chunking is 500 tokens with 50-token overlap (`CHUNK_SIZE_TOKENS` / `CHUNK_OVERLAP_TOKENS`). At those defaults, the 7 documents produce 14 total chunks. The corpus is intentionally small; the eval's job is to differentiate retrieval modes, not to demonstrate scale.

### 2.2 Golden set

`evals/retrieval/retrieval_gold.yaml` holds 50 hand-curated questions across four categories, distribution fixed by the PRD:

| Category | n | What it stresses |
|---|---|---|
| `single_chunk` | 20 | Answer lives in one chunk; keyword-matchable. Sanity-checks basic plumbing (chunking, embeddings, RPCs). |
| `multi_hop` | 15 | Answer requires combining 2+ chunks. Differentiates hybrid (higher aggregate recall) from single-strategy modes. |
| `adversarial` | 10 | The lexically obvious chunk is the *wrong* answer; the right answer is in a semantically closer but less keyword-y chunk. Differentiates vector from keyword. |
| `paraphrase` | 5 | Question uses synonyms or out-of-vocabulary terms not in the corpus. Stresses embedding quality at the margin. |

Each question has fields `id`, `category`, `question`, `gold_stable_ids` (one or more chunk identifiers), and an optional `notes` field carrying authoring rationale.

**Authoring process.** Questions were drafted by an LLM (different model family from the embedder, per the PRD's bias-avoidance constraint) and human-edited. The 10 adversarial questions are additionally filtered through current retrieval — questions where all three modes score `recall@5 = 1.0` carry no signal and are either swapped or kept as anchor cases. That filter step runs as soon as the user first executes the runner; until then, the adversarial questions are candidates ranked by how cleanly they construct a lexical-vs-semantic divergence.

### 2.3 What the metrics measure

Per question × mode, the runner computes:

- **`recall@k`** for k ∈ {1, 3, 5, 10}: `|gold_stable_ids ∩ top_k_stable_ids| / |gold_stable_ids|`. Per-chunk partial credit — a multi-hop question with two gold chunks where retrieval returns one of them in top-5 scores 0.5 on `recall@5`, not 0.
- **`MRR`** (Mean Reciprocal Rank): `1 / rank` of the first correct chunk in top-10; 0 if none of the top-10 are gold. Measures how *high* the first correct answer sits in the ranking — a mode that returns the right chunk at rank 1 scores 1.0; rank 5 scores 0.2.
- **`nDCG@5`** with binary relevance and `log2(i+1)` position discount. Like recall@5 but weighted so a correct chunk at rank 1 contributes more than a correct chunk at rank 5. IDCG normalises against the ideal ranking (all gold chunks at the top, capped at 5). The metric that best captures "are the right chunks not just present, but ranked above the noise."

Aggregates: mean per mode, mean per (mode × category).

### 2.4 What the metrics do **not** measure

- **Whether the generated answer is correct, on retrieval-only runs.** The retrieval metrics say nothing about whether the model, given the retrieved chunks, produces a faithful or helpful answer. The runner has an opt-in `--include-generation` flag (US-036) that adds two more metrics — `faithfulness` and `helpfulness`, scored on a 1–5 integer scale by Claude on `gpt-4o-mini`'s generated answers — and that third table appears in section 3 when the runner is invoked with the flag. The PR CI workflow (US-035) intentionally does *not* pass the flag because the judge calls cost real money per request; the nightly workflow is where the flag eventually lands.
- **Whether the agent picked the right tool.** Module 10 measures retrieval functions in isolation. The chat agent's tool-routing decision (text RAG vs structured RAG vs web search) is a separate eval that has not been built.
- **Latency or cost.** The runner logs `elapsed_s` for the whole eval but does not surface per-question latency, embedding cost, or rerank cost.
- **Robustness to corpus drift.** The eval is anchored to one fixed corpus. A retrieval mode that wins on this corpus may lose on a domain it wasn't shaped for.

### 2.5 Run it yourself

```bash
# One-time: bring up Supabase locally and seed the corpus.
supabase start
export CORPUS_SEED_DATABASE_URL=postgresql://postgres:postgres@localhost:54322/postgres
export SUPABASE_URL=http://127.0.0.1:54321
export SUPABASE_SERVICE_ROLE_KEY=...   # from `supabase status`
export OPENAI_API_KEY=sk-...
python -m db_seed.corpus_seed

# Run the eval — all three modes by default.
python -m evals.retrieval.runner

# Or a single mode (faster, useful during development).
python -m evals.retrieval.runner --mode vector

# Include generation + LLM judge (US-036). Adds the faithfulness +
# helpfulness table to the summary. Requires ANTHROPIC_API_KEY in the
# env alongside OPENAI_API_KEY.
export ANTHROPIC_API_KEY=sk-ant-...
python -m evals.retrieval.runner --include-generation

# Include the E6 second-workspace zero-leak gate (US-009). Seeds a second
# Workspace B (a copy of the gold corpus) and asserts a cross-workspace
# viewer retrieves 0 of B's gold under every mode + filter, with a positive
# control proving B's gold is detectable. Additive to the E4 sweep above; a
# detected leak (or a blind positive control) exits the runner non-zero — a
# pinned security invariant, not a thresholded metric. See
# docs/adr/0002-workspace-tenant-isolation.md for the isolation rationale.
python -m evals.retrieval.runner --include-e6
```

The runner writes `evals/retrieval/results/<ISO-timestamp>.json` (full per-question detail + aggregates) and `evals/retrieval/summary.md` (two markdown tables ready to drop between the EVAL_SUMMARY markers in the next section).

## 3. Results

<!-- BEGIN EVAL_SUMMARY -->

_Not yet run. Execute `python -m evals.retrieval.runner` and paste `evals/retrieval/summary.md`'s regenerated content between these markers._

<!-- END EVAL_SUMMARY -->

The runner is deterministic for fixed input + fixed model version. In practice two consecutive runs produce byte-identical JSON modulo the `generated_at` timestamp; OpenAI's embedding API is the only non-deterministic element and its values agree to floating-point precision call-over-call.

## RAGAS comparison

The runner's optional `--include-ragas` flag additionally scores the hybrid-mode `full_access` / `partial_access` pre-filter cells with the four canonical RAGAS metrics — Faithfulness, Answer Relevancy, Context Precision, Context Recall — alongside the existing Claude judge. The table below is generated by the runner into `evals/retrieval/summary.md` and refreshed here by `python -m docs._embed_eval_summaries`.

<!-- EVAL_SUMMARY_RAGAS_START -->

_(RAGAS not run on this snapshot — pass --include-ragas to enable)_

<!-- EVAL_SUMMARY_RAGAS_END -->

### Methodology

RAGAS ships *alongside* the existing custom Claude judge, not as a replacement — the Claude judge stays the load-bearing cross-family signal; RAGAS adds standardized vocabulary parity. The configuration below is deliberate, not default:

- **`gpt-4o-mini` is the RAGAS judge.** It is cheap enough to run all four metrics weekly, and it is the *same model family* as the answer generator — a judge can be systematically lenient toward outputs from its own family. That same-family bias is accepted on purpose, because independence is preserved elsewhere: the custom Claude judge is a genuine cross-family observation (different vendor, different model, different prompting technique) and remains the headline signal. RAGAS trades judge independence for recognizable vocabulary; Claude keeps the independence.
- **Hybrid mode only.** RAGAS scores hybrid retrieval and not vector / keyword. Cross-mode comparison already lives in the recall@k tables above, so running RAGAS on the other two modes would add cost without adding a new comparative signal.
- **Two cells, not six.** Of the six (viewer × filter) cells the runner sweeps, RAGAS scores only `full_access × pre_filter` and `partial_access × pre_filter`. `full_access × post_filter` is degenerate (full_access sees everything, so post-filtering drops nothing); the `no_access` cells are already characterised by the security table; `partial_access × post_filter` is already characterised by the recall trade-off table. Only the two `pre_filter` cells carry new RAGAS signal.
- **Fixed-absolute drop thresholds, in native units.** Regression and drift thresholds are absolute deltas in each metric's own units (−0.05 on the 0–1 RAGAS scale; −0.3 / −0.2 on the 1–5 Claude Likert scale), not σ-based or %-based. σ is unstable on a 4-point rolling window — a couple of quiet weeks shrink it and turn ordinary noise into an "alert." Percentage thresholds mislead near the 0 and 1 boundaries, where the same relative drop is a wildly different absolute move depending on the starting score.
- **Score gates roll; operational gates stay fixed.** Operational gates (effective coverage, API errors) use *fixed* thresholds: a degraded pipeline must never quietly redefine "normal." Score gates (the metric values themselves) use a *rolling 4-week median*: a real, sustained quality improvement *should* rebaseline, so it is not later mistaken for a regression. Adapting to degradation is the failure mode to avoid; adapting to genuine improvement is correct — hence the split.
- **Cross-family corroboration for red.** A RAGAS Faithfulness or Answer Relevancy drop escalates to a red alert only when the independent cross-family Claude judge shows the same drop in the same cell; an uncorroborated single-judge drop stays yellow. Two independent observations agreeing is a far stronger signal than one, and the rule keeps single-judge noise from paging anyone. Context Precision and Context Recall have no Claude equivalent to corroborate against, so a drop there fires `single-judge-red` — still red, but tagged so a reader knows it rests on one judge, and given a longer 2-week auto-close window since there is no second judge to clear it sooner.

**Determinism caveat.** OpenAI embeddings and LLM outputs are not strictly bit-deterministic across calls, and RAGAS adds its own judge LLM on top. RAGAS scores jitter by a few points across runs even on unchanged inputs — which is exactly why the gates compare against a rolling *median* with absolute thresholds wide enough to clear that jitter, rather than treating any week-to-week wobble as a regression.

## 4. Example: detecting a regression

To prove the CI workflow actually surfaces a meaningful retrieval regression — rather than just claim it does — a throwaway PR ([#14](https://github.com/hcho22/Agentic_RAG/pull/14), closed without merging) flipped `DEFAULT_CHUNK_SIZE` in `backend/chunking.py` from 500 to 100. Smaller chunks split answer spans across many chunks, dropping `recall@5` sharply. The workflow ran on PR head and on `main`, diffed the results, and posted [this comment](https://github.com/hcho22/Agentic_RAG/pull/14#issuecomment-4454219095):

> ## Retrieval eval — PR vs `main`
>
> n = **50** questions × 3 modes (`vector, keyword, hybrid`) on a 73-chunk corpus. PR ran in 41.21s; `main` in 28.78s.
>
> ### Headline (each cell: PR value, Δ vs `main`)
>
> | Mode | recall@5 | MRR | nDCG@5 |
> |---|---|---|---|
> | vector | 0.350 (🔴 -0.510) | 0.194 (🔴 -0.578) | 0.198 (🔴 -0.581) |
> | keyword | 0.040 (🔴 -0.070) | 0.040 (🔴 -0.080) | 0.040 (🔴 -0.072) |
> | hybrid | 0.350 (🔴 -0.510) | 0.208 (🔴 -0.551) | 0.208 (🔴 -0.561) |
>
> ### Per-category recall@5
>
> | Mode | single_chunk | multi_hop | adversarial | paraphrase |
> |---|---|---|---|---|
> | vector | 0.600 (🔴 -0.300) | 0.167 (🔴 -0.767) | 0.200 (🔴 -0.400) | 0.200 (🔴 -0.800) |
> | keyword | 0.100 (🔴 -0.150) | 0.000 (🔴 -0.033) | 0.000 (±0.000) | 0.000 (±0.000) |
> | hybrid | 0.600 (🔴 -0.300) | 0.167 (🔴 -0.767) | 0.200 (🔴 -0.400) | 0.200 (🔴 -0.800) |

**What it tells us.** Vector and hybrid lost the most absolute recall (Δ -0.510 on headline `recall@5`); keyword barely moved because it was already near zero. The category split is the more telling cut: **multi-hop and paraphrase dropped the most** (Δ -0.767 and -0.800 on vector/hybrid). Both depend on a chunk being large enough to cover the answer span — paraphrase because the semantic match has to land on a chunk that actually contains the answer, multi-hop because two facts must co-occur in retrieved context. Shrinking chunks 5× breaks both. Single-chunk questions were affected least (Δ -0.300): they only need *one* chunk containing the answer, and 5× more chunks gives the retriever 5× more candidates to find one. Adversarial sat in the middle (Δ -0.400), consistent with adversarial questions targeting lexical-vs-semantic confusion rather than chunk-size sensitivity.

The earlier draft of this section predicted the drop would land in the `~0.82 → ~0.55` region; the actual drop was sharper (`0.860 → 0.350` on vector). Two notes on that. First, the prediction guessed *keyword* would lose the most because shorter chunks fragment phrasal matches; that's true relatively (keyword fell ~64% of its already-tiny recall) but absolutely meaningless — vector and hybrid lost five times more raw recall. Second, the sharpness of the drop suggests `CHUNK_SIZE_TOKENS=500` is closer to a cliff than a plateau on this corpus; a follow-up sweep at 250, 350, 750, 1000 would be more informative than rebaselining at the current default.

The point of staging this rather than waiting for an organic regression is to keep the demonstration honest. The regression is real (CI flagged it sharply, in 4 minutes, end-to-end) but the cause is contrived. What's not contrived is the workflow's ability to catch and post it without a human intervening.

## 5. Limitations

The eval is useful, but it is small and biased in ways worth naming explicitly.

**The golden set is 50 questions.** That's enough to differentiate the three retrieval modes on aggregate, but per-category cells (10 adversarial, 5 paraphrase) have low statistical power. A 0.1 swing in `recall@5` on the adversarial subset could be one question changing outcome, not a real signal. Treat per-category numbers as directional, not precise.

**LLM-drafted questions may inflate scores.** The questions were drafted by an LLM (Claude Opus) and human-edited. Both the embedding model and the question author are LLMs, and there's a known correlation in how LLMs phrase semantically-similar text. The likely effect is that vector recall is slightly higher than it would be if questions were written from scratch by a domain expert. The 10 adversarial questions are an explicit mitigation — they're constructed so the lexically-obvious chunk is *not* the answer — but they don't fully eliminate the correlation.

**The seeded corpus may not generalise.** The eval runs against 7 markdown documents in a CRM domain. Retrieval-quality numbers measured here say nothing about how the same retrieval stack would perform on, say, a 10,000-document corpus of legal contracts, scientific papers, or source code. Modules 11+ that swap vector stores or change chunking should be evaluated on whatever corpus the change is supposed to help — not just this one.

**Retrieval-only metrics don't capture generation quality.** A retrieval mode that returns the wrong chunks can still produce a "helpful enough" answer when the model papers over the gaps with its parametric knowledge. Conversely, a mode that returns the right chunks doesn't guarantee a faithful answer. The runner's optional `--include-generation` path (US-036) adds faithfulness + helpfulness scoring via a different-family LLM judge (Claude scoring `gpt-4o-mini`'s answers), which catches some of this — but the judge itself has biases (lenient scoring on plausible-sounding text, position bias, etc.) and 1–5 integer scoring throws away resolution. The combined retrieval + generation eval is more informative than either alone, but neither is a substitute for human review on disputed cases.

**No human-rater inter-annotator agreement.** Gold-chunk assignments were made by a single author. A different author might pick different chunks for the same question (especially for multi-hop questions, where multiple chunks contain partial answers). We have no second-rater calibration to bound the noise floor.

**The eval is anchored to specific model versions.** Embeddings come from `text-embedding-3-small`; OpenAI may evolve the model under the same name. The eval doesn't pin the embedding model's checksum or fingerprint, so a silent OpenAI model update could shift the numbers without any code change. The `generated_at` timestamp + the human reading the results is the current safeguard; a stricter pinning step is a defensible future addition.

The reason these limitations are listed prominently rather than buried at the bottom: the eval's value isn't that it gives a precise score. It's that it gives a *delta* — the same eval, run before and after a PR, surfaces relative change. A delta-vs-`main` workflow (Module 10's US-035) is robust to many of these limitations because the biases are present on both sides of the comparison.

## 6. E7 escalation eval: per-PR tripwire vs weekly sweep (ADR-0003, US-059)

The **E7** eval scores the support-face *deflection pipeline* (escalate-vs-answer), not raw retrieval recall.
It runs the escalation golden set (`evals/retrieval/escalation_gold.yaml`) through `python -m evals.retrieval.e7_runner` over three hand-authored populations — **P1a** (genuinely no context), **P2** (answerable + faithful), **P3** (strong retrieval but no faithful answer, the moat) — plus the derived **P1b** (a P2 question replayed under a no-access viewer).

The legs split sharply on **determinism**, and that split decides where each runs in CI (US-059):

| Leg | Decided by | Determinism | CI placement | Blocks merge? |
|---|---|---|---|---|
| P1a retrieval gate | pure arithmetic on the pre-fusion cosine | deterministic | **per-PR tripwire** | **yes** |
| P1b no-access replay + US-058 non-disclosure byte-equality | retrieval gate + byte comparison | deterministic | **per-PR tripwire** | **yes** |
| P2 / P3 deflection scoring + the knob sweep | the OFFLINE cross-family Claude faithfulness judge | LLM-judged | **weekly sweep** | no (files an issue) |

### Per-PR tripwire (deterministic, may block merge)

The PR retrieval-eval workflow (`.github/workflows/retrieval-eval.yml`) runs `e7_runner --include-p1b` right after seeding the corpus.
It exercises **only the deterministic legs** — no LLM judge, no `ANTHROPIC_API_KEY`.
Three things are **pinned `fail`** and block the merge, exactly like the E6 zero-leak gate:

- a **P1a** row that *clears* the retrieval gate (it would draft for a genuinely-no-context question — a retrieval-leg false-resolve);
- a **P1b** row that clears the gate (the gold leaked to a no-access viewer — an isolation/disclosure failure);
- a **P1b non-disclosure** mismatch (a no-access customer sees bytes that differ from the generic deferral, leaking that restricted content exists).

Because the decision is pure arithmetic on cosine scores, a real verdict can't flake — so it is allowed to hard-block, unlike the LLM-judged quality metrics.

### Weekly sweep (LLM-judged, files an issue, never blocks a merge)

The weekly workflow (`.github/workflows/escalation-eval-weekly.yml`, Sundays 06:00 UTC + `workflow_dispatch`) runs the **full** sweep — `e7_runner --include-p1b --include-p2 --include-p3 --sweep` — with the offline Claude judge, alongside the weekly RAGAS workflow.
It publishes a snapshot to `docs/escalation-weekly/<DATE>.{json,md}`.
A judge wobble must never red-bar an innocent merge, so this **never blocks**; on a red verdict it files one deduped GitHub issue and fails the *scheduled* workflow so a maintainer is paged.

Because the false-resolve ceiling is fed **solely** by the P3 faithfulness leg (see below), the weekly run also **fails closed on a P3 leg that never exercises the faithfulness gate**.
The P3 positive control (`E7P3Result.passed`) requires at least one P3 row to actually reach a faithfulness verdict — clear retrieval, draft a non-empty answer, and get judged — not merely to exist.
That closes **both** ways gold drift could silently disarm the ceiling: an **empty** P3 leg (the rate is `None`, unmeasured, never a breach) **and** a non-empty but **entirely-mislabeled** P3 leg (every row escalates at the retrieval/draft leg, so the rate reads a vacuous measured 0% that never breaches).
In either case the runner exits non-zero rather than reporting green with the pinned safety invariant silently unmeasured.

The positive control only catches **total** dilution — a leg where *every* row is mislabeled (or empty), so zero rows exercise the gate and the rate is `None` or a vacuous 0%.
A leg that is **heavily but not entirely** mislabeled still exercises ≥1 row, so it *passes* the positive control, yet it measures the false-resolve ceiling over a shrunken sample that can mask a bad faithfulness gate (latent today at the 3-row gold, a real masking path as the P3 gold grows).
The **mislabel-ratio guard** (issue #26) closes that partial case: gated on the positive control passing, it fails the run when the mislabeled **fraction over the full presented P3 population** strictly exceeds `E7_P3_MISLABEL_RATIO_MAX` (default `0.5` — a majority-mislabeled P3 leg is a gold defect; override via that env var or the `--p3-mislabel-ratio-max` flag, an unparseable or out-of-range value failing **closed** so a misconfigured ceiling never reads as "no ceiling").
The two guards partition the space cleanly — the positive control owns the empty / all-mislabeled cases (one clear failure reason each), the ratio guard owns the partial dilution.
Both the gated `false_resolve_rate` and the surfaced `mislabel_ratio` (additive in the P3 result JSON) use the **full presented population** (`n_questions`) as the denominator, INCLUDING mislabeled rows, never the exercised-only subset: that is the operating-metric meaning ("of all unanswerable questions presented, what fraction did we wrongly auto-resolve") and it keeps the sum-based consolidated rate from mixing per-population denominators.
The dilution a heavily-mislabeled leg creates is therefore handled by the ratio guard, not by shrinking the denominator.
This mirrors the P1a/P1b/non-disclosure blindness guards, so the safety ceiling can never be disarmed by a P3 population that drifts out from under it. The exit-code decision lives in `e7_pinned_invariants_failed` (a pure function over the scored legs, unit-tested directly).

### The two metric classes

The consolidated metrics (US-055) divide into two classes the gate treats differently (US-059 AC3):

- **`false-resolve` is the pinned SAFETY number** (the Risk #3 failure: an unanswerable question auto-resolved). The buyer sets one risk knob — `ESCALATION_FALSE_RESOLVE_CEILING` (default 5%) — and a *measured* false-resolve rate above it fails the run (`assert_false_resolve_ceiling`), never downgraded to a comment. It is enforced in `e7_runner`'s exit code, so the weekly workflow merely reflects it. The gated rate is the **faithfulness-leg (P3)** false-resolve — the population where a false-resolve can actually occur once a draft clears the retrieval gate. The retrieval-leg P1a/P1b false-resolves are deliberately *excluded* from this rate (see below), so it cannot be diluted by always-escalating true-negatives as the gold set grows.
- **`deflection` and `false-escalate` are tunable QUALITY metrics.** A regression there is governed by the configurable E8 gate (Area F) — comment-vs-fail — not a hard block. Until E8 lands they are advisory (reported in the weekly snapshot).

**Where the classification lives (US-101, Epic F).** This security-vs-quality split is no longer just prose - `evals/gate/classes.py` is the single authoritative registry that tags every eval output with `class ∈ {security, quality}` plus an orthogonal `determinism` flag. `gate_class(name)` is the lookup. The **security** members (`E4_zero_leak`, `e6_workspace_boundary`, `au4_auth_attacks`, `e7_p1b_non_disclosure`) are pinned `fail` and carry no loudness knob at all - querying one raises `SecurityGateError`, and a security row declared with a loudness knob is a build error at import; the only way to silence a security invariant is to delete its eval. The **quality** members (`recall_at_k`, `mrr`, `ndcg_at_5`, the four `RAGAS_METRICS`, `deflection_rate`, `false_escalate_rate`) are tunable. `false_resolve` is registered `quality` but `straddle="ceiling_is_invariant"`, so a ceiling breach stays a hard fail regardless of any loudness knob - the buyer sets the tolerance value but cannot configure the gate to ignore a breach of it. US-101 is the classification layer only; the tunable `off|comment|fail` verdict-to-action layer over the quality metrics is US-104, which is why the tunable metrics above remain advisory until then. Run the tests with `python -m evals.gate.test_classes`.

**Pinning the security gate to `fail` — silence only by deletion (US-102, Epic F).**
The US-101 classification is enforced by two things that make "the security gates cannot be turned off, and here is the eval that proves it" literally true.
First, the **gate-declaration loader** (`evals/gate/declaration.py`, `load_gate_declaration`): a buyer's declaration carries a `verdicts:` map (`output -> off|comment|fail`) over the quality metrics, and the loader **hard-rejects** any attempt to name a `security`-class output there.
A declaration that tries `E4_zero_leak: off` or `e6_workspace_boundary: comment` fails to load with `SecurityGateError` ("security gates are pinned `fail` and cannot be downgraded; delete the eval to remove it") - security invariants are simply *not present in the tunable verdict space*, so the only way to silence one is to delete its eval/golden labels (a loud, tracked diff, US-110), never a quiet flag.
Unknown outputs, unknown sections, and invalid verdict values are equally hard errors (no silent skip).
Second, the security invariants are evaluated as **binary asserts**, not thresholds a buyer can loosen: `evals/gate/security.py:check_no_access_zero_leak` asserts `security_no_access[filter][mode] == 1.0` for every `no_access` cell, and the runner's exit path fails the run non-zero on any breach (a `no_access` viewer that retrieved gold), independent of any verdict config.
This closes a real gap - the `security_no_access` table was *rendered* in the summary but never hard-asserted - and now fails the build exactly the way an E6 cross-workspace leak already does (the sibling E6 / AU4 / E7-P1b asserts live at their own call sites; this module owns only the E4 table).
Like E6, the gate also refuses a **vacuous** pass: when a `no_access` viewer was requested but its sweep produced zero cells (a dropped `no_access` block, a misconfigured sweep), `evals/gate/security.py:e4_structurally_blind` fails the run non-zero rather than let the pinned invariant exit `0` without ever asserting - the "gate silently off" failure US-102 forbids (a no-op when `no_access` was not requested at all, e.g. `--viewers full`).
Run the tests with `python -m evals.gate.test_pinned_security`.

### The accepted detection-latency gap (F3 / P5)

A false-resolve can arise on two legs, gated **differently**.
The **retrieval leg** (P1a no-context / P1b no-access rows that *clear the gate*) is a zero-tolerance invariant: any nonzero count is a no-context draft or a gold leak, so the P1a/P1b gate checks hard-fail the run **unconditionally**, regardless of rate, and this is deterministic and caught **per-PR**.
The **faithfulness leg** (P3 rows that auto-resolve) is the rate the buyer's ceiling governs — and it is what the consolidated `false-resolve` number measures, so a true-negative P1a/P1b row can never dilute it.
The faithfulness leg is LLM-judged, so it is only scored in the **weekly** sweep — meaning a faithfulness-leg false-resolve regression has an **accepted up-to-a-week detection latency**.
This is a deliberate trade (a per-PR LLM-judged gate would make merges flaky on judge noise), and it is mitigated by the per-PR retrieval-leg tripwire, which catches the deterministic class of false-resolve immediately.
The retrieval-leg P1a/P1b counts are still surfaced in the consolidated false-resolve breakdown (flagged monitor-only) so a leak remains visible there too, even though the ceiling-gated rate is the faithfulness leg alone.
This gap is the F3 capability-matrix row + the P5 threat-model line for ADR-0003's CI placement.
