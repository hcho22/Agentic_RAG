# PRD: Retrieval-Quality Pass (Lexical Leg Revival + Adaptive Fusion)

Status: in progress. US-113 (lexical golden-set instrumentation), US-114 (keyword_search OR-fallback), and US-115 (deterministic-alpha fusion seam, pure/unwired) landed; US-116 through US-118 not started.
Origin: comparison of Agentic_RAG against RakuenSoftware/aimee (2026-07-10), scoped via grilling session.
Predecessor numbering: continues after US-112 (Phase-2 PRD, `.claude/agent/tasks/prd-phase2-implementation.md`).

## 1. Introduction / Overview

The nightly retrieval eval (`docs/nightly/2026-07-09.md`) shows the hybrid retrieval stack is underperforming its own vector leg.
The keyword (lexical) leg is structurally dead: recall@5 is 0.140 overall and 0.000 on paraphrase questions, because `keyword_search` builds its query with `websearch_to_tsquery('english', query)`, which ANDs every non-stopword term of a full natural-language question.
Any question word missing from a chunk zeroes the match.
Fused via equal-weight RRF, this dead leg actively drags hybrid below vector: hybrid MRR 0.786 vs vector 0.796 overall, and 0.453 vs 0.503 on the adversarial category.
Hybrid retrieval should never be worse than its best single leg.

This pass revives the lexical leg, makes fusion query-adaptive, and instruments both changes with a new golden-set category so improvements are measurable rather than assumed.
It borrows three proven ideas from the aimee project (RakuenSoftware/aimee): a deterministic per-query fusion weight ("dynamic alpha"), benchmark-before-ship discipline for rerankers, and category-level eval instrumentation.
It deliberately borrows nothing that touches trust boundaries.

## 2. Goals

- Keyword-mode recall@5 rises materially from 0.140 after the OR-fallback lands (measured on the extended golden set).
- Hybrid recall@5 and MRR are greater than or equal to vector on every golden-set category, including the new lexical category (fixing the current adversarial inversion).
- Adversarial hybrid MRR recovers above the current vector value of 0.503.
- The eval harness gains a lexical/exact-token category so lexical-leg performance is observable forever.
- A reranker recommendation exists, backed by measured adversarial and lexical MRR/nDCG deltas, without changing any default.
- The nightly non-regression table stops showing a permanent false-alarm state.
- All existing security gates (E4 zero-leak, E6 second-workspace, E7 P1a/P1b escalation tripwire, AU4) remain green through every story.

## 3. User Stories

### US-113: Lexical golden-set category and identifier-dense corpus doc

**Description:** As a kit maintainer, I want a golden-set category of exact-token questions (error codes, config keys, quoted phrases) so that lexical-leg fixes and fusion changes are measurable instead of assumed.

**Acceptance Criteria:**

- [x] New corpus doc `db_seed/corpus/api-error-reference.md` containing identifier-dense content: error codes (e.g. `ERR-4102`), config keys (e.g. `WEBHOOK_RETRY_MAX`), and quotable literal phrases, written in the style of the existing corpus docs.
- [x] Approximately 10 new questions (q51 onward) in `evals/retrieval/retrieval_gold.yaml` with `category: lexical`, whose phrasing targets exact-token matching (the query contains the identifier or quoted phrase verbatim).
- [x] Gold anchors quoted verbatim from the new doc per `docs/golden-set-authoring.md`, with every chunk containing each answer anchored (completeness contract, since E4 no-access populations derive from gold).
- [x] Reference answers for the new questions added to `evals/retrieval/generation_gold.yaml`.
- [x] `"lexical"` appended to `CATEGORY_ORDER` in `evals/retrieval/runner.py` (fail-closed list at ~line 129) and in the duplicate list in `evals/retrieval/ci/diff_results.py` (~line 36).
- [x] Headline question count in the runner summary is computed from `len(per_question)` rather than hardcoded "50".
- [x] `python -m evals.retrieval.test_us108_layered_golden_set` and `python -m evals.retrieval.test_content_anchors` pass.

**Validation Test:**

- **Setup:** Local Supabase running; corpus reseeded via `python -m db_seed.corpus_seed`.
- **Steps:**
  1. Run `python -m evals.retrieval.runner --viewers all --include-e6 --out /tmp/us113.json`.
  2. Inspect the per-category tables in the output summary.
  3. Inspect the E4 security table.
