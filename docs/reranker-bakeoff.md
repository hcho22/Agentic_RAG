# Reranker bake-off (US-117)

Eval-only measurement of each available reranker backend on top of the hybrid retrieval leg, so the reranker recommendation is data-backed rather than assumed.
This changed **no default**: `RERANKER` stays `none`, and nothing on the widget deflection path is touched (its escalation gate is calibrated on raw cosine; any reranker there is a separate, E7-gated effort per PRD Non-Goals).

## What was measured

The eval runner (`evals/retrieval/runner.py`) gained a `--reranker {none,llm,cohere,voyage}` flag.
It reuses the exact production path — retrieve `max(RERANK_INPUT_K, TOP_K)` candidates, apply `build_reranker(...)`, trim to `TOP_K` — that `backend/main.py::_retrieve_for_agent` runs, so the deltas below describe the real agent pipeline, not a runner-only reimplementation.
`--reranker none` is a true pass-through: its output is byte-identical (modulo timestamps) to a no-flag run, and the chosen backend is recorded in the results JSON under `"reranker"`.

Backends covered in this run:

- `none` — control (NullReranker pass-through). Committed: [`hybrid-none-control.json`](reranker-bakeoff/hybrid-none-control.json).
- `llm` — LLM-as-reranker on the answerer model (`gpt-4o-mini` here). Committed: [`hybrid-llm.json`](reranker-bakeoff/hybrid-llm.json).
- `cohere` / `voyage` — **not measured**: no `COHERE_API_KEY` / `VOYAGE_API_KEY` in the local environment. Rerun the commands below with those keys set to complete the matrix.

### How it was run

```
# control (pass-through) and llm reranker, hybrid mode / full viewer
python -m evals.retrieval.runner --mode hybrid --viewers full \
  --reranker none --out docs/reranker-bakeoff/hybrid-none-control.json
python -m evals.retrieval.runner --mode hybrid --viewers full \
  --reranker llm  --out docs/reranker-bakeoff/hybrid-llm.json
# add cohere/voyage once keys are available:
#   --reranker cohere  (needs COHERE_API_KEY)
#   --reranker voyage  (needs VOYAGE_API_KEY)
```

Corpus: the committed seed corpus (8 docs / 16 chunks) reseeded via `python -m db_seed.corpus_seed`; golden set: 60 questions across five categories.

## Results — hybrid mode, full viewer, 60-question golden set

Ranking-quality metrics (MRR and nDCG@5 are the reranker-sensitive ones; recall@5 shown for context).
Every number traces to the committed JSON named above (`aggregates.by_mode_category.hybrid`).

| Category      | MRR none | MRR llm | ΔMRR    | nDCG@5 none | nDCG@5 llm | ΔnDCG@5 | R@5 none | R@5 llm |
| ------------- | -------- | ------- | ------- | ----------- | ---------- | ------- | -------- | ------- |
| single_chunk  | 0.870    | 1.000   | +0.130  | 0.885       | 1.000      | +0.115  | 0.950    | 1.000   |
| multi_hop     | 0.933    | 0.889   | -0.044  | 0.927       | 0.919      | -0.007  | 1.000    | 1.000   |
| **adversarial** | 0.623  | 0.675   | +0.052  | 0.658       | 0.732      | +0.074  | 0.800    | 0.900   |
| paraphrase    | 0.767    | 0.767   | +0.000  | 0.826       | 0.826      | +0.000  | 1.000    | 1.000   |
| **lexical**   | 1.000    | 1.000   | +0.000  | 1.000       | 1.000      | +0.000  | 1.000    | 1.000   |
| **OVERALL**   | 0.858    | 0.899   | +0.041  | 0.872       | 0.921      | +0.049  | 0.950    | 0.983   |

The two categories the PRD flags as the reranker's reason to exist are bolded:

- **adversarial** — the LLM reranker delivers the intended win: MRR +0.052, nDCG@5 +0.074, and recall@5 lifts 0.800 → 0.900. This is exactly the category where RRF's rank-position fusion is coarsest, and a stronger relevance signal helps.
- **lexical** — already saturated at 1.000 for both backends. US-116's adaptive-alpha fusion (weighting the lexical leg up on identifier-style queries) already nails exact-token lookups, so the reranker has nothing left to gain here.

Elsewhere: `single_chunk` improves sharply (MRR +0.130), `paraphrase` is unchanged, and `multi_hop` regresses slightly (MRR −0.044) — the reranker occasionally reorders a multi-hop chunk set away from the ideal ordering even though recall@5 stays 1.000.

## Latency

The `llm` reranker adds one serial LLM call per query.
Per-call rerank latency ranged roughly 0.6–2.2s (median ~1.2s), with 1 of 60 calls exceeding the 2s `RERANK_LATENCY_WARN_SECONDS` threshold.
End-to-end wall-clock for the 60-question run was **343.6s with `llm` vs 12.6s for the `none` control** (`elapsed_s` in the committed JSONs) — a ~27× increase dominated by the added serial model call.
`cohere`/`voyage` hosted cross-encoders are expected to be materially faster per call, but were not measured here.

Caveat: the local corpus is only 16 chunks, so many queries retrieve fewer than the 20-candidate `RERANK_INPUT_K` pool; a production-scale corpus with deeper pools would exercise the reranker (and its latency) harder. The numbers here are directional but run against the kit's own committed golden set, so they are representative of what CI sees.

## Recommendation

**Keep `RERANKER=none` as the default.** The LLM reranker produces a real adversarial and single_chunk gain, but:

1. **Lexical is already solved** by US-116's adaptive alpha — the reranker adds zero there, removing the strongest a-priori argument for it.
2. **Multi-hop regresses** slightly, so the improvement is not uniform across categories.
3. **The latency cost is severe** (~1.2s median added per query) on an interactive assistant path, and the reranker is explicitly barred from the widget deflection path (raw-cosine escalation gate, E7-gated — PRD Non-Goals).
4. **The matrix is incomplete** — Cohere/Voyage, the purpose-built cross-encoders that would be the real production candidates, were not measurable without API keys.

**Observability going forward:** the nightly workflow (`.github/workflows/retrieval-eval-nightly.yml`) now publishes a `hybrid+llm` reranker run beside the baseline (`docs/nightly/<date>-rerank-llm.json`), so the reranker delta is tracked over time as the corpus and golden set evolve.

**Before any default change:** rerun the bake-off with `COHERE_API_KEY` / `VOYAGE_API_KEY` to measure the hosted cross-encoders (lower latency, purpose-built for reranking). If one shows a large, uniform adversarial win at acceptable latency, that motivates a follow-up story to wire it into the **assistant** path only — the widget deflection path remains a separate, E7-gated decision.
