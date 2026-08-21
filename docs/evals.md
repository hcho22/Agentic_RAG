# Retrieval Evals: measuring vector, keyword, and hybrid search

## 1. Why this exists

The `search_documents` tool in `backend/retrieval.py` exposes three retrieval modes — vector-only (`match_chunks` over pgvector HNSW), keyword-only (Postgres full-text search through the `keyword_search` RPC), and hybrid (both, fused via Reciprocal Rank Fusion). They've been wired up since Module 6 (US-020 / US-021). What we did **not** have, until Module 10, was a way to say *how good* any of them are.

Without measurement, claims about retrieval quality are vibes. "Hybrid is better than vector-only" is not falsifiable until you can put a number on it. "Adding a reranker improves recall" is not falsifiable without a recall metric. And — most consequentially for the future modules — "swapping pgvector for Pinecone changes quality by X%" is not a comparison you can make if X is undefined.

Module 10 is the infrastructure that lets future modules ship with real numbers instead of "trust me, it works." This document describes what we measure, how, where the eval falls short, and what a regression looks like when CI catches one.

## 2. Methodology

### 2.1 Corpus

The eval runs against a fixed text corpus of 8 markdown documents committed to the repo under `db_seed/corpus/`. The documents describe Acme Co's customer-facing policies and developer-integration reference — refund policy, shipping FAQ, warranty terms, loyalty program, customer-service SOP, returns process, product catalog, and an API & integration error reference (error codes, config keys, header names - the identifier-dense doc the `lexical` questions target, US-113). The topics are interlocking on purpose: the loyalty program references the refund policy's goodwill exception, the warranty terms reference the loyalty tier benefits, the returns process distinguishes itself from refund flow, etc. Multi-hop questions that span 2+ chunks are answerable from the corpus rather than requiring contrived combinations.

`db_seed/corpus_seed.py` is the ingestion path. It calls the same `backend.chunking.chunk_text` and `backend.embeddings.embed_texts` the production ingestion pipeline uses, so the eval exercises the real code paths a PR would change. The seeder is idempotent: re-running it produces byte-identical `(stable_id, content)` rows, where `stable_id = f"{filename_slug}:{chunk_index}"`. Byte-identical rows keep the eval reproducible across CI cold starts; the golden set itself no longer references chunks by name but anchors on answer-bearing content that the runner resolves to the current `stable_id`s at eval time (§2.2), so a re-chunk needs no re-labeling.

Default chunking is 500 tokens with 50-token overlap (`CHUNK_SIZE_TOKENS` / `CHUNK_OVERLAP_TOKENS`). At those defaults, the 8 documents produce 16 total chunks. The corpus is intentionally small; the eval's job is to differentiate retrieval modes, not to demonstrate scale.

### 2.2 Golden set

`evals/retrieval/retrieval_gold.yaml` holds 60 hand-curated questions across five categories, distribution fixed by the PRD:

| Category | n | What it stresses |
|---|---|---|
| `single_chunk` | 20 | Answer lives in one chunk; keyword-matchable. Sanity-checks basic plumbing (chunking, embeddings, RPCs). |
| `multi_hop` | 15 | Answer requires combining 2+ chunks. Differentiates hybrid (higher aggregate recall) from single-strategy modes. |
| `adversarial` | 10 | The lexically obvious chunk is the *wrong* answer; the right answer is in a semantically closer but less keyword-y chunk. Differentiates vector from keyword. |
| `paraphrase` | 5 | Question uses synonyms or out-of-vocabulary terms not in the corpus. Stresses embedding quality at the margin. |
| `lexical` | 10 | Exact-token queries - error codes, config keys, header names, quoted literal phrases - that appear in the query verbatim. Instruments the keyword (lexical) leg so the US-114 OR-fallback and US-116 adaptive-fusion changes are measurable (US-113). |

Each question has fields `id`, `category`, `question`, `gold_anchors` (one or more gold labels), and an optional `notes` field carrying authoring rationale.

**Gold labels are content anchors, not chunk indices (US-107).** Each entry in `gold_anchors` is an *answer-bearing span* — a quoted string that actually appears in the corpus (or a `{text, doc}` mapping that restricts resolution to one document by `filename_slug`). At eval time `evals/retrieval/content_anchors.py` resolves each anchor to whichever chunk `stable_id`(s) currently *contain* its text (whitespace-normalized, otherwise exact — never fuzzy) and injects the resolved `gold_stable_ids` the scorer consumes; the `{filename_slug}:{chunk_index}` id survives only as that resolved internal representation, never authored. Three consequences: a `chunk_size`/overlap/docling re-seed needs **zero** re-labeling (the same anchor re-resolves to the new indices); a span in the chunker's overlap region is a verbatim substring of both adjacent chunks and resolves to **both** (the multi-gold recall scorer handles it — the shipped `q07` return-fee anchor is this case); and an anchor that matches no current chunk is a **hard error** naming the question id + anchor text, never a silent `recall=0` — editing the source content so the quoted span no longer appears breaks the anchor by design.

**The layered golden set (US-108).** The kit ships **one** layered golden-set format so a buyer's authoring burden scales with the faces they ship, and the permission matrix can never drift from the retrieval gold:

- **Base layer** (every buyer): the `question → gold_anchors + category` primitive above — the minimal thing to author. Loaded by `runner.load_questions`.
- **Derived for free** (zero extra authoring): from the gold labels alone the runner builds the three E4 viewer setups — `full_access` (owner), `partial_access = gold ∪ N filler`, `no_access = all_non_gold` (the top-level `viewer_construction` block + `runner.compute_visible_stable_ids`) — *and* the E7 **P1b** population (the same question replayed under a `no_access` viewer). The buyer hand-writes neither a permission test nor a P1b case; labeling gold once yields both.
- **Support-face layer** (support buyers only): one optional `escalation` label per question — `no_context` (P1a), `answerable_faithful` (P2), or `should_escalate` (P3) — authored in `evals/retrieval/escalation_gold.yaml` on the **same** content-anchor primitive (§6), never a second gold format. A knowledge-assistant-only buyer omits it: `runner.load_questions` treats the layer as optional, so a base-only golden set loads and runs the base + derived-for-free layers without error (a *present-but-typo'd* label is still rejected fail-closed). A support golden set additionally runs the escalation suite. P2-vs-P3 is the only authoring judgment that cannot be derived ("does a faithful answer exist from these chunks?"). Pinned by `python -m evals.retrieval.test_us108_layered_golden_set`.

**Authoring process.** Questions were drafted by an LLM (different model family from the embedder, per the PRD's bias-avoidance constraint) and human-edited. The 10 adversarial questions are additionally filtered through current retrieval — questions where all three modes score `recall@5 = 1.0` carry no signal and are either swapped or kept as anchor cases. That filter step runs as soon as the user first executes the runner; until then, the adversarial questions are candidates ranked by how cleanly they construct a lexical-vs-semantic divergence.

**Authoring your own golden set (US-109).** `docs/golden-set-authoring.md` is the buyer-facing guide for replacing this set with one built on your own corpus. It teaches the **completeness contract**: because `no_access = all_non_gold` and `partial_access = gold ∪ N filler`, an un-labeled relevant chunk is silently reclassified as safe-to-disclose and produces a **false security pass the green table won't reveal**, so exhaustive gold labeling is load-bearing for the *security* claim, not merely recall. It also states loudly that a **single-family eval is a weaker proof** (same-family judge bias) that must not be cited to a client as "proven," and actively recommends the cross-family corroboration below (§"RAGAS comparison"). The shipped set is a format template to learn from, not a survives-the-swap artifact - its content anchors fail loud on a corpus swap by design (US-107).

### 2.3 What the metrics measure

Per question × mode, the runner computes:

- **`recall@k`** for k ∈ {1, 3, 5, 10}: `|gold_stable_ids ∩ top_k_stable_ids| / |gold_stable_ids|`. Per-chunk partial credit — a multi-hop question with two gold chunks where retrieval returns one of them in top-5 scores 0.5 on `recall@5`, not 0.
- **`MRR`** (Mean Reciprocal Rank): `1 / rank` of the first correct chunk in top-10; 0 if none of the top-10 are gold. Measures how *high* the first correct answer sits in the ranking — a mode that returns the right chunk at rank 1 scores 1.0; rank 5 scores 0.2.
- **`nDCG@5`** with binary relevance and `log2(i+1)` position discount. Like recall@5 but weighted so a correct chunk at rank 1 contributes more than a correct chunk at rank 5. IDCG normalises against the ideal ranking (all gold chunks at the top, capped at 5). The metric that best captures "are the right chunks not just present, but ranked above the noise."

All three divide by `|gold_stable_ids|`, so all three are **undefined** - not zero - when a question's resolved gold set is empty. They raise `EmptyGoldError` in that case rather than reporting a number, which is what keeps a genuine `0.000` (gold existed, retrieval missed it) readable as the real measurement it is. See §5, "An empty gold set refuses the run".

Aggregates: mean per mode, mean per (mode × category).

### 2.4 What the metrics do **not** measure

- **Whether the generated answer is correct, on retrieval-only runs.** The retrieval metrics say nothing about whether the model, given the retrieved chunks, produces a faithful or helpful answer. The runner has an opt-in `--include-generation` flag (US-036) that adds two more metrics — `faithfulness` and `helpfulness`, scored on a 1–5 integer scale by Claude on `gpt-5.6-luna`'s generated answers — and that third table appears in section 3 when the runner is invoked with the flag. The PR CI workflow (US-035) intentionally does *not* pass the flag because the judge calls cost real money per request; the nightly workflow is where the flag eventually lands.
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

# Rerank the retrieved candidates before scoring (US-117), reusing the same
# path production runs in backend/main.py::_retrieve_for_agent: pull
# max(RERANK_INPUT_K, TOP_K) candidates, rerank, trim to TOP_K. `none` (the
# default) is a true pass-through - output is identical to a no-flag run.
# The chosen backend is recorded in the results JSON under "reranker".
# `llm` needs OPENAI_API_KEY; `cohere`/`voyage` need COHERE_API_KEY /
# VOYAGE_API_KEY. Eval-only: the production RERANKER default stays `none`.
# Measured results + recommendation: docs/reranker-bakeoff.md.
python -m evals.retrieval.runner --mode hybrid --reranker llm

# Eval-harness guards (US-107 / invariant 12). Fully offline - no Supabase, no
# secrets, no network - so they run anywhere. They pin the rule that a harness
# which measured nothing refuses to publish a number for it, while a genuine miss
# still scores 0.000.
python -m evals.retrieval.test_empty_gold_guard          # authored gold (E4/E6 scorers)
python -m evals.permissions_scale.test_degenerate_guard  # derived gold (scale runner)

# Answer/faithfulness-gate guards (issue #104), offline for the same reason - the
# two gate modules drive fake judge clients, and the rubric module reads the
# eval-side rubric via `ast` rather than importing the runner. These three plus
# the two above are the five modules the per-PR `eval-harness-guards` job runs.
python -m backend.test_answer_gate_rubric   # runtime/offline answer rubrics stay in LOCKSTEP (§6)
python -m backend.test_answer_gate          # answer gate: JUDGE_TEMPERATURE pin sent, fail-closed paths
python -m backend.test_faithfulness_gate    # same for the faithfulness gate + the boot-warning scoping
```

`test_answer_gate_rubric` additionally has a LIVE layer that makes real judge calls to check the rubric still *discriminates* (a recorded fixture would only replay a canned verdict).
It skips cleanly with no keys, so CI runs only its offline lockstep half - run it with `OPENAI_API_KEY` + `ANTHROPIC_API_KEY` set whenever you edit either rubric.

The runner writes `evals/retrieval/results/<ISO-timestamp>.json` (full per-question detail + aggregates) and `evals/retrieval/summary.md` (two markdown tables ready to drop between the EVAL_SUMMARY markers in the next section).

### 2.6 Seeding your own corpus: the generic seeder (US-110)

`db_seed/corpus_seed.py` (§2.1) seeds the shipped demo corpus into a fixed owner and Default Workspace so the example golden set resolves reproducibly.
When you replace the demo corpus with your **own** documents, use the **generic seeder** `db_seed/generic_seed.py` instead - it is the same production chunk-and-embed path, generalized to a corpus you can run against **production** data without polluting it with synthetic test principals.

```bash
export GENERIC_SEED_DATABASE_URL=postgresql://postgres:postgres@localhost:54322/postgres
export OPENAI_API_KEY=sk-...   # or your EMBEDDER_* provider

# Owner-only corpus: documents + chunks, an empty chunk_acl (no one but the
# owner sees them). No manifest needed.
python -m db_seed.generic_seed --docs-dir ./my_corpus --seed-label acme

# With real workspaces / principals / grants from a manifest.
python -m db_seed.generic_seed --docs-dir ./my_corpus \
    --manifest ./db_seed/manifest.example.yaml --seed-label acme
```

Two invariants make it safe against a production corpus:

- **It seeds a corpus (+ optional real grants) and nothing eval-specific.** It reads a folder of `*.md` / `*.txt` documents plus an **optional** manifest describing **real** workspaces, principals (users / groups + memberships), and document→principal grants, then runs the unchanged `chunk_text` + `embed_texts` paths and inserts `documents` + `chunks` (+ real `chunk_acl` / `workspace_membership` / group rows from the manifest). See `db_seed/manifest.example.yaml` for the format. A grant is expanded to one `chunk_acl` row per chunk of its document, exactly as the app's share action does.
- **It never bakes eval scaffolding into the seed.** The synthetic eval viewers (`partial_access` / `no_access`) and the derived `full/partial/no_access` ACL matrix are **not** a seed concept - they are constructed transiently by the **runner** at run time (`evals/retrieval/runner.py::ensure_viewer_users` / `reset_viewer_acls`) from your gold labels (§4 of `docs/golden-set-authoring.md`) and reset per question. A production seed therefore carries **zero** test principals. The seeder has no reference to those constants at all; the guarantee is structural, pinned by `python -m db_seed.test_generic_seed`.

With no manifest the corpus is owner-only (empty `chunk_acl`), consistent with the no-backfill rollout.
The seeder is idempotent and byte-stable across re-seeds the same way `corpus_seed.py` is, scoped by `--seed-label` so a buyer seed never clobbers the demo corpus (`corpus_seed=true`) or another labelled seed.

Replacing the corpus and authoring a new golden set are the **same step** (US-107): the demo golden set's content anchors are quoted from the demo docs, so they fail loud against your corpus by design - see `docs/golden-set-authoring.md` (US-109) to author a set on your own content.

### 2.7 The kit ships green out of the box (US-111)

Day-zero, a fresh `seed → eval` on the **shipped** artifacts alone - the default 8-doc / 16-chunk e-commerce corpus (`db_seed/corpus/`), its content-anchored golden set (`evals/retrieval/retrieval_gold.yaml`, §2.2), and the default gate (`evals/gate/gate.yaml`, §"Buyer-authored gate declaration") - reproduces the **1.000 E4 no-leak security table** with **zero buyer authoring**.
There is nothing to write before the first eval can run: the 60 golden anchors are quoted from the shipped docs so they all resolve (`python -m evals.retrieval.test_content_anchors`), and the default `gate.yaml` reproduces today's gate constants byte-for-byte (`python -m evals.gate.test_gate_bindings`).

A dead or drifted demo corpus embarrasses the quickstart (the P3 build-in-public demo), so the kit's **own** CI keeps the promise honest.
`.github/workflows/ship-green.yml` runs the clean `python -m db_seed.corpus_seed` → `python -m evals.retrieval.runner --viewers all` on every change to the green-determining surface **and on `main`**, then hard-asserts the `security_no_access` table is `1.000` across every `no_access` cell (reusing the same pinned `evals/gate/security.py` invariant the runner asserts internally, with a `e4_structurally_blind` guard that refuses a vacuous pass).
It is a separate workflow from the PR delta comment (`retrieval-eval.yml`, an advisory *diff* vs main) and the nightly snapshot (`retrieval-eval-nightly.yml`): this one is the **absolute** "does the shipped kit still ship green" assertion, and by also triggering on `push: main` it catches a demo that rots directly on main.
It is E4-only by design - E6 cross-workspace is already the per-PR hard gate in `retrieval-eval.yml`, and the LLM-judged quality legs are scheduled-only (US-105), so pulling them in here would add cost and flake without serving this job's one question.

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

These cell, threshold, and corroboration values are the kit defaults declared in `evals/gate/gate.yaml` (US-103, § 6): the gate *algorithms* above are fixed, but their *bindings* are buyer-configurable in that declaration, so a buyer points the same gates at their own cells / thresholds / judge families without forking `evals/retrieval/ragas_gates.py`.

**Determinism caveat.** OpenAI embeddings and LLM outputs are not strictly bit-deterministic across calls, and RAGAS adds its own judge LLM on top. RAGAS scores jitter by a few points across runs even on unchanged inputs — which is exactly why the gates compare against a rolling *median* with absolute thresholds wide enough to clear that jitter, rather than treating any week-to-week wobble as a regression.

**A weekly run that publishes nothing is UNMEASURED, not green.** `retrieval-eval-ragas-weekly.yml` treats a missing snapshot as its own state - a deduped `ragas-weekly-harness-failure` issue plus a failed job, never a red gate finding - because the rolling-median gates above are only as current as the last run that actually produced a snapshot. Details: § 6, "A scheduled run has three outcomes, not two".

## 4. Example: detecting a regression

To prove the CI workflow actually surfaces a meaningful retrieval regression — rather than just claim it does — a throwaway PR ([#14](https://github.com/hcho22/Purvia/pull/14), closed without merging) flipped `DEFAULT_CHUNK_SIZE` in `backend/chunking.py` from 500 to 100. Smaller chunks split answer spans across many chunks, dropping `recall@5` sharply. The workflow ran on PR head and on `main`, diffed the results, and posted [this comment](https://github.com/hcho22/Purvia/pull/14#issuecomment-4454219095):

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

**The golden set is 60 questions.** That's enough to differentiate the three retrieval modes on aggregate, but per-category cells (10 adversarial, 5 paraphrase, 10 lexical) have low statistical power. A 0.1 swing in `recall@5` on the adversarial subset could be one question changing outcome, not a real signal. Treat per-category numbers as directional, not precise.

**LLM-drafted questions may inflate scores.** The questions were drafted by an LLM (Claude Opus) and human-edited. Both the embedding model and the question author are LLMs, and there's a known correlation in how LLMs phrase semantically-similar text. The likely effect is that vector recall is slightly higher than it would be if questions were written from scratch by a domain expert. The 10 adversarial questions are an explicit mitigation — they're constructed so the lexically-obvious chunk is *not* the answer — but they don't fully eliminate the correlation.

**The seeded corpus may not generalise.** The eval runs against 8 markdown documents in a CRM domain. Retrieval-quality numbers measured here say nothing about how the same retrieval stack would perform on, say, a 10,000-document corpus of legal contracts, scientific papers, or source code. Modules 11+ that swap vector stores or change chunking should be evaluated on whatever corpus the change is supposed to help — not just this one.

**Retrieval-only metrics don't capture generation quality.** A retrieval mode that returns the wrong chunks can still produce a "helpful enough" answer when the model papers over the gaps with its parametric knowledge. Conversely, a mode that returns the right chunks doesn't guarantee a faithful answer. The runner's optional `--include-generation` path (US-036) adds faithfulness + helpfulness scoring via a different-family LLM judge (Claude scoring `gpt-5.6-luna`'s answers), which catches some of this — but the judge itself has biases (lenient scoring on plausible-sounding text, position bias, etc.) and 1–5 integer scoring throws away resolution. The combined retrieval + generation eval is more informative than either alone, but neither is a substitute for human review on disputed cases.

**No human-rater inter-annotator agreement.** Gold-chunk assignments were made by a single author. A different author might pick different chunks for the same question (especially for multi-hop questions, where multiple chunks contain partial answers). We have no second-rater calibration to bound the noise floor.

**An empty gold set refuses the run; a genuine miss still scores 0.000.** These are different facts and the harness no longer reports them as the same number. `recall_at_k` / `ndcg_at_5` / `mrr` used to `return 0.0` for an empty resolved gold set, so an authoring mistake arrived at the gate wearing the shape of a retrieval regression and dragged the gated E4/E6 means down with it. All three now raise `EmptyGoldError` (`evals/retrieval/content_anchors.py`, beside `ZeroResolveError`), and the refusal is planted at each depth where the degeneracy first becomes knowable: `resolve_gold_anchors` before any query is issued, `_metrics_block` naming the offending question / mode / viewer / filter, and each scorer. E6 gets the same treatment through a single pre-flight that resolves both gold projections (`b_gold_for` / `a_gold_for`) before a single live query, so no question can burn its queries ahead of a blind one - its instance was the sharper one, because E6's negative assertion *is* `recall@10 == 0.0` against Workspace B's gold, so an empty B-gold set used to score a PASS on a tenant-isolation invariant the run never exercised. What has not changed is the load-bearing distinction: gold that exists and was missed is a real measurement and still scores 0.000. Both halves are pinned offline (no DB, no secrets) by `python -m evals.retrieval.test_empty_gold_guard`, which - together with its permissions-scale sibling - runs per-PR in the `eval-harness-guards` job of `retrieval-eval.yml`, so the guard against a silently-green harness is itself enforced by CI rather than by a command in a markdown file. Content anchoring (§2.2) remains the first line of defence on the authoring path - `parse_gold_anchors` rejects a question with no anchors, and an anchor that resolves to nothing raises `ZeroResolveError`. This is the authored-gold half of invariant 12 in `AGENTS.md`; the sibling permissions-scale runner enforces the derived-gold half via `DegenerateRunError`.

**The eval is anchored to specific model versions.** Embeddings come from `text-embedding-3-small`; OpenAI may evolve the model under the same name. The eval doesn't pin the embedding model's checksum or fingerprint, so a silent OpenAI model update could shift the numbers without any code change. The `generated_at` timestamp + the human reading the results is the current safeguard; a stricter pinning step is a defensible future addition.

The reason these limitations are listed prominently rather than buried at the bottom: the eval's value isn't that it gives a precise score. It's that it gives a *delta* — the same eval, run before and after a PR, surfaces relative change. A delta-vs-`main` workflow (Module 10's US-035) is robust to many of these limitations because the biases are present on both sides of the comparison.

## 6. E7 escalation eval: per-PR tripwire vs weekly sweep (ADR-0003, US-059)

The **E7** eval scores the support-face *deflection pipeline* (escalate-vs-answer), not raw retrieval recall.
It runs the escalation golden set (`evals/retrieval/escalation_gold.yaml`) through `python -m evals.retrieval.e7_runner` over three hand-authored populations — **P1a** (genuinely no context), **P2** (answerable + faithful), **P3** (strong retrieval but no grounded answer that answers the question, the moat) — plus the derived **P1b** (a P2 question replayed under a no-access viewer).

The P2/P3 legs mirror the runtime deflection pipeline's **two** content gates (`_run_judged_leg`), not one: after the retrieval gate and draft, an offline cross-family Claude judge scores **faithfulness** (`runner.judge_answer`) and, on the would-be-answered path, **answer-completeness** (`runner.judge_answering`, the issue-#97 gate). A P3 draft is caught by whichever content gate it fails: an ungrounded draft trips the faithfulness leg, and a grounded-but-non-answering draft — a faithful "the corpus doesn't cover jewelry" deferral, which the drafter is *instructed* to produce rather than guess — trips the answer leg. Both are the moat working; only a draft that is faithful **and** answers auto-resolves. Modeling the answer leg is the **issue-#96 fix**: before it, every grounded P3 deferral scored 5/5 faithful and auto-resolved as a false-resolve, so the P3 leg read a spurious 100% false-resolve and breached the ceiling even though the runtime pipeline (post issue #97) correctly escalates every one of those drafts.

**Issue #104 sharpened what the answer leg asks.** As first written the rubric caught the draft that ANNOUNCES its own ignorance, and missed the case where the *corpus's* answer is itself a deferral. The 2026-08-03 sweep false-resolved `e7-p3-05` and `e7-p3-11` on exactly that: `returns-process` says over-20-lbs return shipping is "quoted per-case" and `warranty-terms` puts a book claim past 30 days "at the discretion of customer service", so a faithful restatement is fluent, lands on the asked slot, and still leaves the customer holding nothing. The rubric now states that a reply reporting only that the value is case-by-case / discretionary / unpublished does not answer. Two consequences for anyone editing it: the rubric here and `escalation._ANSWER_JUDGE_SYSTEM_PROMPT` must move in LOCKSTEP (they are independent strings by design, and they were measured disagreeing on 5 of 17 probes before this was pinned), and `backend/test_answer_gate_rubric.py` is the layer that checks a rubric edit still discriminates — the weekly sweep used to be the only one. Its offline half (the lockstep check) runs per-PR in the `eval-harness-guards` job of `retrieval-eval.yml`, triggered by a change to either rubric or to the guard itself; its live-judge half needs `OPENAI_API_KEY` + `ANTHROPIC_API_KEY` and is run by hand.

This golden set is the **support-face layer** of the one layered format (§2.2, US-108): each row carries one `escalation` label, and the P2/P3 gold is authored as **US-107 content anchors** (`gold_anchors`, bare span or `{text, doc}`) on the same primitive as the base retrieval gold — not the legacy `gold_stable_ids` chunk-index list. `e7.resolve_escalation_gold` resolves those anchors to the current chunk `stable_id`(s) at eval time (fail-loud on zero-resolve) in the `--include-p1b` path, so the no-access P1b replay revokes exactly the resolved gold and a `chunk_size`/overlap re-seed needs zero re-labeling here too. A `no_context` (P1a) row carries no anchor by definition.

The legs split sharply on **determinism**, and that split decides where each runs in CI (US-059):

| Leg | Decided by | Determinism | CI placement | Blocks merge? |
|---|---|---|---|---|
| P1a retrieval gate | pure arithmetic on the pre-fusion cosine | deterministic | **per-PR tripwire** | **yes** |
| P1b no-access replay + US-058 non-disclosure byte-equality | retrieval gate + byte comparison | deterministic | **per-PR tripwire** | **yes** |
| P2 / P3 deflection scoring + the knob sweep | the OFFLINE cross-family Claude faithfulness **and** answer-completeness judges | LLM-judged | **weekly sweep** | no (files an issue) |

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

Because the false-resolve ceiling is fed **solely** by the P3 content leg (see below), the weekly run also **fails closed on a P3 leg that never exercises the content gates**.
The P3 positive control (`E7P3Result.passed`) requires at least one P3 row to actually reach a content-gate verdict — clear retrieval, draft a non-empty answer, and get judged — not merely to exist.
That closes **both** ways gold drift could silently disarm the ceiling: an **empty** P3 leg (the rate is `None`, unmeasured, never a breach) **and** a non-empty but **entirely-mislabeled** P3 leg (every row escalates at the retrieval/draft leg, so the rate reads a vacuous measured 0% that never breaches).
In either case the runner exits non-zero rather than reporting green with the pinned safety invariant silently unmeasured.

The positive control only catches **total** dilution — a leg where *every* row is mislabeled (or empty), so zero rows exercise the gate and the rate is `None` or a vacuous 0%.
A leg that is **heavily but not entirely** mislabeled still exercises ≥1 row, so it *passes* the positive control, yet it measures the false-resolve ceiling over a shrunken sample that can mask a bad content gate.
On the main leg that path is **dormant today but reachable**, not permanently latent: at the default τ_sim of 0.4 (`backend/escalation.py`) all 9 P3 rows clear the retrieval gate, so the measured ratio is 0/9 and the guard never fires.
Promote `ESCALATION_TAU_SIM` to 0.5 and the 5 rows measuring below it (`e7-p3-01` 0.4607, `e7-p3-02` 0.4214, `e7-p3-03` 0.4298, `e7-p3-07` 0.4234, `e7-p3-09` 0.4570) fall out at the retrieval gate, taking the ratio to **at least** 5/9 ≈ 56% — a lower bound, since rows can additionally fall out on `n_cleared < n_min` — over the default ceiling.
The **mislabel-ratio guard** (issue #26) closes that partial case *on the main leg*: gated on the positive control passing, it fails the run when the mislabeled **fraction over the full presented P3 population** strictly exceeds `E7_P3_MISLABEL_RATIO_MAX` (default `0.5` — a majority-mislabeled P3 leg is a gold defect; override via that env var or the `--p3-mislabel-ratio-max` flag, an unparseable or out-of-range value failing **closed** so a misconfigured ceiling never reads as "no ceiling").
The two guards partition the space cleanly — the positive control owns the empty / all-mislabeled cases (one clear failure reason each), the ratio guard owns the partial dilution.
Both the gated `false_resolve_rate` and the surfaced `mislabel_ratio` (additive in the P3 result JSON) use the **full presented population** (`n_questions`) as the denominator, INCLUDING mislabeled rows, never the exercised-only subset: that is the operating-metric meaning ("of all unanswerable questions presented, what fraction did we wrongly auto-resolve") and it keeps the sum-based consolidated rate from mixing per-population denominators.
The dilution a heavily-mislabeled leg creates is therefore handled by the ratio guard, not by shrinking the denominator.
This mirrors the P1a/P1b/non-disclosure blindness guards, so the safety ceiling can never be disarmed by a P3 population that drifts out from under it. The exit-code decision lives in `e7_pinned_invariants_failed` (a pure function over the scored legs, unit-tested directly).

**Neither guard applies inside the knob sweep, so read its curve with the `P3 exercised` column.**
Both guards above govern the *main leg* — the single operating point the run is scored at.
The US-056 sweep re-runs the legs at every grid point, and `_select_knee` filters only on `false_resolve <= ceiling` then maximizes deflection; it never consults the positive control or the mislabel ratio.
That matters because τ_sim is the knob most able to fake a good number: raising it pushes P3 rows below the retrieval gate, where they are `mislabeled` and can no longer be tallied a false-resolve, so the rate falls toward 0 without the content gates having improved at all.
A grid point whose P3 rows *all* fall out is the vacuous 0% the main leg's positive control exits non-zero on — which means the sweep can recommend an operating point the runner's own pinned invariants would then reject.
Each `SweepPoint` therefore carries `p3_n_exercised` / `p3_n_mislabeled` / `p3_mislabel_ratio` / `p3_vacuous`, rendered as a `P3 exercised` column in the weekly report, and a vacuous or majority-mislabeled knee is called out explicitly above the recommendation (the majority-mislabeled callout is measured against the ceiling actually in force for that run, so `--p3-mislabel-ratio-max` moves it in both directions).
`p3_vacuous` covers both halves of the "measured nothing" shape exactly as the main leg's positive control does - the point whose P3 rows all escalated early, *and* the run that presented no P3 rows at all - with `p3_absent` split out so the callout can name the right remedy (author P3 rows, versus keep the existing ones above τ_sim); a sweep whose every point is `p3_absent` is reported as "no knee - and nothing was measured", not as a red curve.
The same fields ride in the JSON snapshot - on each point, on the `curve_points` list, and on `recommended_defaults` - so a script promoting the knee's knobs straight off the JSON sees the same warning the markdown prose carries.
This is **reporting, not enforcement** — the knee is still selected on the ceiling alone, so gating on it would be a separate, visible change.
The authoring-side counterpart is in `docs/golden-set-authoring.md`: a P3 population clustered below the top of the swept τ_sim grid cannot falsify a τ_sim recommendation, so keep P3 rows spanning the grid.
The already-published `docs/escalation-weekly/2026-07-31.{md,json}` predates both halves and is the worked example: it recommends τ_sim=0.5 on a false-resolve 0% that no P3 row was measured at.
Published snapshots are generated artifacts and are never hand-edited, so that correction is filed as a dated erratum in `docs/escalation-weekly/README.md`; the `P3 exercised` column appears from the next weekly run onward.
Pinned by `test_sweep_reports_p3_exercise_per_point` in `evals/retrieval/test_e7_runner.py`.

### A scheduled run has three outcomes, not two: green, red, and UNMEASURED

Every guard above assumes the run got far enough to score something.
The runner writes its JSON + markdown **before** it exits non-zero on a pinned invariant, so a snapshot is publishable for any failure *on the gate path* - but that is not a guarantee that a snapshot exists at all.
Anything that kills the process before scoring (an invalid `ANTHROPIC_API_KEY`, a seed failure, an OOM, a job that dies before the runner step ever runs) produces none, and a run in that state has produced no verdict.
Both weekly workflows (`escalation-eval-weekly.yml` and `retrieval-eval-ragas-weekly.yml`) therefore name that as its own state rather than folding it into either of the other two.
A dedicated snapshot-check step runs first; on a miss it emits an `UNMEASURED` annotation, skips publish, files a deduped `e7-weekly-harness-failure` / `ragas-weekly-harness-failure` issue stating that nothing was evaluated this week, and fails the job.
The "reflect runner exit code" step is gated on the snapshot as well as the exit code, so a harness crash can never be captioned "a pinned safety invariant fired" - describing the very thing that was *not* evaluated.
On the next run that does produce a snapshot the harness-failure issue auto-closes; unlike the gate-finding auto-close that is **not** restricted to scheduled runs, because "did this run produce a snapshot" is a fact about that run alone rather than a verdict a partial re-run could misrepresent.
Both workflows previously `exit 0`'d on a missing snapshot, which is why the E7 gate had never filed a single issue and the RAGAS weekly stopped publishing while both went red week after week.
This is the CI half of invariant 12 in `AGENTS.md`: the path that should shout must never be the path that goes quiet.

### Scheduled snapshots publish via an auto-merging PR, not a direct push

Every scheduled eval workflow that writes a snapshot to `docs/` (`retrieval-eval-nightly.yml`, `permissions-scale-eval.yml`, `retrieval-eval-ragas-weekly.yml`, `escalation-eval-weekly.yml`) publishes it by opening a **docs-only pull request to `main` that auto-merges**, never by `git push`-ing to `main` directly.
This is the prerequisite for the required-status-check ruleset on `main`: a required check gates *every* update to `main`, so a nightly's direct push (which carries no PR and therefore no checks) would be rejected and the public snapshots would stop updating.
A docs-only PR instead trips the required checks as **skipped = pass** (the `retrieval-eval.yml` per-job path filters match none of the `docs/**` snapshot paths), so it merges cleanly with enforcement fully intact - no bypass.
The PR is opened and set to auto-merge (`gh pr merge --auto --squash --delete-branch`) with a **GitHub App installation token** (the **eval-publish** App, minted fresh on every run by an `actions/create-github-app-token` step from the `EVAL_PUBLISH_APP_ID` / `EVAL_PUBLISH_APP_PRIVATE_KEY` repo secrets), **not** the default `GITHUB_TOKEN`: GitHub blocks recursive workflow runs, so a PR opened by `GITHUB_TOKEN` would never trigger the required checks and would sit unmergeable forever.
The App token is what ends the recurring outage: unlike the fine-grained PAT it replaced, it is re-minted each run and never expires, while still being a non-`GITHUB_TOKEN` identity so the required checks still fire.
The snapshot commit is pushed to a fresh per-run branch (`eval-publish/<slug>-<date>-<run_id>-<run_attempt>`) so concurrent and re-run jobs never collide (`--delete-branch` cleans the branch up once the PR merges), and the "no changes → no PR" guard (`git diff --cached --quiet`) is preserved.
If the App secrets are absent/invalid the mint step **fails loudly** on its own, and if "Allow auto-merge" is off the publish step fails rather than falling back to a direct push - a silent fallback would defeat the whole ruleset.
Should the minted token ever resolve empty, `gh pr create` would otherwise die with the opaque "gh: ... set the GH_TOKEN environment variable" (exit 4), so each of the five publish steps (the nightly baseline + its `continue-on-error` reranker step, the permissions-scale nightly, and the two weeklies) runs a **named-cause preflight** just before `gh pr create`: an empty token emits an explicit `::error::` annotation naming the `EVAL_PUBLISH_APP_ID` / `EVAL_PUBLISH_APP_PRIVATE_KEY` secrets and the exact `eval-publish/...` branch the snapshot is now stranded on, then fails the step.
A slow expiry of that kind is the concrete failure that stranded 17 nights of receipts on push branches unnoticed for a week from 2026-08-12 while `main` froze under the old expiring PAT - the App token removes that expiry class, but the preflight and alarm below stay to catch any other publish failure.
Every one of these workflows also carries an **`Alarm on stranded eval publish`** step, distinct from the harness-failure step (§ 6): harness-failure means NO snapshot was produced (**UNMEASURED**), whereas this fires when a snapshot WAS produced but could not be published to `main`.
On any publish-step failure it files a deduped, auto-closing `<prefix>-publish-failure` issue (`retrieval-nightly-`, `permissions-scale-`, `ragas-weekly-`, `e7-weekly-`), reusing the harness-failure convention (REST `?labels=` listing rather than the eventually-consistent GraphQL search, drop PRs via the `pull_request` key, `label create --force`, dedup before filing, close on a healthy publish).
It runs under the default `GITHUB_TOKEN`, never the minted eval-publish App token, so it can still file the alarm even when the App credentials are exactly what is missing, and it never fails the job on its own (a failed publish is already red, and the nightly reranker publish is deliberately `continue-on-error`, so its failure must stay non-blocking); it gates on the publish step's `outcome` so a runner/seed crash is left to the harness-failure step instead.
This is why `retrieval-eval-nightly.yml` and `permissions-scale-eval.yml` carry `issues: write` alongside `contents: write` (the two weekly workflows already had it).
Any NEW scheduled workflow that publishes a snapshot must follow this shape, not add a direct push to `main`, and must carry the same preflight + alarm.

### The two metric classes

The consolidated metrics (US-055) divide into two classes the gate treats differently (US-059 AC3):

- **`false-resolve` is the pinned SAFETY number** (the Risk #3 failure: an unanswerable question auto-resolved). The buyer sets one risk knob — `ESCALATION_FALSE_RESOLVE_CEILING` (default 5%) — and a *measured* false-resolve rate above it fails the run (`assert_false_resolve_ceiling`), never downgraded to a comment. It is enforced in `e7_runner`'s exit code, so the weekly workflow merely reflects it. The gated rate is the **content-leg (P3)** false-resolve — the population where a false-resolve can actually occur once a draft clears the retrieval gate (a draft that clears both the faithfulness gate and the issue-#97 answer gate). The retrieval-leg P1a/P1b false-resolves are deliberately *excluded* from this rate (see below), so it cannot be diluted by always-escalating true-negatives as the gold set grows.
- **`deflection` and `false-escalate` are tunable QUALITY metrics.** A regression there is governed by the configurable E8 gate (Area F) - the escalation suite's `off|comment|fail` loudness knob (US-104, below) over their `red`/`yellow` severity, not a hard block. They are also **non-deterministic** (LLM-judged), so US-105's determinism rule structurally places them scheduled-only - a `fail` there fails the weekly workflow + files an issue, never a per-PR merge block. The knob + placement layers ship in `evals/gate/verdict.py` / `evals/gate/placement.py` (with `GateDeclaration.blocks_merge` / `files_issue` as the consuming seam); until those predicates are wired onto the escalation runner's own exit / comment call sites they remain advisory (reported in the weekly snapshot).

**Where the classification lives (US-101, Epic F).** This security-vs-quality split is no longer just prose - `evals/gate/classes.py` is the single authoritative registry that tags every eval output with `class ∈ {security, quality}` plus an orthogonal `determinism` flag. `gate_class(name)` is the lookup. The **security** members (`E4_zero_leak`, `e6_workspace_boundary`, `au4_auth_attacks`, `e7_p1b_non_disclosure`) are pinned `fail` and carry no loudness knob at all - querying one raises `SecurityGateError`, and a security row declared with a loudness knob is a build error at import; the only way to silence a security invariant is to delete its eval. The **quality** members (`recall_at_k`, `mrr`, `ndcg_at_5`, the four `RAGAS_METRICS`, `deflection_rate`, `false_escalate_rate`) are tunable. `false_resolve` is registered `quality` but `straddle="ceiling_is_invariant"`, so a ceiling breach stays a hard fail regardless of any loudness knob - the buyer sets the tolerance value but cannot configure the gate to ignore a breach of it. US-101 is the classification layer only; the tunable `off|comment|fail` verdict-to-action layer over the quality metrics is US-104 (below). Run the tests with `python -m evals.gate.test_classes`.

**Pinning the security gate to `fail` — silence only by deletion (US-102, Epic F).**
The US-101 classification is enforced by two things that make "the security gates cannot be turned off, and here is the eval that proves it" literally true.
First, the **gate-declaration loader** (`evals/gate/declaration.py`, `load_gate_declaration`): a buyer's declaration carries a `verdicts:` map (`output -> off|comment|fail`) over the quality metrics, and the loader **hard-rejects** any attempt to name a `security`-class output there.
A declaration that tries `E4_zero_leak: off` or `e6_workspace_boundary: comment` fails to load with `SecurityGateError` ("security gates are pinned `fail` and cannot be downgraded; delete the eval to remove it") - security invariants are simply *not present in the tunable verdict space*, so the only way to silence one is to delete its eval/golden labels (a loud, tracked diff, US-110), never a quiet flag.
Unknown outputs, unknown sections, and invalid verdict values are equally hard errors (no silent skip).
Second, the security invariants are evaluated as **binary asserts**, not thresholds a buyer can loosen: `evals/gate/security.py:check_no_access_zero_leak` asserts `security_no_access[filter][mode] == 1.0` for every `no_access` cell, and the runner's exit path fails the run non-zero on any breach (a `no_access` viewer that retrieved gold), independent of any verdict config.
This closes a real gap - the `security_no_access` table was *rendered* in the summary but never hard-asserted - and now fails the build exactly the way an E6 cross-workspace leak already does (the sibling E6 / AU4 / E7-P1b asserts live at their own call sites; this module owns only the E4 table).
Like E6, the gate also refuses a **vacuous** pass: when a `no_access` viewer was requested but its sweep produced zero cells (a dropped `no_access` block, a misconfigured sweep), `evals/gate/security.py:e4_structurally_blind` fails the run non-zero rather than let the pinned invariant exit `0` without ever asserting - the "gate silently off" failure US-102 forbids (a no-op when `no_access` was not requested at all, e.g. `--viewers full`).
Run the tests with `python -m evals.gate.test_pinned_security`.

**Project bindings in a buyer-authored declaration (US-103, Epic F).**
The same gate declaration also carries the RAGAS gates' *project bindings* under a `bindings:` section that `evals/gate/gate.yaml` ships as the kit default: the cells to gate, the threshold constants (the −0.05 RAGAS drop, the 0.96 coverage floor, the API-error ceiling, the coverage-drift band, the minimum rolling-window sizes), and the cross-family judge map / cell.
The detection *algorithms* in `evals/retrieval/ragas_gates.py` (fixed floors, rolling-median drift, cross-family corroboration, `single-judge-red`, severity, `auto_close_weeks`) are unchanged; only these bindings moved out of module constants into a `GateBindings` config object the three detection functions take, so a buyer describes *their* cells / thresholds / judge families without forking the detector.
The shipped `gate.yaml` reproduces today's constants byte-for-byte, so the declaration-driven path is identical to the legacy hardcoded one; an unknown cell / metric / section is a hard load error, never a silent skip.
Cross-family corroboration is the one binding that is *not* inherited when omitted: a custom `bindings:` block that leaves out the `corroboration:` sub-block (or sets `judge_family == generator_family`) runs single-family, degrading every RAGAS drop to `single-judge-red` (AC4).
Run the tests with `python -m evals.gate.test_gate_bindings`.

**The per-suite loudness knob — `off|comment|fail` over severity (US-104, Epic F).**
The last gate piece is `evals/gate/verdict.py`: a thin, **detector-agnostic** layer that maps a finding's `(severity, knob)` to a CI *action* under one buyer knob per quality suite.
It detects nothing - the `red`/`yellow` findings are computed exactly as before; the knob only changes how loud a finding is, never *which* findings exist (`verdict_action` is a pure read; the findings are never mutated).
The `(severity, knob) → action` table: `fail` ⇒ red **blocks** (fails the run / merge) and yellow **comments**; `comment` ⇒ both comment and nothing blocks; `off` ⇒ nothing posts; default `comment`.
So the two postures the repo already ships are two values of one knob - the weekly `runner.py::amain` red→exit-non-zero posture is `fail`, and the PR `ci/diff_results.py` comment-only posture is `comment`.
The knob is **per quality suite**, not a single global flag: the three suites - `retrieval_metrics` (recall@k / mrr / ndcg), `ragas` (the four RAGAS scores + coverage gates), `escalation` (deflection / false-escalate / false-resolve) - partition every *quality* output, and a buyer can run RAGAS at `fail` weekly while the retrieval-metrics suite stays `comment`.
A `security`-class output is in **no** suite (pinned `fail`, no knob - US-102), and `false_resolve`'s **ceiling breach** is a pinned invariant that ignores the knob entirely: a ceiling-breach finding (tag `false-resolve-ceiling`) always maps to `block`, so the buyer sets the ceiling *value* (US-050) but cannot configure the gate to ignore a breach of their own tolerance.
The knob lives in the gate declaration's optional `suites:` section (`evals/gate/gate.yaml`); an unknown suite or knob is a hard load error, and the shipped default omits the section so every suite stays at `comment`.
A finer per-*output* `verdicts:` entry (US-102) still wins over its suite's knob (`GateDeclaration.action_for_finding` / `resolve_knob`).
US-104 ships the verdict layer and the declaration seam; the per-PR-vs-scheduled determinism split that decides which of those `fail`s may block a merge is US-105 (below).
Run the tests with `python -m evals.gate.test_verdict`.

**Determinism decides merge-blocking - the one-rule four-workflow split (US-105, Epic F).**
The last gate piece answers a different question from loudness: not *how loud* a finding is, but *where it runs and what its `fail` may block*.
The rule is the **determinism axis, not buyer preference** - `evals/gate/placement.py` is its single source of truth (`placement_for(gate)`), and the whole four-workflow split reduces to it:

| Determinism | Gates | CI placement | A `fail` there |
|---|---|---|---|
| **deterministic** | recall@k / MRR / nDCG, the pinned E4 / E6 / AU4 / E7-P1b invariants, the deterministic retrieval-gate tripwire (pure arithmetic / binary asserts) | **per-PR** (`retrieval-eval.yml`) | **blocks the merge** (non-zero exit on the `pull_request` workflow) |
| **non-deterministic** | the four RAGAS scores, the runtime faithfulness gate, the full E7 P2/P3 deflection + false-resolve sweep (all LLM-judged) | **scheduled** (`retrieval-eval-ragas-weekly.yml` / `retrieval-eval-nightly.yml` / `permissions-scale-eval.yml` + `escalation-eval-weekly.yml`) | **fails the scheduled workflow + files one issue per tag**, never a merge block |

Two enforcement points make the rule structural rather than prose:

- **The loader rejects a per-PR `fail` on a non-deterministic gate.**
  A gate declaration's optional `per_pr:` section names the *deterministic* quality gates (a suite or output) the buyer opts into per-PR merge-blocking (`per_pr: {recall_at_5: fail}`).
  Naming a **non-deterministic** target there - `per_pr: {faithfulness: fail}`, or a whole `ragas` / `escalation` suite - is a **structural load error** (`evals/gate/placement.py::PlacementError`, a `ValueError` subclass), rejected before the run ever starts, exactly like a security downgrade (US-102).
  A judge wobble must never red-bar an innocent merge, so the config simply *cannot express* a per-PR `fail` on an LLM-judged gate.
  A security output is rejected too (it is pinned `fail` and blocks per-PR through its own binary assert, carrying no tunable knob); an unknown target or a non-`fail` value is a hard error.
- **A new per-PR AU4 job.**
  `retrieval-eval.yml` already runs E4 (the `security_no_access` binary assert), E6 (the cross-workspace boundary), and the deterministic E7 P1a/P1b retrieval-leg tripwire as hard-blocking per-PR gates.
  US-105 adds the API-layer auth-attack suite (`backend/test_au4_auth_attacks.py`) as a **new per-PR job** (`au4-security-invariants`) so a cross-workspace leak at the API edge also fails before merge.
  It is a separate job (not a step) because it imports `backend/main.py` (docling/torch) - the heavy stack `requirements-ci.txt` avoids - so it gets its own runner's disk budget; its assertions are exact `== 0` / `rejected` binary checks (no judge), so it is allowed to hard-block.

Loudness (`suites:` / `verdicts:`) and placement (`per_pr:`) stay orthogonal: `GateDeclaration.blocks_merge(finding)` answers "does this deterministic red finding block the *merge* per-PR?" (True only for a deterministic gate opted into `per_pr:` - a non-deterministic finding can **never** block a merge, the load-bearing guarantee), while `GateDeclaration.files_issue(finding)` answers "does it fail the *scheduled* run and file an issue?" (the same non-zero exit, but scheduled).
So `false_resolve`'s **pinned ceiling** breach - always the `block` action (US-104 AC3) but a *non-deterministic* metric - files an issue on the scheduled run and is **never** a per-PR merge block: that is exactly the accepted content-leg detection-latency gap (F3 / P5, below), whose per-PR mitigation is the deterministic P1a/P1b retrieval-leg tripwire.
The shipped default `gate.yaml` omits `per_pr:`, so no quality gate blocks the merge via config - today's posture (retrieval metrics stay advisory per-PR, US-035; the security invariants block per-PR through their own asserts).
Run the tests with `python -m evals.gate.test_placement`.

### The accepted detection-latency gap (F3 / P5)

A false-resolve can arise on two legs, gated **differently**.
The **retrieval leg** (P1a no-context / P1b no-access rows that *clear the gate*) is a zero-tolerance invariant: any nonzero count is a no-context draft or a gold leak, so the P1a/P1b gate checks hard-fail the run **unconditionally**, regardless of rate, and this is deterministic and caught **per-PR**.
The **content leg** (P3 rows that auto-resolve — a draft that cleared BOTH the faithfulness gate and the issue-#97 answer gate) is the rate the buyer's ceiling governs — and it is what the consolidated `false-resolve` number measures, so a true-negative P1a/P1b row can never dilute it.
The content leg is LLM-judged, so it is only scored in the **weekly** sweep — meaning a content-leg false-resolve regression has an **accepted up-to-a-week detection latency**.
This is a deliberate trade.
A blocking **per-PR LLM-faithfulness gate** was the rejected alternative: it would make merges flaky on judge noise *and* charge per-push judge spend - the wrong trade for a starter kit, where a green PR must stay a deterministic signal and a quickstart cannot depend on a paid judge on every push.
The gap is mitigated by the per-PR retrieval-leg tripwire, which catches the deterministic class of false-resolve immediately.
The retrieval-leg P1a/P1b counts are still surfaced in the consolidated false-resolve breakdown (flagged monitor-only) so a leak remains visible there too, even though the ceiling-gated rate is the content leg alone.

This gap is recorded here as the eval-domain's inline F3 row + P5 line (the "standing sink" discipline - each domain writes its own row, the consolidated F3/P5 doc gathers them later; see the PRD "F3 capability-matrix rows owed", D/F).

**Capability matrix (F3) row.** _Safety-critical content-gate regressions (E7 **P3** - strong retrieval but no grounded answer that answers: an unfaithful draft OR a grounded deferral, issue #97) have an accepted **up-to-a-week detection latency**: the false-resolve **content leg** is LLM-judged and scored only on the weekly sweep, never per-PR. The merge path catches only the deterministic **retrieval leg** (weak-retrieval / no-context / no-access escalations) via the per-PR retrieval-gate tripwire. Not fully caught per-PR by design._ (Cross-ref: CONTEXT "Escalation signal & deflection pipeline (Phase 2, ADR-0003)".)

**Threat model (P5) line.** _The false-resolve safety invariant is enforced on two legs with different detection latencies. The **retrieval leg** (P1a no-context / P1b no-access rows that clear the gate) is deterministic and hard-blocks the merge **per-PR** (zero latency - the retrieval-leg tripwire). The **content leg** (P3 auto-resolves, the ceiling-gated rate) is **LLM-judged and scored only on the weekly sweep**, so a content-leg regression is merge-then-catch with **up-to-a-week latency**, mitigated only by that deterministic retrieval-leg tripwire on the merge path. The rejected alternative - a blocking per-PR LLM-faithfulness gate - would trade a deterministic green PR for flaky merges plus per-push judge spend, wrong for a starter kit._ (Cross-ref: CONTEXT "Escalation signal & deflection pipeline (Phase 2, ADR-0003)".)