- **Expected Result:** A `lexical` row appears in every per-category table; no `ZeroResolveError` (all anchors resolve); the E4 no-access table stays 1.000 across all modes; keyword-mode lexical recall is recorded (expected poor at this point, since this story is the before-instrument, not the fix).
- **Failure Indicator:** Runner rejects the new category, an anchor fails to resolve to any chunk, or any E4 cell drops below 1.000.

### US-114: OR-fallback in keyword_search

**Description:** As an end user asking full natural-language questions, I want keyword search to degrade from AND to OR matching when strict matching finds too little, so that the lexical leg returns signal instead of nothing.

**Acceptance Criteria:**

- [x] New migration `supabase/migrations/<timestamp>_keyword_search_or_fallback.sql` that DROPs the current 7-parameter `public.keyword_search` (the live definition is `20260624150100_keyword_search_workspace_filter.sql`, including trailing `filter_workspace_id uuid default null`) and recreates it with a byte-identical signature and return table, `security invoker`, re-issuing the `grant execute` to `authenticated`, following the repo's DROP-and-CREATE migration pattern.
- [x] Query semantics: `tsq_and` (current `websearch_to_tsquery`) is tried first; when it fills fewer than `match_count` rows, remaining slots are filled by `tsq_or` built as `to_tsquery('english', array_to_string(tsvector_to_array(to_tsvector('english', query)), ' | '))`, guarded with `nullif`/`numnode` so stopword-only queries return empty rather than erroring.
- [x] AND-matched rows always rank above OR-fallback rows; within each block, ordering stays `similarity desc, id asc`.
- [x] The visibility predicate block (owner-OR-ACL, workspace-membership EXISTS, `d.deleted_at is null`, `filter_workspace_id`, metadata filters) is copied verbatim; no `role` or `is_bot` appears anywhere in the function (core invariant 1).
- [x] No backend code change (`backend/retrieval.py::keyword_search` payload unchanged).
- [x] ADR `docs/adr/0009-keyword-or-fallback.md` committed, recording why fallback-not-replacement and why AND-above-OR ranking.
- [x] `python -m backend.test_au4_auth_attacks` passes.

**Validation Test:**

- **Setup:** Local Supabase; `supabase db reset` applies all migrations including the new one; corpus reseeded.
- **Steps:**
  1. Run `python -m evals.retrieval.runner --mode keyword --viewers full --out /tmp/us114-kw.json` and compare keyword recall@5 against the pre-change 0.140.
  2. Run `python -m evals.retrieval.runner --viewers all --include-e6 --out /tmp/us114-full.json`.
  3. Run `python -m evals.retrieval.e7_runner --include-p1b --out /tmp/us114-e7.json`.
- **Expected Result:** Keyword recall@5 rises materially (paraphrase no longer 0.000; lexical category strong); E4 security table stays 1.000 in all modes (OR widens matching but the predicate is unchanged); E7 P1a/P1b deterministic legs pass.
- **Failure Indicator:** Keyword recall unchanged, any E4 cell below 1.000, E7 tripwire fires, or `supabase db reset` fails on the migration.
- **Result (2026-07-10, local Supabase):** Keyword recall@5 **0.140 → 0.917** (paraphrase **0.000 → 0.800**, lexical **1.000**); hybrid recall@5 0.95 now above vector 0.875. E4 `security_no_access` **1.000** across keyword/hybrid/vector in both pre- and post-filter. `python -m backend.test_au4_auth_attacks` PASS (36 assertions; keyword boundary V=0, MALLORY=0). E7 P1a/P1b PASS (no gold leaked, byte-for-byte generic deferral, no tripwire).

### US-115: Deterministic-alpha fusion seam (pure functions, not yet wired)

**Description:** As a kit maintainer, I want a unit-tested, pure query-to-weight function and a weighted RRF fuser so that adaptive fusion exists as a verified seam before any live call-site changes behavior (core invariant 9: ship the seam before the call-site).

**Acceptance Criteria:**

- [x] Pure module-level function `predict_alpha(query: str) -> float` in `backend/retrieval.py`: no I/O, deterministic, features are quoted-phrase presence, identifier-shaped token count (snake_case, UPPER_SNAKE, code-like digit-symbol mixes such as `CAT-1234`), digit density, and token count; returns the vector-leg weight clamped to [0.3, 0.7]; neutral prose queries return exactly 0.5.
- [x] `_rrf_fuse` gains an optional `weights` parameter where each ranking contributes `2 * w_i / (k + rank)`; with `weights=None` or `(0.5, 0.5)` the scores are byte-identical to today's `1 / (k + rank)`.
- [x] `cosine_similarity` pass-through in `_rrf_fuse` is untouched (US-046: the escalation gate thresholds on raw cosine; fusion weights must never alter per-row cosine values).
- [x] Nothing calls `predict_alpha` yet; `hybrid_search` behavior is bit-for-bit unchanged in this story.
- [x] New self-running test `backend/test_alpha_fusion.py` (pattern of `test_cosine_surface.py`) covering: determinism and purity, empty query, all-identifier query, quoted-phrase query, clamp bounds, legacy-equivalence of `weights=(0.5, 0.5)`, and cosine preservation under unequal weights.
- [x] `python -m backend.test_cosine_surface` extended for the new kwarg default and passes.

