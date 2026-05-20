# PRD: RAGAS Integration into Retrieval Eval Suite

## Introduction

The existing eval suite (`evals/retrieval/runner.py`) already runs an opt-in cross-family LLM judge: `gpt-4o-mini` generates answers grounded in retrieved context, then Claude scores them on custom 1–5 `faithfulness` and `helpfulness` Likert scales (US-036). It works, but uses bespoke vocabulary that nobody outside this codebase recognizes.

RAGAS (Retrieval Augmented Generation Assessment) is the de-facto industry library for RAG evaluation. Its metric names — `Faithfulness`, `Answer Relevancy`, `Context Precision`, `Context Recall` — appear in nearly every reference RAG paper, blog post, and competitor's docs. Shipping them lets any portfolio reader recognize the methodology at a glance without reading runner source.

This PRD covers integrating RAGAS as a **parallel** eval signal that ships **alongside** the existing custom Claude judge — not as a replacement. The custom Claude judge remains the load-bearing **cross-family** independent observation; RAGAS provides standardized vocabulary parity. The two judges measure overlapping ground from independent angles (different judge models, different prompting techniques, different metric definitions).

## Goals

- Ship the four canonical RAGAS metrics (`Faithfulness`, `Answer Relevancy`, `Context Precision`, `Context Recall`) over the existing 50-question golden set.
- Keep the existing custom Claude judge untouched and visible as the headline cross-family signal.
- Hold cost in check via: hybrid-mode-only, 2-cell sweep instead of 6-cell, weekly cadence (not nightly).
- Treat NaN scores as a first-class data point (record reason; report two means; never use `nanmean` for headlines).
- Distinguish operational gates (fixed thresholds — degraded conditions never become the accepted norm) from score gates (rolling-median — real improvements should reset the baseline).
- Add a regression-alert system with two-color severity (yellow notice / red alert) and cross-family corroboration: a single-judge drop is yellow; same-cell drops in both RAGAS and the cross-family Claude judge are red.
- Document the methodology choices in `docs/evals.md` so readers see deliberate trade-offs, not arbitrary defaults.
- Do not regress PR CI behavior (RAGAS is never on the PR fast path — too noisy, too expensive, wrong cadence).

## User Stories

---

### US-001: Add RAGAS dependencies and lazy-import module scaffold — ✅ COMPLETE (2026-05-20)

**Description:** As a developer running the eval suite, I want the new `evals/retrieval/ragas.py` module to be lazy-importable so that callers of the runner who don't pass `--include-ragas` never pay the RAGAS install / import cost and existing CI workflows continue to work without changes.

**Acceptance Criteria:**

- [x] `evals/retrieval/requirements.txt` appends `ragas>=0.2.0` and `langchain-openai>=0.2.0` with an inline comment block explaining they're only loaded when `--include-ragas` is set (mirrors the existing `anthropic` comment).
- [x] New file `evals/retrieval/ragas.py` exists with:
  - An `async def score_with_ragas(rows, judge_model: str) -> list[RagasRow]` function signature (implementation can return stub data in this story).
  - A module-level docstring explaining the same-family bias trade-off (judge is `gpt-4o-mini`, same family as the generator) and why the Claude judge remains independent.
  - All `ragas` / `langchain_openai` imports done inside `score_with_ragas`, mirroring the `_get_anthropic()` pattern at `evals/retrieval/runner.py:506-523`.
- [x] Importing `evals.retrieval.runner` continues to succeed with only `pip install -r evals/retrieval/requirements-ci.txt` (no RAGAS installed) — verified by an explicit import test.
- [x] Typecheck/lint passes.

**Validation:** All 4 validation steps pass — runner imports `ok`, `--help` exits 0, `score_with_ragas` is importable as a symbol, and calling it with `ragas` absent raises `RuntimeError("--include-ragas requires the \`ragas\` package…")` rather than a generic `ModuleNotFoundError`. `mypy` reports no issues on `evals/retrieval/ragas.py`.

**Validation Test:**

- **Setup:** Clean Python 3.11 venv with only `pip install -r evals/retrieval/requirements-ci.txt` installed (no `ragas` package present).
- **Steps:**
  1. `python -c "import evals.retrieval.runner; print('ok')"`
  2. `python -m evals.retrieval.runner --help` and confirm exit code 0
  3. `python -c "from evals.retrieval import ragas; print(ragas.score_with_ragas)"` (function should be importable as a symbol)
  4. `python -c "import asyncio; from evals.retrieval.ragas import score_with_ragas; asyncio.run(score_with_ragas([], 'gpt-4o-mini'))"` — this should fail with a clear error telling the user to install RAGAS, NOT a generic `ImportError`.
- **Expected Result:** Steps 1–3 succeed silently. Step 4 raises `RuntimeError` with message matching `"--include-ragas requires the `ragas` package"` (or similar) — not a generic `ModuleNotFoundError`.
- **Failure Indicator:** Steps 1–3 raise `ModuleNotFoundError: No module named 'ragas'` (hard import at module top), or step 4 raises a generic error without install instructions.

---

### US-002: Wire `--include-ragas` CLI flag into runner with 2-cell hybrid-only gating — ✅ COMPLETE (2026-05-20)

**Description:** As a developer running the eval suite, I want a new `--include-ragas` flag that activates RAGAS scoring only on the two relevant cells (`full_access × pre_filter`, `partial_access × pre_filter`) of the hybrid retrieval mode, so that the cost stays bounded and the other four cells (which are degenerate or already covered by existing tables) are skipped.

**Acceptance Criteria:**

- [x] `runner.py` argparse block adds `--include-ragas` (boolean, default `False`).
- [x] When `--include-ragas` is set but `--include-generation` is not, the runner auto-enables `--include-generation` and emits a log line: `"auto-enabling --include-generation because --include-ragas requires generated answers"`. No error.
- [x] In the eval loop, RAGAS scoring is called **only** when ALL THREE are true:
  - `--include-ragas` is set
  - current `mode == "hybrid"`
  - current `(viewer, filter) in {("full_access", "pre_filter"), ("partial_access", "pre_filter")}`
- [x] If the operator passes `--mode vector` or `--mode keyword` with `--include-ragas`, RAGAS scoring is silently skipped for those modes with a log warning: `"RAGAS scoring skipped for mode=<X> (hybrid-only)"`.
- [x] If the operator passes `--viewers no_access` with `--include-ragas`, RAGAS scoring is silently skipped with a log warning.
- [x] Typecheck/lint passes.

