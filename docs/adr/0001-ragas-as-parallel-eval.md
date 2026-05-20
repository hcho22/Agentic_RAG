# ADR 0001: RAGAS as a parallel eval signal

- **Status:** Accepted
- **Date:** 2026-05-20

## Context

The retrieval eval suite (`evals/retrieval/runner.py`) already runs an opt-in,
cross-family LLM judge: `gpt-4o-mini` generates an answer grounded in the
retrieved context, then Claude scores it on custom 1–5 `faithfulness` and
`helpfulness` Likert scales. The setup works, and the cross-family pairing
(OpenAI generator, Anthropic judge) deliberately avoids same-model scoring
bias.

Its weakness is vocabulary. `faithfulness` and `helpfulness` on a bespoke 1–5
scale mean nothing to a reader who has not read the runner source. RAGAS
(Retrieval Augmented Generation Assessment) is the de-facto industry library
for RAG evaluation; its metric names — Faithfulness, Answer Relevancy, Context
Precision, Context Recall — appear in nearly every reference RAG paper, blog
post, and competitor's docs. A portfolio reader recognises them at a glance.

## Decision

Ship RAGAS as a **parallel** eval signal that runs **alongside** the existing
custom Claude judge — explicitly **not** as a replacement.

- The custom Claude judge remains the load-bearing **cross-family** independent
  observation and the headline signal.
- RAGAS adds standardized-vocabulary parity. The two judges measure overlapping
  ground from independent angles — different judge models, different prompting
  techniques, different metric definitions.
- The configuration is locked: `gpt-4o-mini` judge; hybrid mode only; the two
  `pre_filter` cells only; weekly cadence (never on the PR fast path); NaN
  scores recorded with a reason and reported as two means (`mean_strict`,
  `mean_available`); fixed operational gates vs rolling-median score gates;
  cross-family corroboration required for a red Faithfulness / Answer Relevancy
  regression; `single-judge-red` for Context Precision / Recall. The full
  rationale lives in `docs/evals.md` § "RAGAS comparison" → "Methodology".

## Consequences

- A weekly time series of RAGAS snapshots is committed to
  `docs/ragas-weekly/<DATE>.{json,md}`, giving the drift and score-regression
  gates the rolling history they compare against.
- Cost is accepted and bounded: roughly 1,200 `gpt-4o-mini` calls per weekly
  run (well under $1), kept down by the hybrid-only, two-cell,
  weekly-not-nightly scoping.
- Same-family bias is accepted — the RAGAS judge shares a model family with the
  answer generator. Independence is preserved by keeping the cross-family
  Claude judge as the headline signal.
- The eval suite now speaks a methodology a portfolio reader recognises without
  reading runner source — the stated goal of the integration.
- PR CI is unaffected: RAGAS never runs on the PR fast path (too noisy, too
  expensive, wrong cadence).

## Alternatives considered and rejected

- **Replace the custom Claude judge entirely with RAGAS.** Rejected — it would
  discard the cross-family independent observation, the strongest property of
  the existing setup.
- **Use Claude as the RAGAS judge.** Rejected — RAGAS is built around the
  OpenAI ecosystem, and a Claude RAGAS judge would collapse into the existing
  Claude custom judge, losing the independent-angle benefit of two different
  judges.
- **Mirror the full six-cell sweep.** Rejected — four of the six cells are
  degenerate or already covered by the security and recall trade-off tables;
  scoring them adds cost, not signal.
- **σ-based (standard-deviation) regression thresholds.** Rejected — σ is
  unstable on a 4-point rolling window; a couple of quiet weeks shrink it and
  turn ordinary noise into false alerts.
- **%-based (relative) regression thresholds.** Rejected — a fixed percentage
  misleads near the 0 and 1 score boundaries, where the same relative drop is a
  wildly different absolute move.