**Status:** Done. `predict_alpha` + `_rrf_fuse(weights=...)` landed as a pure, unwired seam in `backend/retrieval.py`; `backend/test_alpha_fusion.py` (10 groups) and the extended `backend/test_cosine_surface.py` (6 groups) both pass. Seam is provably inert: `hybrid_search` still fuses with `weights=None`, `predict_alpha` has no call-site, and the byte-identical legacy-equivalence tests guarantee eval metrics cannot shift, so the DB-backed diff in step 3 is a formality (no live path reads the new code).

**Validation Test:**

- **Setup:** No DB needed (unit layer only).
- **Steps:**
  1. Run `python -m backend.test_alpha_fusion`.
  2. Run `python -m backend.test_cosine_surface`.
  3. Run `python -m evals.retrieval.runner --viewers full --out /tmp/us115.json` (with local Supabase) and diff headline metrics against the previous run.
- **Expected Result:** Both test modules pass; eval metrics are identical to pre-story values (the seam is inert).
- **Failure Indicator:** Any metric shifts (the seam leaked into live behavior), or `weights=(0.5, 0.5)` produces scores differing from the legacy path.

### US-116: Wire adaptive alpha into hybrid_search

**Description:** As an end user, I want hybrid retrieval to weight the lexical leg up on identifier-style queries and down on prose so that hybrid is never worse than vector and exact-token lookups improve.

**Acceptance Criteria:**

- [ ] `hybrid_search` computes `alpha = predict_alpha(query)` and passes `weights=(alpha, 1 - alpha)` (vector ranking first, matching current order).
- [ ] Env knob `HYBRID_FUSION_ALPHA` with values `auto` (default) or a fixed float in [0, 1], validated following the `get_rrf_k()` pattern; `0.5` pins legacy equal-weight behavior as the ops escape hatch.
- [ ] The deflection pipeline (`backend/escalation.py::run_deflection_pipeline` calls `hybrid_search` directly) inherits alpha by design; this coupling and its guards are documented in the ADR.
- [ ] `python -m backend.test_escalation_gate` and `python -m backend.test_deflection_pipeline` pass.
- [ ] Eval evidence in the PR body: hybrid recall@5 and MRR >= vector on every category including lexical; adversarial hybrid MRR > 0.503. This is PR-body evidence, not a new hard gate (per-PR quality blocking stays off per `evals/gate/gate.yaml` policy).
- [ ] ADR `docs/adr/0010-deterministic-alpha-fusion.md` committed (asymmetric fusion trade-off, deflection coupling, clamp rationale); CLAUDE.md ADR list line updated.

**Validation Test:**

- **Setup:** Local Supabase seeded; US-113 through US-115 merged.
- **Steps:**
  1. Run `python -m evals.retrieval.runner --viewers full --out /tmp/us116.json`.
  2. Compare `aggregates.by_mode_category`: hybrid vs vector for recall@5 and MRR on every category.
  3. Run `python -m evals.retrieval.e7_runner --include-p1b --out /tmp/us116-e7.json`.
  4. Set `HYBRID_FUSION_ALPHA=0.5` and re-run step 1; diff against a pre-US-116 baseline run.
- **Expected Result:** Step 2 shows hybrid >= vector everywhere and adversarial hybrid MRR above 0.503; step 3 passes (escalation behavior stable); step 4 reproduces pre-change metrics exactly (escape hatch works).
- **Failure Indicator:** Any category where hybrid < vector, E7 tripwire fires, or the 0.5 pin does not reproduce legacy numbers.

### US-117: Reranker bake-off (eval-only, assistant surface)

**Description:** As a kit maintainer, I want measured adversarial and lexical ranking deltas for each available reranker so that the reranker recommendation is data-backed, without changing any default or touching the widget deflection path.

**Acceptance Criteria:**