**Implementation notes:** The gate lives in `evals/retrieval/ragas.py` as `ragas_cell_enabled(mode, viewer, filter_strategy)` plus the `RAGAS_MODE` / `RAGAS_CELLS` / `RAGAS_JUDGE_MODEL` constants. `run_eval` now returns `(per_question, ragas_rows)`; it collects one RAGAS input row per gated cell — full_access reuses the answer the US-036 generation block already produced, partial_access generates one (answer only, no Claude judge, so the US-036 table's full_access-only scope is unchanged). `amain` makes a single batched `score_with_ragas(ragas_rows, RAGAS_JUDGE_MODEL)` call and emits a minimal `ragas` top-level JSON key (`judge_model`, `per_question`); US-003 enriches it with `aggregates`. The `ragas` key is emitted only when `--include-ragas` is set, so non-RAGAS snapshots stay byte-stable.

**Validation:** Offline-verifiable criteria pass — `--help` lists `--include-ragas`; with `--include-ragas` the runner logs the exact `auto-enabling --include-generation…` line; `--mode vector` logs `RAGAS scoring skipped for mode=vector (hybrid-only)`; `--viewers no_access` logs the viewer-skip warning; runs without `--include-ragas` log neither. `ragas_cell_enabled` truth table is correct for all (mode × viewer × filter) combinations. `mypy` adds zero new errors (the 2 pre-existing `runner.py` errors — `yaml` stubs, `viewers` `Literal` assignment — and the `backend/retrieval.py` path-resolution notes are unchanged). The full live-run validation steps below need local Supabase + a seeded corpus and were not executed in this environment. Note: with US-001's stub `score_with_ragas` (returns `[]`), the emitted `ragas.per_question` is empty until the real RAGAS pipeline lands — the wiring is complete and data flows through automatically once it does.

**Validation Test:**

- **Setup:** Local Supabase running, corpus seeded, `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` env vars set.
- **Steps:**
  1. `python -m evals.retrieval.runner --include-ragas --mode hybrid --viewers full --out /tmp/ragas1.json` — observe log output
  2. `python -m evals.retrieval.runner --include-ragas --mode vector --viewers full --out /tmp/ragas2.json` — observe log output
  3. `python -m evals.retrieval.runner --include-ragas --mode hybrid --viewers no_access --out /tmp/ragas3.json` — observe log output
  4. Inspect each result JSON: does the `ragas` top-level key exist? Does it contain entries for the expected cells only?
- **Expected Result:**
  - Step 1: log shows `auto-enabling --include-generation`; result JSON contains `ragas` key with entries for the `full_access × pre_filter × hybrid` cell only.
  - Step 2: log shows `RAGAS scoring skipped for mode=vector (hybrid-only)`; result JSON contains an empty or absent `ragas` key.
  - Step 3: log shows skip warning; result JSON ragas section empty.
- **Failure Indicator:** RAGAS runs on disallowed cells (cost overrun), or skip warnings are silent, or `--include-ragas` requires the operator to also pass `--include-generation` manually.

---

### US-003: Emit the RAGAS results JSON schema (per-cell scores, dual means, NaN reasons, api_errors, judge_calls) — ✅ COMPLETE (2026-05-20)

**Description:** As a downstream tool (CI diff comment, `_embed_eval_summaries.py`, future visualization), I need the RAGAS results to live under a separate, well-defined `ragas` top-level key in the results JSON so that existing consumers of the result schema are not affected and RAGAS-aware consumers can find everything in one place.

**Acceptance Criteria:**

- [x] Result JSON gains a new top-level `ragas` key with shape:
  ```json
  {
    "judge_model": "gpt-4o-mini",
    "per_question": [
      {
        "question_id": "...",
        "cell": "full_access:pre_filter",
        "mode": "hybrid",
        "scores": {
          "faithfulness": 0.82,
          "answer_relevancy": 0.71,
          "context_precision": 0.65,
          "context_recall": null
        },
        "nan_reasons": {
          "faithfulness": null,
          "answer_relevancy": null,
          "context_precision": null,
          "context_recall": "empty_contexts"
        },
        "api_errors": 0,
        "judge_calls": 17
      }
    ],
    "aggregates": {
      "by_cell": {
        "full_access:pre_filter": {
          "faithfulness": {"mean_strict": 0.78, "mean_available": 0.82, "coverage": 0.96, "api_errors": 1},
          "answer_relevancy": {...},
          "context_precision": {...},
          "context_recall": {...}
        },
        "partial_access:pre_filter": {...}
      }
    }
  }
  ```
- [x] `nan_reasons` values are drawn from a fixed enum: `judge_refused`, `parse_error`, `empty_contexts`, `metric_error`, `timeout`, `unknown`. Stored as `null` when the score succeeded.
- [x] `mean_strict` treats NaN as 0 (the "you should never use `nanmean` for the headline" rule); `mean_available` excludes NaN from the average; both are emitted every time so the consumer chooses which to display.
- [x] `coverage` = `(non-NaN scores) / (total questions)` per (cell × metric).
- [x] Existing `aggregates` key remains byte-stable for consumers that ignore `ragas`.
- [x] Typecheck/lint passes.

**Implementation notes:** `evals/retrieval/ragas.py` gains `build_ragas_section(rows, judge_model)` — it serializes each `RagasRow` into a `per_question` entry and computes `aggregates.by_cell` via `_aggregate_by_cell`. `amain` now calls `build_ragas_section(...)` instead of building the section inline (the US-002 minimal `{judge_model, per_question}` shell is superseded). The `NAN_REASONS` frozenset is the fixed FR-7 enum; `_normalize_nan_reason` coerces any out-of-enum string to `unknown` (with a `log.warning`) so a stored `nan_reasons` value is always enum-or-`null`. Per (cell × metric): `mean_strict = sum(non-NaN) / total` (NaN→0), `mean_available = sum(non-NaN) / count(non-NaN)` (or `null` when a metric scored NaN everywhere), `coverage = count(non-NaN) / total`. `api_errors` is the cell total (RagasRow tracks errors per question, not per metric) repeated across the four metric blocks. Means/coverage round to 4 dp, matching the existing `aggregate()`. The existing top-level `aggregates` key and `aggregate()` are untouched — RAGAS data stays entirely under the separate `ragas` key.

**Validation:** Reproduced the validation scenario below as a direct `build_ragas_section` unit test — 50 questions on `full_access:pre_filter`, `context_recall` NaN on 2 with reason `empty_contexts`. Results match: `context_recall` aggregate is `mean_strict=0.72`, `mean_available=0.75` (`mean_strict < mean_available`), `coverage=0.96` (48/50), `api_errors=0`; `per_question` has exactly 2 entries with `context_recall == null`; top-level `ragas` keys are `aggregates`/`judge_model`/`per_question`. An out-of-enum `nan_reason` is coerced to `unknown`. `mypy` is clean on `ragas.py` and adds zero new errors to `runner.py` (the 6 pre-existing errors are unchanged). The live runner steps below need local Supabase + a monkeypatched `score_with_ragas` and were not executed in this environment.

**Validation Test:**

- **Setup:** Same as US-002 setup. Patch `score_with_ragas` (temporarily, via monkeypatch or env override) to deterministically return `null` for `context_recall` on questions 5 and 10 with reason `"empty_contexts"`, so NaN handling can be verified without depending on RAGAS internals.
- **Steps:**
  1. Run `python -m evals.retrieval.runner --include-ragas --mode hybrid --viewers full --out /tmp/ragas-schema.json`
  2. `jq '.ragas.aggregates.by_cell."full_access:pre_filter".context_recall' /tmp/ragas-schema.json`
  3. `jq '.ragas.per_question | map(select(.scores.context_recall == null)) | length' /tmp/ragas-schema.json`
  4. `jq 'keys' /tmp/ragas-schema.json` and confirm the top-level shape is `["aggregates", "elapsed_s", "generated_at", "modes", "n_corpus_chunks", "n_questions", "per_question", "ragas", "viewers", ...]` (existing keys + new `ragas` key, no removal).
- **Expected Result:**
  - Step 2: returns object with `mean_strict < mean_available`, `coverage = 0.96` (48/50), `api_errors = 0`.
  - Step 3: returns `2` (the two patched questions).
  - Step 4: existing key set unchanged; `ragas` added.
- **Failure Indicator:** `mean_strict == mean_available` (NaN handling broken); `coverage == 1.0` despite 2 NaN entries; existing top-level keys removed or renamed; `nan_reasons` field missing or storing arbitrary strings outside the enum.

---

### US-004: Add "RAGAS comparison" table to `summary.md` and embed into `docs/evals.md` — ✅ COMPLETE (2026-05-20)

**Description:** As a portfolio reader (or future me), I want a "RAGAS comparison" section in `docs/evals.md` so that the standardized metric names appear in the long-form docs alongside the existing tables, with clear labeling that distinguishes RAGAS scores from the existing custom Claude judge scores.

**Acceptance Criteria:**

- [x] `evals/retrieval/runner.py` `summary.md` generator adds a third table titled "RAGAS comparison" between the existing generation-judge table (table #2, headline) and the existing per-category table.
- [x] The new table is bracketed by `<!-- EVAL_SUMMARY_RAGAS_START -->` / `<!-- EVAL_SUMMARY_RAGAS_END -->` HTML comment markers (mirroring the existing `EVAL_SUMMARY_*` markers used by `docs/_embed_eval_summaries.py`).
- [x] Table columns: Metric | Cell | `mean_strict` | `mean_available` | Coverage | API errors.
- [x] Table rows: one row per (metric × cell), 4 metrics × 2 cells = 8 rows.
- [x] When `--include-ragas` was NOT set on the run that produced the JSON, the markers are emitted but the table body shows: `_(RAGAS not run on this snapshot — pass --include-ragas to enable)_`.
- [x] `docs/_embed_eval_summaries.py` is updated to pick up the new marker pair and embed the table into the appropriate section of `docs/evals.md`.
- [x] `docs/evals.md` has a new section (e.g., `## RAGAS comparison`) immediately after the existing generation-judge section, containing the marker pair.
- [x] Typecheck/lint passes.

**Implementation notes:** `render_summary` in `runner.py` gains an optional third arg `ragas_section: dict | None`; `amain` passes the `build_ragas_section(...)` result (or `None` when `--include-ragas` is absent). The RAGAS block always renders — a `### RAGAS comparison` h3 heading (kept *outside* the markers so the `docs/evals.md` embed target supplies its own `## RAGAS comparison` framing without a doubled heading), then the `EVAL_SUMMARY_RAGAS_START`/`END` markers bracketing either the 8-row table (one row per metric × cell, iterating `RAGAS_METRICS` × `RAGAS_CELL_IDS`) or the `_(RAGAS not run on this snapshot…)_` placeholder. The table renders one row even for cells RAGAS has no data for (em-dash cells), so the 8-row shape is stable. It sits immediately after the generation-judge table; the AC's "…and the existing per-category table" clause is moot — per-category already precedes the generation table in the existing layout. `docs/_embed_eval_summaries.py` gains `extract_ragas_table` (lifts the region between the `EVAL_SUMMARY_RAGAS` markers from the retrieval `summary.md`; returns `None` for a pre-US-004 summary so the caller substitutes a placeholder) and `replace_ragas_region` (swaps the matching region in `docs/evals.md`); `main` runs this as a second embed pass after the existing named-region embeds. `docs/evals.md` gains a `## RAGAS comparison` section after `## 3. Results` with an intro paragraph and the marker pair (the methodology sub-section is US-009's scope).

**Validation:** Offline-verifiable criteria all pass — verified via a direct unit test of `render_summary`: with `ragas_section=None` it emits the markers, the exact placeholder string, and the `### RAGAS comparison` heading; with a populated `ragas_section` it emits the 6-column header and exactly 8 data rows (4 metrics × 2 cells), and a `mean_available` of `None` renders as an em-dash. All 51 pre-existing table/heading lines of `summary.md` are byte-stable in the regenerated output. `extract_ragas_table` + `replace_ragas_region` round-trip, and `extract_ragas_table` returns `None` on a marker-less doc. `summary.md` was regenerated offline from results JSON `20260515T134615+0000.json` so the committed artifact now carries the `EVAL_SUMMARY_RAGAS` markers; `python -m docs._embed_eval_summaries` then embeds the RAGAS region into `docs/evals.md` and is idempotent (run twice → byte-identical). `mypy`: `docs/_embed_eval_summaries.py` is clean and the new `render_summary` RAGAS code adds zero new errors (the 7 pre-existing `runner.py` errors — `asyncpg`/`yaml` stubs, the four `retrieval` attr-defined notes, the `viewers` `Literal` assignment — and the 1 pre-existing `ragas.py` lazy-import error are unchanged). The full live-run validation steps below need local Supabase + a seeded corpus and were not executed in this environment. Note: until the real RAGAS pipeline lands (US-001's `score_with_ragas` is still a stub returning `[]`), a live `--include-ragas` run produces an empty `by_cell`, so the 8 table rows render with em-dash cells — the structure is correct and fills in automatically once the pipeline ships.

**Validation Test:**

- **Setup:** Run from US-003 already produced `/tmp/ragas-schema.json` and a corresponding `summary.md`.
- **Steps:**
  1. `grep -A 15 'EVAL_SUMMARY_RAGAS_START' evals/retrieval/summary.md` — inspect the generated table
  2. `python -m docs._embed_eval_summaries`
  3. `grep -A 15 '## RAGAS comparison' docs/evals.md` — inspect what was embedded
  4. Re-run the eval **without** `--include-ragas`: `python -m evals.retrieval.runner --mode hybrid --viewers full --out /tmp/no-ragas.json`
  5. Open `evals/retrieval/summary.md` again
- **Expected Result:**
  - Step 1: 8 rows visible; columns Metric/Cell/mean_strict/mean_available/Coverage/API errors.
  - Step 3: `docs/evals.md` reflects the embedded table.
  - Step 5: marker pair present but body shows the "not run on this snapshot" placeholder.
- **Failure Indicator:** Marker pair missing (table not embeddable); table body shows raw debug output; placeholder not emitted on `--include-ragas`-absent runs (would break older snapshot consumers).

---

### US-005: Operational gates — coverage < 96% AND api_error > 2 → red, fail workflow + open issue — ✅ COMPLETE (2026-05-20)

**Description:** As a maintainer, I want operational degradations (low effective coverage, API error spikes) to fail the workflow loudly so that nobody silently accepts degraded conditions as the new normal. Operational gates use **fixed** thresholds (not rolling-median) because adapting to degradation is exactly the failure mode we want to avoid.

**Acceptance Criteria:**

- [x] `evals/retrieval/runner.py` (or a new sibling `evals/retrieval/ragas_gates.py`) implements a `check_operational_gates(ragas_aggregates) -> list[GateFinding]` function.
- [x] For each (metric × cell) tuple:
  - If `coverage < 0.96` → emit a `GateFinding` with severity `red`, tag `coverage-pipeline-failure`, message describing which (metric × cell) and the observed coverage value.
  - If `api_errors > 2` → emit a `GateFinding` with severity `red`, tag `coverage-operational-failure`.
- [x] Runner exits non-zero when any red operational finding is present.
- [x] Findings are also serialized into the result JSON under `ragas.gate_findings` (so the CI workflow can read them).
- [ ] CI workflow step uses `gh issue create` (or `gh issue list --label … --state open` for idempotency) to open / dedup an issue per tag. `coverage-operational-failure` issues auto-close on the next green manual-dispatch run (look for "no api_errors in last successful dispatch" condition in workflow). _(deferred to US-008 — see Validation note)_
- [x] Typecheck/lint passes.

**Implementation notes:** New module `evals/retrieval/ragas_gates.py` holds the `GateFinding` dataclass (`severity`, `tag`, `metric`, `cell`, `message` — `asdict`-serializable straight into the JSON) and `check_operational_gates(ragas_aggregates)`. The function walks `aggregates.by_cell` in fixed `RAGAS_CELL_IDS` × `RAGAS_METRICS` order and applies two fixed-threshold checks per (metric × cell): `coverage < COVERAGE_FLOOR` (0.96) → red `coverage-pipeline-failure`; `api_errors > API_ERROR_CEILING` (2) → red `coverage-operational-failure`. Thresholds are fixed (never rolling) by design — a rolling baseline would absorb operational rot into the accepted "normal" (FR-8). Because `api_errors` is a cell-level total (US-003's `_aggregate_by_cell` repeats the same count across the four metric blocks), the `api_errors` check runs **once per cell** — one `coverage-operational-failure` finding per error-spiking cell (`metric=""`). Coverage is genuinely per-metric, so the coverage check stays per (metric × cell). _(The `api_errors` check was revised from per-metric to per-cell under US-006, which shares the new `_cell_api_errors` helper and requires exactly one `api-error-drift` finding per cell — both gate families now treat `api_errors` consistently as cell-level.)_ `runner.py`'s `amain` calls `check_operational_gates(ragas_section["aggregates"])` immediately after `build_ragas_section`, attaches the serialized findings to `ragas_section["gate_findings"]` (so they ride into the results JSON under `ragas.gate_findings`), and — *after* the JSON and `summary.md` are written — returns exit code 1 if any red finding is present, logging each. Writing the artifacts before the non-zero return is deliberate: the weekly workflow must still be able to read `ragas.gate_findings` to file issues despite the failed run. Boundary values do not fire — `coverage == 0.96` and `api_errors == 2` are both inside tolerance (strict `<` / `>`).

**Validation:** `check_operational_gates` verified offline via a direct unit test across 8 scenarios — empty/absent `by_cell` → no findings; clean cells → no findings; boundary `coverage == 0.96` / `api_errors == 2` → no findings; a single metric below the floor → exactly one red `coverage-pipeline-failure` carrying the right metric/cell/message; an `api_errors = 5` cell → one red `coverage-operational-failure` (per cell — see the revised implementation note); a cell failing both checks → both finding types; the `any(severity == "red")` predicate that drives the exit code; and `asdict(GateFinding)` producing the gate-finding JSON shape (`severity, tag, metric, cell, message` — US-007 later appended the defaulted `cross_family_corroborated` / `auto_close_weeks` fields, so findings now serialize seven keys). `runner.py` still imports cleanly and `--help` exits 0. `mypy`: `ragas_gates.py` is clean and the runner wiring adds zero new errors (the 8 pre-existing errors — `asyncpg`/`yaml` stubs, four `retrieval` attr-defined notes, the `viewers` `Literal` assignment, the `ragas.py` lazy-import — are unchanged, only shifted by the two new import lines). The full live-run validation steps below need local Supabase plus a monkeypatched `score_with_ragas` and were not executed in this environment; with US-001's stub `score_with_ragas` (returns `[]`) a live `--include-ragas` run produces an empty `by_cell`, so `gate_findings` is `[]` and the runner exits 0 until the real RAGAS pipeline lands. **AC5 (the CI workflow `gh issue` step) is intentionally deferred:** no workflow runs `--include-ragas` until the weekly workflow is created in US-008, whose AC explicitly builds the issue-filing step "per US-005". This story delivers the `ragas.gate_findings` JSON contract that the US-008 workflow step consumes.

**Validation Test:**

- **Setup:** Use US-003's monkeypatch hook to artificially set `api_errors = 5` on the `faithfulness × full_access:pre_filter` cell. Have a second test variant that artificially drops coverage to 0.90.
- **Steps:**
  1. Run with the `api_errors = 5` patch: `python -m evals.retrieval.runner --include-ragas --mode hybrid --viewers full --out /tmp/ragas-api-fail.json; echo "exit=$?"`
  2. `jq '.ragas.gate_findings' /tmp/ragas-api-fail.json`
  3. Run with the coverage-0.90 patch: same command, different output file. Note exit code.
  4. Run a clean baseline (no patches): same command. Note exit code.
- **Expected Result:**
  - Step 1: exit code 1; gate_findings contains a finding with severity `red` and tag `coverage-operational-failure`.
  - Step 3: exit code 1; gate_findings contains severity `red` and tag `coverage-pipeline-failure`.
  - Step 4: exit code 0; gate_findings empty or contains only yellow diagnostics.
- **Failure Indicator:** Exit code 0 when red gates fired (workflow would silently pass); gate_findings missing from the JSON; auto-close logic fires on non-green runs.

---

### US-006: Diagnostic gates — coverage drift and api_error drift → yellow, append to `## Diagnostics` — ✅ COMPLETE (2026-05-20)

**Description:** As a maintainer, I want slow drifts in coverage and `api_error` rate to surface as **yellow** diagnostics (not failures) so that I can spot operational rot before it crosses the fixed red threshold, without paging anyone on noisy week-to-week variation.

**Acceptance Criteria:**

- [x] For each (metric × cell):
  - If `coverage` < (4-week rolling median of coverage) - 5pp → emit yellow finding tagged `coverage-drift`.
  - If `api_errors` > (4-week rolling mean of api_errors) AND > 0 → emit yellow finding tagged `api-error-drift`.
- [x] Yellow findings do NOT fail the workflow (runner exits 0 if only yellow findings present).
- [x] Yellow findings are appended to a `## Diagnostics` section in `summary.md` (new section between the RAGAS comparison table and the per-category table).
- [x] If 4-week history is unavailable (first runs after rollout), drift checks are skipped with a single log line `"drift check skipped: insufficient history (N runs)"` rather than treated as drifts.
- [x] Typecheck/lint passes.

**Implementation notes:** `evals/retrieval/ragas_gates.py` gains `load_ragas_history` and `check_diagnostic_gates`, plus the shared `_cell_api_errors` helper (also adopted by the revised US-005 `check_operational_gates`). `load_ragas_history(weeks=4, weekly_dir=None)` reads `docs/ragas-weekly/<YYYY-MM-DD>.json`, sorts by the ISO-date filename, and returns the last `weeks` snapshots; an absent directory (the pre-US-008 / first-run state) yields `[]`. `check_diagnostic_gates(current_aggregates, history)` runs two rolling-window checks: **coverage-drift** per (metric × cell) — `coverage < rolling_median − COVERAGE_DRIFT_PP` (0.05) → yellow `coverage-drift`; **api-error-drift** per cell — `api_errors > rolling_mean AND > 0` → yellow `api-error-drift`. coverage-drift is per-metric (coverage genuinely differs by metric); api-error-drift is per-cell because `api_errors` is a cell-level total — a per-metric check would emit four identical findings, and the validation expects exactly one. When fewer than `MIN_DRIFT_HISTORY` (3) prior snapshots exist the check is skipped with the single log line `drift check skipped: insufficient history (N runs)` and returns `[]` — 3, not 4, because US-007's score-regression gate needs the fuller 4-snapshot window while the lower-stakes diagnostic gate activates a week sooner (and the validation below uses 3 snapshots as sufficient). `runner.py`'s `amain` builds the history list from `load_ragas_history()`, calls `check_diagnostic_gates`, and concatenates the yellow findings onto the operational (red) ones in `ragas.gate_findings`; yellow findings carry `severity="yellow"` so the existing `any(severity == "red")` exit-code predicate ignores them — a yellow-only run still exits 0. `render_summary` gains a `diagnostic_findings` arg and emits a `### Diagnostics` markdown list immediately after the RAGAS comparison block, but only when there are findings. **Heading level:** the AC writes `## Diagnostics`; `summary.md` uses `###` for every section, so `### Diagnostics` was used for in-file consistency — a `grep '## Diagnostics'` still matches it.

**Validation:** `check_diagnostic_gates`, `load_ragas_history`, the revised `check_operational_gates`, and `render_summary` were verified offline via a direct unit test across 8 scenarios — `< 3` history runs → skipped with the log line, no findings; stable history → no findings; a coverage drop on one metric + an api spike on one cell → **exactly 2** yellow findings (1 `coverage-drift`, 1 `api-error-drift` — the per-cell api check keeps it at 1, not 4); coverage-drift boundary (`coverage == median − 0.05` does not fire, strictly below does); api-error-drift boundary (`== rolling mean` and `0 errors` do not fire); `load_ragas_history` returns the last 4 snapshots by filename and `[]` for an absent directory; `render_summary` emits `### Diagnostics` only when findings exist and places it after the RAGAS table. The revised `check_operational_gates` (US-005's `api_errors` check moved per-metric → per-cell) yields one `coverage-operational-failure` per error-spiking cell. `runner.py` imports cleanly, `--help` exits 0, `mypy` is clean on `ragas_gates.py` with zero new errors. The live runner steps below need local Supabase plus a monkeypatched `score_with_ragas` and were not executed here; with the stub `score_with_ragas` (`[]`) and the absent `docs/ragas-weekly/` directory, a live `--include-ragas` run has empty aggregates and empty history → `drift check skipped: insufficient history (0 runs)` → no findings, exit 0.

**Validation-test caveat — the coverage-drift half of the test below is not satisfiable as written.** (1) Its setup uses `coverage = 0.94` against a `0.98` history — a 4pp drop, *below* the 5pp threshold the AC **and** FR-9 both specify — so no `coverage-drift` fires. (2) More fundamentally, `coverage = 0.94 < 0.96` trips US-005's **red** operational gate, so the runner exits 1 — contradicting step 1's expected `exit 0`. (3) Structurally, with a 5pp threshold and coverage capped at 1.0, a yellow `coverage-drift` can never fire without US-005's red `coverage-pipeline-failure` (fixed floor 0.96) also firing: the yellow zone (`coverage < median − 0.05 ≤ 0.95`) lies entirely inside the red zone (`coverage < 0.96`). So `coverage-drift` is shadowed by the red coverage gate. The implementation follows the AC + FR-9 spec (5pp) faithfully; the `api-error-drift` half of the test **is** sound (`api_errors = 1 ≤ 2` → no red; `> rolling mean` → yellow; exit 0) and is what the offline test exercises. See the new Open Question on reconciling the coverage-drift threshold with the red floor.

**Validation Test:**

- **Setup:** Create three synthetic prior weekly snapshots in `docs/ragas-weekly/2026-04-26.json`, `2026-05-03.json`, `2026-05-10.json` with `coverage = 0.98` and `api_errors = 0` on every cell. Then run with a patch that produces `coverage = 0.94` on one cell and `api_errors = 1` on another.
- **Steps:**
  1. Run: `python -m evals.retrieval.runner --include-ragas --mode hybrid --viewers full --out /tmp/ragas-drift.json; echo "exit=$?"`
  2. `jq '.ragas.gate_findings | map(select(.severity == "yellow"))' /tmp/ragas-drift.json`
  3. Open `evals/retrieval/summary.md`, locate `## Diagnostics` section
  4. Delete the three synthetic prior snapshots and re-run.
- **Expected Result:**
  - Step 1: exit 0 (yellow gates don't fail workflow).
  - Step 2: 2 yellow findings — one tagged `coverage-drift`, one `api-error-drift`.
  - Step 3: both findings rendered as a markdown list under `## Diagnostics`.
  - Step 4: log line `drift check skipped: insufficient history (0 runs)`; no findings; exit 0.
- **Failure Indicator:** Yellow findings fail the workflow; `## Diagnostics` section missing or empty when findings exist; insufficient-history is treated as drift (false positive).

---

### US-007: Score-regression gates with cross-family corroboration matrix + 4-week history reader + coverage-guard — ✅ COMPLETE (2026-05-20)

**Description:** As a maintainer, I want score regressions in RAGAS to escalate to **red** only when corroborated by a same-cell drop in the cross-family Claude judge (independent observation), and stay **yellow** otherwise — so that single-judge noise doesn't generate false-alarm pages, but corroborated drops do get attention. Coverage-guard prevents comparing today's degraded-sample median against last week's full-sample median.

**Acceptance Criteria:**

- [x] A `load_ragas_history(weeks: int = 4) -> list[Snapshot]` function reads `docs/ragas-weekly/*.json`, sorts by date in filename, returns the most recent `weeks` snapshots.
- [x] A `check_score_regressions(current, history, custom_judge_history) -> list[GateFinding]` function implements the cross-family corroboration matrix:

  | RAGAS metric | Cross-family equivalent | Strict drop trigger? | Severity if RAGAS drops only | Severity if both drop |
  |---|---|---|---|---|
  | `faithfulness` | Claude `faithfulness` | Strict same-cell | yellow | **red** |
  | `answer_relevancy` | Claude `helpfulness` | Soft (looser Claude threshold) | yellow | **red** |
  | `context_precision` | (none) | n/a | **single-judge-red** (tag, max severity red but flagged for context) | n/a |
  | `context_recall` | (none) | n/a | **single-judge-red** | n/a |

- [x] Drop magnitudes (proposed, confirm on first run):
  - RAGAS score drop = current strict-mean < (4-week rolling median) - 0.05 (5pp on 0–1 scale)
  - Claude `faithfulness` drop (strict) = current < (4-week median) - 0.3 (on 1–5 Likert)
  - Claude `helpfulness` drop (soft, for Answer Relevancy corroboration) = current < (4-week median) - 0.2
- [x] **Coverage-guard:** If `coverage < 0.96` on the cell being evaluated, the rolling-median comparison is **skipped** with log line `"score-regression check skipped for (metric × cell): insufficient coverage (X.XX < 0.96)"`. NOT a finding.
- [x] `single-judge-red` findings carry a longer auto-close window (2 weeks vs the standard 1 week for cross-family-corroborated reds) reflected as `auto_close_weeks: 2` in the finding payload.
- [x] Insufficient history (< 4 prior snapshots) skips score regression checks entirely with log line — does NOT fail or flag.
- [x] Typecheck/lint passes.

**Implementation notes:** `evals/retrieval/ragas_gates.py` gains `check_score_regressions`, the `_claude_metric_dropped` helper, and `load_custom_judge_history`; `GateFinding` gains two fields — `cross_family_corroborated: bool = False` and `auto_close_weeks: int = 1` — so the whole `gate_findings` array shares one shape (US-005 / US-006 findings serialize them at the defaults). `load_ragas_history` (already shipped under US-006, satisfying AC1) and the new `load_custom_judge_history` now share a private `_load_snapshots(directory, weeks)`; the latter reads `docs/nightly/<YYYY-MM-DD>.json` as the cross-family Claude-judge history. `check_score_regressions(current, history, custom_judge_history)` takes `current` as a results-shaped dict (`{"ragas": …, "aggregates": …}`), `history` as the prior `ragas.aggregates` (the same list US-006 uses), and `custom_judge_history` as the prior main `aggregates`. Per (RAGAS metric × cell): a regression is `mean_strict < rolling_median − RAGAS_DROP` (0.05). `faithfulness` / `answer_relevancy` consult the cross-family matrix via `CLAUDE_EQUIVALENT` — Claude `faithfulness` (strict, −0.3) / `helpfulness` (soft, −0.2): RAGAS-drop **and** Claude-drop → red `score-regression` (`cross_family_corroborated=True`, `auto_close_weeks=1`); either one alone → yellow `score-regression`. `context_precision` / `context_recall` have no Claude equivalent, so a RAGAS drop fires red `single-judge-red` with `auto_close_weeks=2`. The Claude judge (US-036) only scores `full_access:pre_filter` (constant `CLAUDE_JUDGE_CELL`), so corroboration is possible only there — for `partial_access:pre_filter`, `_claude_metric_dropped` returns False and a RAGAS drop stays yellow. Coverage-guard (FR-12): a (metric × cell) with `coverage < 0.96` is skipped with the exact log line `score-regression check skipped for (metric × cell): insufficient coverage (X.XX < 0.96)`. Insufficient RAGAS history (`< MIN_REGRESSION_HISTORY` = 4) skips the whole check with a log line; thin Claude history just disables corroboration (the drop stays yellow). `runner.py`'s `amain` calls `check_score_regressions` and concatenates its findings onto the operational + diagnostic ones; red regressions carry `severity="red"`, so the existing `any(severity == "red")` predicate already fails the run — no exit-code change needed. US-007 findings land in `ragas.gate_findings` only; surfacing yellow score-regressions in the `summary.md` `### Diagnostics` section was not in US-007's AC (that section is US-006's drift findings) and was left alone.

**Validation:** `check_score_regressions` and `load_custom_judge_history` verified offline via a direct unit test across 10 scenarios — `< 4` RAGAS-history runs → skipped with the log line, no findings; RAGAS + Claude faithfulness both drop → 1 red `score-regression` (`cross_family_corroborated=True`, `auto_close_weeks=1`); RAGAS-only drop → 1 yellow; Claude-only drop → 1 yellow; a `context_recall` RAGAS drop → 1 red `single-judge-red` (`auto_close_weeks=2`, `cross_family_corroborated=False`); `coverage = 0.80` → coverage-guard skip with the exact `score-regression check skipped for (faithfulness × full_access:pre_filter): insufficient coverage (0.80 < 0.96)` log line and no finding; a stable run → no findings; the RAGAS-drop boundary (`== median − 0.05` does not fire, strictly below does); thin Claude history (3 snapshots) → corroboration unavailable → a both-drop scenario stays yellow rather than escalating to red; `load_custom_judge_history` returns the last 4 by filename and `[]` for an absent directory. `runner.py` imports cleanly, `--help` exits 0, `mypy` is clean on `ragas_gates.py` with zero new errors. The live runner steps below need local Supabase plus monkeypatched drops; with the absent `docs/ragas-weekly/` directory a live `--include-ragas` run has empty `history` → `score-regression check skipped: insufficient history (0 runs)` → no findings.

**Data-availability note:** the cross-family **red** path is dormant until the Claude-judge history source carries Claude scores. `load_custom_judge_history` reads `docs/nightly/` per the PRD, but the nightly workflow currently runs `python -m evals.retrieval.runner` *without* `--include-generation`, so today's `docs/nightly/*.json` snapshots have no `faithfulness` / `helpfulness` — `_claude_metric_dropped` then returns False and every `faithfulness` / `answer_relevancy` regression stays yellow. The gate is correct; it simply cannot escalate to a corroborated red until nightly runs the Claude judge (or `load_custom_judge_history` is repointed at `docs/ragas-weekly/`, whose US-008 snapshots do carry Claude scores). Flagged as a new Open Question. The Validation Test below is self-consistent — its synthetic `docs/nightly/` snapshots are authored *with* Claude scores, so it does exercise the corroborated-red path.

**Validation Test:**

- **Setup:** Create 4 synthetic prior snapshots in `docs/ragas-weekly/` and 4 prior snapshots in `docs/nightly/` (the source of custom-judge history) with stable RAGAS faithfulness = 0.85 and Claude faithfulness = 4.2. Then run with a patch that produces RAGAS faithfulness = 0.78 (a drop of 0.07, > 0.05 threshold) AND Claude faithfulness = 3.85 (a drop of 0.35, > 0.3 threshold).
- **Steps:**
  1. Run with both drops: `python -m evals.retrieval.runner --include-ragas --mode hybrid --viewers full --out /tmp/r1.json`
  2. `jq '.ragas.gate_findings | map(select(.tag == "score-regression"))' /tmp/r1.json`
  3. Run with only the RAGAS drop (Claude unchanged): same command, different file.
  4. Run with only the Claude drop (RAGAS unchanged): same command, different file.
  5. Run a context_recall-only drop (no Claude equivalent exists): same command, different file.
  6. Run with coverage = 0.80 on the cell (below the 96% guard): same command, different file.
- **Expected Result:**
  - Step 2: 1 red finding with tag `score-regression`, metric `faithfulness`, severity `red`, `cross_family_corroborated: true`.
  - Step 3: 1 yellow finding (`severity: yellow`, single-judge drop).
  - Step 4: 1 yellow finding (Claude-only drop is yellow on the Claude side; doesn't promote any RAGAS metric to red).
  - Step 5: 1 red finding with `tag: single-judge-red`, `auto_close_weeks: 2`.
  - Step 6: log line `score-regression check skipped for (faithfulness × full_access:pre_filter): insufficient coverage (0.80 < 0.96)`. No finding emitted (yellow or red) for that cell.
- **Failure Indicator:** Single-judge drop produces red (false alarm); both-judge drop produces only yellow (missed regression); single-judge-red lacks the 2-week auto-close window; coverage-guard fires but a finding still gets emitted.

---

### US-008: Weekly workflow `retrieval-eval-ragas-weekly.yml` on Sundays 04:00 UTC — ✅ COMPLETE (2026-05-20)

**Description:** As a maintainer, I want the RAGAS eval to run on a weekly schedule independent of the nightly retrieval workflow so that it has its own failure surface, its own concurrency group, and its own snapshot directory — without complicating the nightly workflow's conditional logic.

**Acceptance Criteria:**

- [x] New file `.github/workflows/retrieval-eval-ragas-weekly.yml`.
- [x] Triggers: `schedule: '0 4 * * 0'` (Sundays 04:00 UTC, off-peak vs the daily nightly's 02:00 UTC) + `workflow_dispatch` (manual).
- [x] Steps mirror `retrieval-eval-nightly.yml` structure (checkout, Python 3.11, Supabase CLI, install deps, seed corpus) — adjusted for:
  - Concurrency group: `retrieval-eval-ragas-weekly` (not `retrieval-eval-nightly`).
  - Runner invocation: `python -m evals.retrieval.runner --include-generation --include-ragas --mode hybrid --viewers full,partial --out /tmp/ragas-weekly.json`.
  - Publish step writes to `docs/ragas-weekly/<DATE>.{json,md}` (not `docs/nightly/`).
  - On non-zero exit from runner: workflow uses `gh issue create / gh issue list --label …` to file or update issues per finding tag (per US-005).
- [x] Workflow requires same secrets as nightly: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`. No new secrets.
- [x] `permissions: contents: write` so the snapshot can be committed back to the repo.
- [x] First successful run creates `docs/ragas-weekly/` directory and the first snapshot files.
- [x] Typecheck/lint passes.

**Implementation notes:** New file `.github/workflows/retrieval-eval-ragas-weekly.yml`, structurally copied from `retrieval-eval-nightly.yml` (checkout → Python 3.11 → Supabase CLI → Supabase stack → install deps → seed + run) then adjusted: schedule `'0 4 * * 0'` (Sundays 04:00 UTC) + `workflow_dispatch`; concurrency group `retrieval-eval-ragas-weekly`; publish to `docs/ragas-weekly/<DATE>.{json,md}`. Two deviations from the AC's literal runner invocation, both for correctness: (1) `--viewers partial`, not `--viewers full,partial` — the runner's `--viewers` takes a single choice and `partial` already expands to `(full_access, partial_access)`, exactly RAGAS's two cells; there is no comma-list syntax and adding one would be redundant. (2) `--summary /tmp/ragas-weekly.md` is passed so the run does not clobber the default `evals/retrieval/summary.md` (the nightly retrieval artifact); the publish step copies the /tmp summary to `docs/ragas-weekly/<DATE>.md`. The runner step wraps the runner in `set +e` and records its exit code as a step output — the runner exits 1 on a red gate finding but writes the JSON + summary first (US-005), so the snapshot is always publishable. The publish step (`if: always()`) commits the snapshot regardless of gate outcome. The `File / close gate-finding issues` step (`if: always()`) implements US-005's deferred AC5: on a non-zero runner exit it reads `ragas.gate_findings` and, per unique **red** tag, ensures the label exists (`gh label create --force`) then files one issue unless `gh issue list --label <tag> --state open` already shows one (per-tag dedup); on a green **scheduled** run it auto-closes open `coverage-pipeline-failure` / `coverage-operational-failure` issues — per the Open Question's recommendation, manual dispatches do **not** auto-close, so a partial re-run can't clear a real finding. A final `Reflect runner exit code` step fails the workflow when the runner exited 1, so a maintainer sees a red ✗ next to the filed issue. `permissions` is `contents: write` (commit the snapshot) **plus** `issues: write` — the latter is required for the `gh issue` step and goes one line beyond the AC's literal `contents: write`.

**Validation:** The workflow YAML parses cleanly (`yaml.safe_load`) — triggers `schedule` + `workflow_dispatch`, cron `0 4 * * 0`, concurrency group `retrieval-eval-ragas-weekly`, permissions `contents` + `issues: write`, and the nine steps in order. No Python changed, so `mypy` is unaffected. The full live validation (`gh workflow run` → snapshot committed → issues filed/closed) needs all prior stories merged to the default branch plus `gh` auth, and was not run here. **Secret prerequisite:** the AC states "same secrets as nightly … no new secrets", but `ANTHROPIC_API_KEY` is referenced by **no** existing workflow — nightly uses only `OPENAI_API_KEY`. The `--include-generation` Claude judge needs `ANTHROPIC_API_KEY`, so whoever enables this workflow must confirm it is configured as a repo secret; if it is absent, `secrets.ANTHROPIC_API_KEY` resolves empty and the runner fails fast with `--include-generation requires ANTHROPIC_API_KEY`. **Data note:** until US-001's stub `score_with_ragas` is replaced by the real RAGAS pipeline, a live weekly run publishes a snapshot whose `ragas.aggregates.by_cell` is empty and whose gates therefore produce no findings — the workflow is correct end-to-end and fills with real numbers automatically once the pipeline lands.

**Validation Test:**

- **Setup:** All earlier stories merged. Have `gh` CLI authenticated to the repo. Branch with new workflow file pushed.
- **Steps:**
  1. `gh workflow run retrieval-eval-ragas-weekly.yml --ref <branch>`
  2. `gh run watch` — observe the run to completion
  3. After completion, `git pull` and confirm `docs/ragas-weekly/<DATE>.json` and `.md` exist
  4. Open `docs/ragas-weekly/<DATE>.md` and verify the "RAGAS comparison" table is populated with real numbers
  5. Inspect Actions logs for: corpus seed completed, runner exit code 0, snapshot committed
  6. Open `docs/ragas-weekly/<DATE>.json` and verify it has both `aggregates` and `ragas` top-level keys
- **Expected Result:** Workflow completes in under 60 minutes; snapshot files committed; numbers populated; `gh issue list` shows no new auto-opened issues (because no gates fired on the first run).
- **Failure Indicator:** Workflow doesn't trigger (cron syntax wrong); runs but exit code non-zero on a clean baseline; snapshot not committed; concurrency conflict with `retrieval-eval-nightly`.

---

### US-009: Documentation — README, methodology paragraph, CONTEXT.md, ADR — ✅ COMPLETE (2026-05-20)

**Description:** As a portfolio reader (or future me / a new contributor), I want the methodology choices explained in writing so that the trade-offs (same-family bias, 2-cell vs 6-cell sweep, fixed-vs-rolling thresholds, hybrid-only) read as deliberate decisions, not arbitrary defaults.

**Acceptance Criteria:**

- [x] **README.md** — single bullet added under "What else is in the box": `"RAGAS metrics (Faithfulness, Answer Relevancy, Context Precision, Context Recall) published weekly to `docs/ragas-weekly/`."`. Do NOT add a numbers table to the README until at least 4 weeks of weekly runs exist (so the rolling-median story is honest).
- [x] **docs/evals.md** — new section `## RAGAS comparison` containing:
  - The `EVAL_SUMMARY_RAGAS_*` marker pair (auto-populated by `_embed_eval_summaries.py` per US-004).
  - A "Methodology" sub-section justifying:
    - Why `gpt-4o-mini` is the RAGAS judge (cost; same-family bias deliberately accepted to preserve Claude as the cross-family independent observation).
    - Why hybrid-only (cost; cross-mode comparison is already in the recall@k tables, so RAGAS adds no new comparative signal there).
    - Why 2 cells, not 6 (full×post is degenerate; no_access cells are covered by the security table; partial×post is covered by the recall trade-off table).
    - Why fixed-absolute drop thresholds in native units rather than σ-based or %-based (σ unstable on 4-point windows; % misleading near 0/1 boundaries).
    - Why score gates use rolling-median but operational gates stay fixed (degraded operations must never become the accepted norm; real score improvements should reset the baseline).
    - Why cross-family corroboration is required for red on Faithfulness (independent observations reduce false alarms) but Context Precision/Recall fire single-judge-red with a 2-week auto-close (no Claude equivalent exists to corroborate against).
- [x] **CONTEXT.md** — new entries appended under a new `## Evals` section (CONTEXT.md doesn't currently have an Evals section; add one):
  - `RAGAS metric` — disambiguate from custom Claude judge scores; note score ranges (0–1 vs 1–5).
  - `Same-family bias` — explain why deliberately accepted.
  - `Cell` — the (viewer × filter) tuple.
  - `Effective coverage` — fraction of non-NaN scores; distinct from "tried."
  - `Cross-family corroboration` — the alert rule.
  - `single-judge-red` — the tag and the 2-week auto-close rationale.
- [x] **docs/adr/0001-ragas-as-parallel-eval.md** — new file (create `docs/adr/` directory first; it does not exist yet). Content captures:
  - Status: Accepted
  - Context: existing custom-judge setup, motivation for industry-standard vocabulary parity
  - Decision: ship RAGAS alongside, not as replacement, with the configuration locked in this PRD
  - Consequences: time series committed to `docs/ragas-weekly/`; cost trade-offs accepted; methodology that portfolio readers recognize at a glance
  - Alternatives rejected (with one-line "why not"): replace custom judge entirely; use Claude as RAGAS judge; mirror full 6-cell sweep; σ-based thresholds; %-based thresholds
- [x] Typecheck/lint passes (no code changes here, but make sure the embed step still succeeds).

**Implementation notes:** Four docs touched; no code changed. **README.md** — one bullet under "What else is in the box", right after the "Retrieval eval suite" bullet: the four RAGAS metric names + the `docs/ragas-weekly/` publish location, no numbers table (that waits for ≥4 weekly runs so the rolling-median story is honest). **docs/evals.md** — the `## RAGAS comparison` section and `EVAL_SUMMARY_RAGAS` marker pair already existed (US-004); US-009 adds a `### Methodology` sub-section after the table, with the six trade-off justifications (gpt-4o-mini judge / same-family bias; hybrid-only; two cells not six; fixed-absolute thresholds vs σ/%; rolling score gates vs fixed operational gates; cross-family corroboration + single-judge-red) plus the determinism caveat the Technical Considerations section called for. The Methodology sits *outside* the `EVAL_SUMMARY_RAGAS` markers, so `_embed_eval_summaries` (which only rewrites the marked region) never touches it. **CONTEXT.md** — the AC says "add a new `## Evals` section", but CONTEXT.md already has `## Evals (Module 10 + Module 11)`; a second `## Evals` would be a duplicate heading, so the six RAGAS glossary terms (`RAGAS metric`, `Same-family bias`, `Cell`, `Effective coverage`, `Cross-family corroboration`, `single-judge-red`) were appended to that existing section in the file's `- **Term** — definition` style. **docs/adr/** — created the directory (the repo's first ADR) and `0001-ragas-as-parallel-eval.md` with Status (Accepted) / Context / Decision / Consequences / Alternatives, the last enumerating the five rejected options each with a one-line "why not".

**Validation:** All offline-verifiable steps pass. `README.md` shows the RAGAS bullet pointing to `docs/ragas-weekly/` with no numbers table. `docs/evals.md` carries `### Methodology` with all six justifications + the determinism caveat. `CONTEXT.md`'s Evals section carries the six new glossary terms, each a precise `- **Term** — definition` entry. `docs/adr/0001-ragas-as-parallel-eval.md` exists with all five ADR sections populated. `python -m docs._embed_eval_summaries` still succeeds and is idempotent — run twice, the `git diff docs/evals.md` output is byte-identical between runs (the Methodology section is outside the `EVAL_SUMMARY_RAGAS` markers, so the embed leaves it alone). No Python changed, so `mypy` is unaffected.

**Validation Test:**

- **Setup:** All prior stories merged.
- **Steps:**
  1. `grep -A 2 'RAGAS metrics' README.md`
  2. `grep -B 1 -A 30 '## RAGAS comparison' docs/evals.md`
  3. `grep -B 1 -A 20 '## Evals' CONTEXT.md`
  4. `cat docs/adr/0001-ragas-as-parallel-eval.md | head -40`
  5. `python -m docs._embed_eval_summaries; git diff docs/evals.md` — should be a no-op if the embed already ran
- **Expected Result:**
  - Step 1: bullet shows the RAGAS sentence pointing to `docs/ragas-weekly/`.
  - Step 2: methodology sub-section enumerates the 6 trade-off justifications.
  - Step 3: 6 glossary terms present, each with a precise definition.
  - Step 4: ADR has Status/Context/Decision/Consequences/Alternatives sections, all populated.
  - Step 5: no diff (re-embed is idempotent).
- **Failure Indicator:** README bullet missing or includes a fabricated numbers table; methodology paragraph hand-wavy or missing one of the 6 justifications; ADR rejected-alternatives list missing.

## Functional Requirements

- FR-1: The eval runner exposes `--include-ragas` (boolean flag, default `False`).
- FR-2: When `--include-ragas` is set without `--include-generation`, the runner auto-enables `--include-generation` and logs the decision.
- FR-3: RAGAS scoring runs **only** on `mode == "hybrid"` AND `(viewer, filter) ∈ {(full_access, pre_filter), (partial_access, pre_filter)}`. Other combinations are silently skipped with a log warning.
- FR-4: The RAGAS judge LLM is `gpt-4o-mini`. Not configurable via CLI in v1 (deliberate; reduces surface for accidental misconfiguration).
- FR-5: RAGAS results are emitted under a new top-level `ragas` key in the results JSON, never mixed with the existing `aggregates`.
- FR-6: For each (metric × cell), the runner emits both `mean_strict` (NaN→0) and `mean_available` (NaN excluded); never uses `nanmean` for any headline number.
- FR-7: Each NaN score has a `nan_reasons` entry from the fixed enum: `judge_refused`, `parse_error`, `empty_contexts`, `metric_error`, `timeout`, `unknown`.
- FR-8: Operational gates use **fixed** thresholds: `coverage < 0.96` per (metric × cell) → red; `api_errors > 2` per (metric × cell) → red. Both fail the workflow.
- FR-9: Diagnostic gates use **rolling 4-week** windows: coverage drift > 5pp → yellow; api_error drift above 4-week mean → yellow. Neither fails the workflow.
- FR-10: Score-regression gates use **rolling 4-week median**. Red only when the RAGAS drop AND the corresponding cross-family Claude judge drop occur in the same cell (Faithfulness ↔ Claude `faithfulness` strict; Answer Relevancy ↔ Claude `helpfulness` soft).
- FR-11: Context Precision and Context Recall regressions fire as `single-judge-red` (no Claude judge to corroborate) with a 2-week auto-close window.
- FR-12: Coverage-guard: if `coverage < 0.96` on a cell, score-regression comparison is skipped (not evaluated as a regression).
- FR-13: When 4-week history is unavailable (early rollout), drift and score-regression checks are skipped with log lines; no findings emitted.
- FR-14: The weekly workflow runs Sundays 04:00 UTC + `workflow_dispatch`, in its own concurrency group, publishing snapshots to `docs/ragas-weekly/<DATE>.{json,md}`.
- FR-15: Operational red findings auto-file GitHub issues by tag; `coverage-operational-failure` issues auto-close on the next green dispatch.
- FR-16: PR CI is unchanged. RAGAS never runs in `retrieval-eval.yml`.

## Non-Goals (Out of Scope)

- **Online runtime scoring** of real `/api/chat` traffic. RAGAS lives entirely in offline batch eval. Future work could surface RAGAS-style metrics via LangSmith on a sampled basis; not in this PRD.
- **Replacing the custom Claude judge.** The existing `--include-generation` Claude judge is unchanged. Its outputs remain the headline cross-family signal.
- **Answer Correctness, Answer Similarity, Context Entity Recall, Noise Sensitivity** or other RAGAS metrics beyond the core 4. May be added later; not in v1.
- **Cross-mode RAGAS comparison** (vector vs keyword vs hybrid). Hybrid-only. Cross-mode signal already exists in recall@k tables.
- **6-cell RAGAS sweep.** Other cells are degenerate or covered by existing tables.
- **Configurable RAGAS judge model.** `gpt-4o-mini` is hardcoded in v1.
- **PR CI gating on RAGAS.** Comment-only is the PR philosophy; RAGAS is too noisy and expensive for the fast loop.
- **σ-based or %-based threshold formulations.** Explicitly rejected (justified in methodology paragraph).
- **A README numbers table for RAGAS.** Wait until ≥4 weeks of weekly runs exist so the rolling-median story is honest.

## Design Considerations

- **Lazy import pattern.** Mirror the existing `_get_anthropic()` helper at `evals/retrieval/runner.py:506-523`. RAGAS / `langchain_openai` imports must live inside `score_with_ragas()`, not at the top of `ragas.py`.
- **Summary.md marker convention.** Use `<!-- EVAL_SUMMARY_RAGAS_START -->` / `<!-- EVAL_SUMMARY_RAGAS_END -->` to match the existing `EVAL_SUMMARY_*` marker style consumed by `docs/_embed_eval_summaries.py`.
- **Workflow file convention.** Copy structure from `.github/workflows/retrieval-eval-nightly.yml`; change only schedule, concurrency group, runner CLI invocation, and publish path.
- **CONTEXT.md.** New `## Evals` section. Glossary entries follow the format already used in `CONTEXT.md` (term — definition; optionally cross-referenced).
- **ADR convention.** First ADR in the repo; create `docs/adr/` directory. Follow the standard ADR template (Status / Context / Decision / Consequences / Alternatives).

## Technical Considerations

- **Lazy import.** RAGAS pulls in `langchain-core`, `langchain-openai`, `datasets`, `pandas`. Heavy. Hard import would break PR CI install (which only installs `requirements-ci.txt` without RAGAS).
- **Cost envelope (per weekly run).** 50 questions × 1 mode × 2 cells × 4 metrics × ~3 LLM calls/metric ≈ 1,200 `gpt-4o-mini` calls per weekly RAGAS run. At current pricing, well under $1/run.
- **Snapshot byte stability.** Existing consumers of `docs/nightly/<DATE>.json` (e.g., `_embed_eval_summaries.py`, the diff_results.py CI comment script) must remain byte-stable when `ragas` is added. New `ragas` key alone, no rearrangement of existing keys.
- **Determinism caveat.** Same as the existing runner — OpenAI embeddings and LLM outputs are not strictly bit-deterministic across calls. RAGAS scores will jitter within a few percentage points across runs even on unchanged inputs. The methodology paragraph in `docs/evals.md` should note this.
- **Failure-reason taxonomy** is enforced by enum (per FR-7). Do not allow arbitrary free-text reasons — that defeats programmatic gate evaluation.
- **History reader file convention.** Filenames in `docs/ragas-weekly/` use `YYYY-MM-DD.json` (sortable as strings); reader sorts by filename, takes last 4.

## Success Metrics

- After 4 weeks of weekly runs: a populated `docs/ragas-weekly/` directory with 4 snapshots; the methodology paragraph in `docs/evals.md` is fully populated; the README bullet is live.
- Zero false-alarm reds on RAGAS-only drops (validates the cross-family corroboration rule).
- At least one yellow diagnostic surfaces during the 4-week rollout (validates that the drift detection is sensitive enough to be useful).
- Operational red gates have fired and auto-closed at least once (validates the issue-management flow).
- A portfolio reader (or new contributor) can read `docs/evals.md` and explain the RAGAS methodology without reading runner source.

## Open Questions

- **Drop magnitudes** (RAGAS = -5pp; Claude faithfulness = -0.3; Claude helpfulness soft = -0.2) are proposed defaults. After 4 weeks of weekly runs, evaluate whether they need tuning based on observed week-to-week variance.
- **Failure-reason taxonomy completeness.** The 6-value enum (`judge_refused`, `parse_error`, `empty_contexts`, `metric_error`, `timeout`, `unknown`) is a first cut; may need additional categories once we observe real-world RAGAS failure modes.
- **`single-judge-red` auto-close window.** 2 weeks is a guess; reassess after the first such event fires.
- **`coverage-operational-failure` auto-close mechanics.** The "next green dispatch" condition needs precise implementation — is it the next scheduled run, or also manual dispatches? Recommend: only the next scheduled run auto-closes, manual dispatches do not (to avoid accidentally closing on a partial re-run).
- **First-real-run threshold tuning.** Initial drop magnitudes may need adjustment after observing actual week-to-week variance in production RAGAS scores. Plan to revisit after 4–6 weekly runs.
- **Cross-family corroboration has no live data source yet (found during US-007).** `check_score_regressions` corroborates a RAGAS drop against the Claude judge via `load_custom_judge_history`, which reads `docs/nightly/`. But `retrieval-eval-nightly.yml` runs the retrieval eval *without* `--include-generation`, so `docs/nightly/*.json` snapshots carry no Claude `faithfulness` / `helpfulness`. Until that changes, every `faithfulness` / `answer_relevancy` regression stays yellow (uncorroborated) and the corroborated-red path never fires on real data. Options: add `--include-generation` to `retrieval-eval-nightly.yml` so nightly snapshots carry Claude scores, or repoint `load_custom_judge_history` at `docs/ragas-weekly/` (US-008's weekly snapshots already include the Claude judge). Decide before relying on US-007 reds.
- **Coverage-drift gate is shadowed by the operational red floor (found during US-006).** US-006's yellow `coverage-drift` fires at `coverage < rolling_median − 5pp`; US-005's red `coverage-pipeline-failure` fires at the fixed `coverage < 0.96`. For any realistic rolling median (≤ 1.0) the yellow zone (`≤ 0.95`) sits entirely inside the red zone (`< 0.96`), so a `coverage-drift` yellow never appears without a red operational finding alongside it — the diagnostic gate adds no early-warning value for coverage as currently specified. (`api-error-drift` is **not** shadowed: its red counterpart triggers at `api_errors > 2`, leaving a `1–2` band where yellow can fire alone.) Options: shrink the coverage drift threshold to a small enough pp that yellow can fire while coverage is still above 0.96 (the viable pp depends on the median), scope `coverage-drift` to only `coverage ≥ 0.96`, or drop `coverage-drift` and keep only `api-error-drift`. Revisit once real weekly history exists.
