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
```

The runner writes `evals/retrieval/results/<ISO-timestamp>.json` (full per-question detail + aggregates) and `evals/retrieval/summary.md` (two markdown tables ready to drop between the EVAL_SUMMARY markers in the next section).

## 3. Results

<!-- BEGIN EVAL_SUMMARY -->

_Not yet run. Execute `python -m evals.retrieval.runner` and paste `evals/retrieval/summary.md`'s regenerated content between these markers._

<!-- END EVAL_SUMMARY -->

The runner is deterministic for fixed input + fixed model version. In practice two consecutive runs produce byte-identical JSON modulo the `generated_at` timestamp; OpenAI's embedding API is the only non-deterministic element and its values agree to floating-point precision call-over-call.

## 4. Example: detecting a regression

_This section will be filled in once the CI workflow lands in US-035. The plan, labelled honestly as a staged demonstration rather than an organic catch:_

The intent is to open a throwaway PR that flips `CHUNK_SIZE_TOKENS` in `backend/chunking.py` from 500 to 100 (titled `test: reduce CHUNK_SIZE_TOKENS from 500 to 100 (do not merge)`), let the CI workflow run the retrieval eval against the changed chunking, and capture the comment it posts on the PR. Short chunks split answer spans across many chunks, dropping `recall@5` meaningfully — expected to be in the ~0.82 → ~0.55 region depending on which mode you look at. The PR is then closed without merging; the closed PR + the captured comment serve as the artifact embedded here.

The point of including this in the writeup isn't to brag about catching a problem — it's to make concrete what a regression *looks like* in the CI comment, so a reviewer evaluating the repo sees both the happy-path numbers (section 3) and the unhappy-path mechanic in one place.

Once US-035 lands, this section will contain:

- A link to the closed regression PR.
- The auto-posted CI comment as a quoted markdown block.
- A short paragraph on which mode lost the most recall (likely keyword, since shorter chunks fragment phrasal matches), and what that tells you about the relationship between chunk size and retrieval quality.

## 5. Limitations

The eval is useful, but it is small and biased in ways worth naming explicitly.

**The golden set is 50 questions.** That's enough to differentiate the three retrieval modes on aggregate, but per-category cells (10 adversarial, 5 paraphrase) have low statistical power. A 0.1 swing in `recall@5` on the adversarial subset could be one question changing outcome, not a real signal. Treat per-category numbers as directional, not precise.

**LLM-drafted questions may inflate scores.** The questions were drafted by an LLM (Claude Opus) and human-edited. Both the embedding model and the question author are LLMs, and there's a known correlation in how LLMs phrase semantically-similar text. The likely effect is that vector recall is slightly higher than it would be if questions were written from scratch by a domain expert. The 10 adversarial questions are an explicit mitigation — they're constructed so the lexically-obvious chunk is *not* the answer — but they don't fully eliminate the correlation.

**The seeded corpus may not generalise.** The eval runs against 7 markdown documents in a CRM domain. Retrieval-quality numbers measured here say nothing about how the same retrieval stack would perform on, say, a 10,000-document corpus of legal contracts, scientific papers, or source code. Modules 11+ that swap vector stores or change chunking should be evaluated on whatever corpus the change is supposed to help — not just this one.

**Retrieval-only metrics don't capture generation quality.** A retrieval mode that returns the wrong chunks can still produce a "helpful enough" answer when the model papers over the gaps with its parametric knowledge. Conversely, a mode that returns the right chunks doesn't guarantee a faithful answer. The runner's optional `--include-generation` path (US-036) adds faithfulness + helpfulness scoring via a different-family LLM judge (Claude scoring `gpt-4o-mini`'s answers), which catches some of this — but the judge itself has biases (lenient scoring on plausible-sounding text, position bias, etc.) and 1–5 integer scoring throws away resolution. The combined retrieval + generation eval is more informative than either alone, but neither is a substitute for human review on disputed cases.

**No human-rater inter-annotator agreement.** Gold-chunk assignments were made by a single author. A different author might pick different chunks for the same question (especially for multi-hop questions, where multiple chunks contain partial answers). We have no second-rater calibration to bound the noise floor.

**The eval is anchored to specific model versions.** Embeddings come from `text-embedding-3-small`; OpenAI may evolve the model under the same name. The eval doesn't pin the embedding model's checksum or fingerprint, so a silent OpenAI model update could shift the numbers without any code change. The `generated_at` timestamp + the human reading the results is the current safeguard; a stricter pinning step is a defensible future addition.

The reason these limitations are listed prominently rather than buried at the bottom: the eval's value isn't that it gives a precise score. It's that it gives a *delta* — the same eval, run before and after a PR, surfaces relative change. A delta-vs-`main` workflow (Module 10's US-035) is robust to many of these limitations because the biases are present on both sides of the comparison.