- [ ] `evals/retrieval/runner.py` gains `--reranker {none,llm,cohere,voyage}` (default `none`): retrieve `max(RERANK_INPUT_K, TOP_K)` candidates, then apply `build_reranker(...)` via the same path `backend/main.py::_retrieve_for_agent` uses, trimming to TOP_K; the chosen reranker is recorded in the results JSON.
- [ ] `--reranker none` produces output identical to a no-flag run (NullReranker pass-through sanity).
- [ ] Bake-off executed manually: hybrid mode, full viewer, one run per available backend (llm always; cohere/voyage if API keys present) plus the none control.
- [ ] Results doc `docs/reranker-bakeoff.md`: per-backend adversarial and lexical MRR/nDCG@5 table, latency observations, and a recommendation; `RERANKER` default remains `none`.
- [ ] The reranker-sweep TODO in `.github/workflows/retrieval-eval-nightly.yml` (lines ~78-91) is either resolved by adding a nightly hybrid+llm reranker row or explicitly updated to reference the bake-off doc.
- [ ] No change to `backend/escalation.py` or any widget-path code.

**Validation Test:**

- **Setup:** Local Supabase seeded; `OPENAI_API_KEY` available (for `llm` reranker).
- **Steps:**
  1. Run the runner twice with identical settings: once with no flag, once with `--reranker none`; diff the two JSON outputs (ignoring timestamps).
  2. Run with `--reranker llm --out /tmp/us117-llm.json`.
  3. Open `docs/reranker-bakeoff.md` and check every claimed number appears in a committed or referenced results JSON.
- **Expected Result:** Step 1 outputs are identical; step 2 completes with reranker recorded in the JSON and no fatal errors (reranker failures are non-fatal by design); step 3 numbers trace to real runs.
- **Failure Indicator:** `--reranker none` changes results, the llm run crashes the harness, or the doc contains numbers with no traceable source.

### US-118: Re-pin the non-regression baseline and make its tolerance one-sided

**Description:** As a kit maintainer, I want the nightly non-regression check pinned to current corpus levels and flagging only regressions, so that a permanently-red display cell stops training readers to ignore it.

**Acceptance Criteria:**

- [ ] `MODULE_10_BASELINE_RECALL_AT_5` in `evals/retrieval/runner.py` (~lines 180-185) renamed to `NON_REGRESSION_BASELINE_RECALL_AT_5` and re-pinned to post-US-116 nightly levels, with a provenance comment (date, source snapshot).
- [ ] Tolerance made one-sided: flag only `delta < -NON_REGRESSION_TOLERANCE`; improvements show ✓; the summary column header (~line 1375) updated to read "Δ ≥ −0.005?".
- [ ] `evals/retrieval/summary.md` regenerated by a full run so the committed snapshot (and `docs/evals.md` via `docs/_embed_eval_summaries.py`) no longer carries the stale 0.670 row.
- [ ] This story lands last, after US-113 through US-117 have settled the metric levels.

**Validation Test:**

- **Setup:** Local Supabase seeded; all prior stories merged.
- **Steps:**
  1. Run the full eval; inspect the non-regression table.
  2. Temporarily export a degraded `SEARCH_SIMILARITY_THRESHOLD` (e.g. 0.9), re-run, inspect the table, then unset it.
- **Expected Result:** Step 1 shows ✓ on all three modes; step 2 shows ✗ on the affected modes (the check still detects real regressions).
- **Failure Indicator:** ✗ persists at current levels, or the degraded run still shows ✓ (check is dead).

## 4. Functional Requirements

- FR-1: `keyword_search` must first match with AND semantics (`websearch_to_tsquery`) and, only when fewer than `match_count` rows result, fill remaining slots with OR-semantics matches, AND rows always ranked above OR rows.
- FR-2: The recreated `keyword_search` must preserve the exact 7-parameter signature, return table, `security invoker`, grants, and visibility predicate of the live definition; `role`/`is_bot` must not appear.
- FR-3: `predict_alpha(query)` must be a pure deterministic function returning the vector-leg weight in [0.3, 0.7], with neutral queries mapping to exactly 0.5.
- FR-4: `_rrf_fuse` with `weights=(0.5, 0.5)` or `weights=None` must produce scores byte-identical to the current implementation.
- FR-5: Fusion weighting must never modify per-row `cosine_similarity` values (US-046 escalation-gate contract).
- FR-6: `HYBRID_FUSION_ALPHA` env var must accept `auto` or a float, defaulting to `auto`, with `0.5` reproducing legacy behavior exactly.
- FR-7: The golden set must gain a `lexical` category, registered in both `CATEGORY_ORDER` lists (runner and diff_results), with anchors satisfying the completeness contract in `docs/golden-set-authoring.md`.
- FR-8: The eval runner must support `--reranker {none,llm,cohere,voyage}` with `none` as a true pass-through, recording the choice in results JSON.
- FR-9: The non-regression check must flag only regressions beyond tolerance, never improvements.
- FR-10: Every story must keep E4, E6, E7 P1a/P1b, and AU4 gates green; these run automatically per-PR since all touched paths are in `retrieval-eval.yml`'s trigger list.

## 5. Non-Goals (Out of Scope)

- No query rewriting, HyDE, or multi-query expansion (paraphrase recall is already 1.000; no measured failure mass).
- No knowledge-graph retrieval leg (every entity/relation would become a new ACL-carrying surface; multi-hop is already 0.933).
- No persistent typed-fact memory or cross-conversation recall (collides with the deliberate US-085 decision that support transcripts are not retrievable).
- No conversation summarization or context folding (no measured pain at the 10-turn window).
- No comparative baseline adapters (BM25/ChromaDB); the nightly per-mode table already serves this purpose.
- No reranker default change and no reranker on the widget deflection path (its escalation gate is calibrated on raw cosine; any such change is a separate, E7-gated effort).
- No structured citation objects in this pass; queued as the agreed follow-up (per-answer objects with doc_id, chunk_id, filename, span, surfaced beside the granting-principal badge in `ToolAttribution.tsx`).
- No changes to `match_chunks`, table RLS, trust boundaries, or any of the ten core invariants.
- No frontend changes anywhere in this pass.

## 6. Design Considerations

- The OR-fallback lives entirely inside the SQL function so the backend, rate limiter, and deflection pipeline see no interface change.
- `predict_alpha` mirrors aimee's `kb_fusion_predict_alpha`: a cheap feature-based function, not a model call, consistent with ADR-0003's "deterministic control flow, never a model decision" stance.
- The [0.3, 0.7] clamp exists because keyword-only rows carry `cosine_similarity = None`; an unclamped keyword-heavy top-k would shrink the deflection gate's cosine list and could flip escalation decisions.
- Deflection inherits alpha deliberately (lexical customer queries are where the widget most needs it), guarded by the per-PR E7 P1a/P1b tripwire, the weekly LLM-judged escalation sweep, the clamp, and the `HYBRID_FUSION_ALPHA=0.5` ops pin.

## 7. Technical Considerations

- Migration pattern is DROP-and-CREATE in a new timestamped file with re-issued GRANT (both prior keyword_search migrations document this).
- `CATEGORY_ORDER` is fail-closed: the YAML change and the runner change must land in the same PR or the runner rejects the gold file.
- In the US-113 PR, the CI delta comment compares main's 50-question run against the PR's 60-question run; headline means will shift in that one comment (advisory only; call it out in the PR body).
- The weekly RAGAS rolling-history gates will fire a one-time non-blocking yellow drift finding after the question set grows; expected, self-heals as history rolls.
- `evals/gate/gate.yaml` needs no threshold edits (bindings are coverage/drift, not recall-keyed).
- Corpus seeding is directory-driven (`db_seed/corpus/*.md`); new docs need no seeder code change, and E6 copies corpus docs into Workspace B generically.
- Sequencing is strict: US-113 (instrument) → US-114 (fix leg) → US-115 (seam) → US-116 (wire) → US-117 (bake-off, may parallel US-115/116) → US-118 (re-pin, always last).

## 8. Success Metrics

- Keyword-mode recall@5 rises from 0.140 to a materially higher level, with paraphrase no longer at 0.000 and the lexical category strong.
- Hybrid recall@5 and MRR >= vector on 100% of golden-set categories (currently violated on adversarial and overall MRR).
- Adversarial hybrid MRR > 0.503 (current vector level), from 0.453.
- E4/E6 zero-leak tables remain 1.000 and E7 false-resolve stays within the 5% ceiling on every run.
- Nightly non-regression table shows ✓ on all modes at pinned levels, and a forced degradation still renders ✗.
- A committed `docs/reranker-bakeoff.md` exists with a data-backed recommendation.

## 9. Open Questions

- Should the lexical corpus doc be one doc or two (a second `integration-settings.md` would diversify identifier styles)? Default: start with one, add the second only if 10 good questions cannot be authored against one doc.
- If the reranker bake-off shows a large adversarial win, does a follow-up story wire it into the assistant path by default (E7-gated widget adoption remains separate)?
- Exact re-pinned baseline values for US-118 are unknowable until US-113 through US-116 settle; the story must read them from the then-current nightly snapshot.
