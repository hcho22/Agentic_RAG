# PRD: Migrate the answerer to GPT-5.6 Luna, leaving both judges unchanged

## Introduction/Overview

Purvia currently uses `gpt-4o-mini` as the default model for nine distinct roles.
This feature moves the two highest-volume generation paths - the main chat answerer and the support-widget answer drafter - onto **GPT-5.6 Luna** (`gpt-5.6-luna`, $0.20/$1.20 per MTok), while leaving every safety gate and every eval judge exactly where they are.

The work originated as a request to "change the LLM judge model for better output and cost savings."
Investigation showed that framing does not hold:

- **Judge cost is negligible.** The offline eval judge costs roughly $2/month. Its populations are small (E7 P2 = 4 rows, P3 = 9 rows, two gate calls each), `JUDGE_MAX_TOKENS` caps output at 200, and the 18-point knob sweep adds zero calls because `retrieve`/`draft`/`judge`/`answer_judge` are memoized per question (`evals/retrieval/e7_runner.py:2500`). `claude-haiku-4-5` is also already the cheapest Claude tier, so there is no saving available there at all.
- **Luna cannot serve either judge.** It is a reasoning model that forces `temperature=1`. The runtime gates pin `JUDGE_TEMPERATURE=0` because the 2026-08-03 investigation measured `answers=True` on 2 of 5 identical calls, and an escalate verdict latches `conversations.status` permanently. Luna also matches `_TEMPERATURE_REFUSING_MODEL_PREFIXES` at `backend/escalation.py:408` via its `gpt-5` prefix. Separately, using Luna as the **offline** judge would set `judge_family == generator_family` in `evals/gate/gate.yaml`, collapsing the cross-family independence that `docs/golden-set-authoring.md` §6-7 and ADR-0001 present as a buyer-facing claim.
- **Almost all generation cost is the answerer.** It scales linearly with customer traffic, which is exactly where Luna's price point pays.

ADR-0006's role separation makes this safe to do with almost no code: `backend/escalation.py:249` states that the judge selector deliberately does **not** chain through `OPENAI_MODEL`, so changing the answerer cannot drag the gates along.

## Resolved scope questions

These were settled during design review and are recorded here instead of being re-asked.

1. **Which judge is being changed?** Neither. Both the runtime gates and the offline eval judge stay exactly as they are.
2. **Is the `JUDGE_TEMPERATURE=0` pin negotiable?** No. It is a hard constraint. Any model that cannot hold it is disqualified from the runtime gates.
3. **Where does Luna go?** The answerer role (`OPENAI_MODEL`) and the eval's `GENERATION_MODEL`, which must move in lockstep.
4. **Are the incompatible helpers being ported?** No. They are pinned back to `gpt-4o-mini` using the per-call-site selectors US-023 already provides.

## Goals

- Serve the main chat answerer and the support-answer drafter from `gpt-5.6-luna`.
- Keep the eval's generator in lockstep with production so the weekly eval keeps measuring the shipped system.
- Prevent a partially-configured deployment from silently 400-ing on the four Luna-incompatible helpers.
- Leave `JUDGE_MODEL`, `JUDGE_TEMPERATURE`, and the offline `JUDGE_MODEL` untouched, with cross-family independence intact.
- Establish whether the projected cost saving is real, given Luna bills reasoning tokens as output.

## Background: the nine roles and why they split

| Role | Default set at | Selector | Luna-safe? | Why |
|---|---|---|---|---|
| Main chat answerer | `backend/main.py:219` | `OPENAI_MODEL` | Yes | `responses.create` (`main.py:1245`) and a `chat.completions.create` fallback (`main.py:1667`); no `temperature`, no `tools` |
| Support answer drafter | `backend/escalation.py:864` | `OPENAI_MODEL` | Yes | `chat.completions.create` with explicitly no `tools` and no `temperature` (`escalation.py:950`) |
| Metadata extraction | `backend/metadata.py:76` | `METADATA_MODEL` | Yes | `chat.completions.parse` with `response_format`, no `temperature`, no `tools`; Luna lists `structured_outputs` as supported |
| Query planner | `backend/planner.py:124` | `OPENAI_PLANNER_MODEL` | **No** | hardcoded `temperature=0.0` (`:308`) and `tool_choice="required"` (`:306`) |
| Text-to-SQL | `backend/text_to_sql.py:136` | `OPENAI_SQL_MODEL` | **No** | hardcoded `temperature=0.0` (`:322`) |
| LLM reranker | `backend/reranking.py:326` | `OPENAI_RERANK_MODEL` | **No** | hardcoded `temperature=0.0` (`:238`) |
| Document subagent | `backend/subagent.py:121` | `OPENAI_SUBAGENT_MODEL` | **No** | `tool_choice="auto"` (`:503`) |
| Runtime judge (both gates) | `backend/escalation.py:179` | `JUDGE_MODEL` | N/A | Out of scope; does not chain through `OPENAI_MODEL` |
| RAGAS judge | `evals/retrieval/ragas.py:74` | none | N/A | Dead code; `score_with_ragas` returns `[]` at `:156` |

The three hardcoded `temperature=0.0` literals are the crux: unlike `JUDGE_TEMPERATURE`, they have **no operator escape hatch**, so those call sites 400 on every request if pointed at Luna.

## User Stories

### US-119: Verify Luna compatibility on the two answerer call shapes

**Description:** As an engineer, I need to prove `gpt-5.6-luna` actually works on Purvia's answerer call shapes before any configuration ships, so that a migration does not break the customer-facing widget.

This story is **blocking**. If it fails, US-120 through US-124 do not proceed.

**Acceptance Criteria:**

- [ ] A short throwaway script exercises `chat.completions.create` against `gpt-5.6-luna` with no `tools` and no `temperature`, mirroring `escalation.py:950`, and returns a completion without error
- [ ] The same script exercises `chat.completions.parse` with a Pydantic `response_format`, mirroring `metadata.py:135`, and returns a parsed payload
- [ ] The same script exercises `responses.create`, mirroring `main.py:1245`, and returns a response
- [ ] Findings are recorded in the PR description, including the observed `reasoning_effort` default and whether reasoning tokens appear in the usage payload
- [ ] The script is NOT committed; it is verification scaffolding only
- [ ] Typecheck passes

**Validation Test:**

- **Setup:** `OPENAI_API_KEY` set with access to `gpt-5.6-luna`.
- **Steps:**
  1. Run the script's `chat.completions.create` case with a short support-style prompt and no `temperature` argument.
  2. Run the `chat.completions.parse` case with a two-field Pydantic model.
  3. Run the `responses.create` case.
  4. Record `usage` from each response.
- **Expected Result:** All three return successfully. No 400 mentioning `temperature`, `reasoning_effort`, or model accessibility. Usage shows a reasoning-token count.
- **Failure Indicator:** Any 400, a response indicating the model is unreachable via `/chat/completions`, or `response_format` being rejected. Any of these stops the migration and reopens the model choice.

### US-120: Move the eval generator to `gpt-5.6-luna`

**Status:** ✅ Done - merged in PR #115 (9737e1d).

**Description:** As an eval maintainer, I want the eval's answer generator to match production so that the weekly numbers describe the system we actually ship.

**Acceptance Criteria:**

- [x] `evals/retrieval/runner.py:216` reads `GENERATION_MODEL = "gpt-5.6-luna"`
- [x] The docstring at `runner.py:701` no longer says "via gpt-4o-mini"
- [x] The summary table header at `runner.py:1494` no longer says "Claude on gpt-4o-mini answers"
- [x] `evals/retrieval/runner.py:217` `JUDGE_MODEL = "claude-haiku-4-5"` is **unchanged**
- [x] `evals/gate/gate.yaml` `corroboration.generator_family` remains `openai` and `judge_family` remains `anthropic`
- [x] `python -m evals.retrieval.test_empty_gold_guard` passes
- [x] Typecheck passes

**Validation Test:**

- **Setup:** Local Supabase running, corpus seeded, `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` set.
- **Steps:**
  1. Run `python -m evals.retrieval.runner --include-generation`.
  2. Inspect the emitted JSON snapshot's `generation_model` and `judge_model` keys.
  3. Read the rendered markdown summary's generation-quality table header.
- **Expected Result:** `generation_model` is `gpt-5.6-luna`, `judge_model` is `claude-haiku-4-5`. The run completes and produces faithfulness/helpfulness scores. The table header names the new generator.
- **Failure Indicator:** The run errors, `judge_model` changed, or the snapshot still records `gpt-4o-mini` as the generator.

### US-121: Boot-time warning when the answerer is temperature-refusing and helpers are unpinned

**Status:** ✅ Done - merged in PR #116 (45f83287).

**Description:** As an operator, I want a startup warning when `OPENAI_MODEL` names a model that refuses `temperature` while a hardcoded-temperature helper is still inheriting it, so that I find out at boot rather than from a 400 on a live customer query.

This mirrors the existing `warn_if_judge_rejects_temperature` convention in `backend/escalation.py:427` and follows invariant 9 (ship the seam, then the call site).

**Acceptance Criteria:**

- [x] A pure, import-light helper reads the resolved answerer model and the four selectors (`OPENAI_PLANNER_MODEL`, `OPENAI_SQL_MODEL`, `OPENAI_RERANK_MODEL`, `OPENAI_SUBAGENT_MODEL`)
- [x] It logs one warning at boot naming each unpinned helper that would inherit a temperature-refusing answerer, and the exact env var that fixes it
- [x] It reuses the existing `_TEMPERATURE_REFUSING_MODEL_PREFIXES` tuple and the `_NON_REASONING_CHAT_MARKER` exclusion rather than adding a second list
- [x] It is best-effort and wrong in both directions, documented as such in the docstring, exactly like the judge warning
- [x] It **never blocks startup** and never mutates any model selection
- [x] It is not on the request path
- [x] A colocated unit test covers: pinned helpers produce silence, an unpinned helper under a refusing answerer warns, and a non-refusing answerer produces silence
- [x] Typecheck and lint pass

**Validation Test:**

- **Setup:** No Supabase or API keys required; the unit layer runs anywhere.
- **Steps:**
  1. Set `OPENAI_MODEL=gpt-5.6-luna` with all four helper selectors unset, then start the app.
  2. Set all four helper selectors to `gpt-4o-mini` and restart.
  3. Set `OPENAI_MODEL=gpt-4o-mini` with helpers unset and restart.
- **Expected Result:** Step 1 logs one warning naming all four helpers and their env vars. Step 2 is silent. Step 3 is silent. The app starts successfully in all three cases.
- **Failure Indicator:** Startup fails, the warning fires when helpers are correctly pinned, no warning fires in step 1, or the helper's model selection is altered.

### US-122: Document the model-role split and the pinning requirement

**Description:** As a deployer, I want the documentation to state which roles can take a reasoning model and which cannot, so that I can configure a Luna deployment correctly on the first try.

**Acceptance Criteria:**

- [ ] `README.md:223` `OPENAI_MODEL` row notes that reasoning models require the four helpers to be pinned
- [ ] `README.md:283` per-call-site selector row lists which helpers are incompatible with a temperature-refusing model and why
- [ ] `docs/model-surface.md` answerer-role section documents the same split, including the three hardcoded `temperature=0.0` sites by file and line
- [ ] `docs/model-surface.md` restates that `JUDGE_MODEL` does not chain through `OPENAI_MODEL`, and that this is what keeps the gates pinned
- [ ] The stale model reference in `evals/retrieval/ragas.py:12-17` is updated so it names the generator generically rather than `gpt-4o-mini`
- [ ] A copy-pasteable env block for a Luna deployment appears in the README
- [ ] No behavior changes in this story

**Validation Test:**

- **Setup:** A reader who has never seen this repo.
- **Steps:**
  1. Read only the README env table and the copy-pasteable block.
  2. Configure a deployment from it.
  3. Start the app.
- **Expected Result:** The reader pins all four helpers without consulting source, and US-121's warning stays silent.
- **Failure Indicator:** The warning fires, meaning the documentation was insufficient to configure correctly.

### US-123: ADR-0012 recording why the judges stay on `gpt-4o-mini`

**Description:** As a future maintainer, I want a written record of why the answerer runs Luna while four helpers and both judges are pinned to the old model, so that it reads as a decision rather than an oversight.

**Acceptance Criteria:**

- [ ] `docs/adr/0012-luna-answerer-judges-unchanged.md` exists, following the format of the existing committed ADRs
- [ ] Records the `JUDGE_TEMPERATURE=0` constraint, the measured 2-of-5 defect, and the permanent `conversations.status` latch as the reason judge cost is not the lever
- [ ] Records the cross-family constraint on the offline judge, citing `gate.yaml` and `docs/golden-set-authoring.md` §6-7
- [ ] Records the three hardcoded `temperature=0.0` literals as the reason helpers are pinned rather than migrated
- [ ] Records that `RAGAS_JUDGE_MODEL` is dead pending a real `score_with_ragas`
- [ ] Records the alternatives considered and rejected: Luna as runtime judge (needs unpinning), Luna as offline judge (breaks cross-family), full helper migration (rewrites the call surface)
- [ ] `CLAUDE.md`'s ADR list under "Where the full detail lives" is updated to include 0012

**Validation Test:**

- **Setup:** None.
- **Steps:**
  1. Read ADR-0012 alone, without the PR or this PRD.
  2. Answer: why is `JUDGE_MODEL` still `gpt-4o-mini`, and why is `OPENAI_PLANNER_MODEL` pinned?
- **Expected Result:** Both answers are available from the ADR text, each with a file-and-line citation.
- **Failure Indicator:** The reader must open source or the PR to answer either question.

### US-124: Measure Luna's real cost and latency on the support drafter path

**Description:** As a product owner, I want the projected cost saving verified rather than assumed, because Luna bills reasoning tokens as output at $1.20/MTok and defaults to `medium` reasoning effort.

**Acceptance Criteria:**

- [ ] Token usage for the support drafter is captured for a representative sample of turns on both `gpt-4o-mini` and `gpt-5.6-luna`, including reasoning tokens
- [ ] End-to-end widget turn latency is recorded for both, since the widget path is customer-facing
- [ ] Findings are written up with a per-1,000-conversation cost comparison
- [ ] If Luna is more expensive or materially slower at default effort, the writeup states the `reasoning_effort` value that would fix it and flags that as a follow-up code change
- [ ] Uses the existing LangSmith wiring where possible rather than adding new accounting machinery

**Validation Test:**

- **Setup:** Local Supabase, seeded corpus, widget configured, both API keys set.
- **Steps:**
  1. Run 10 identical widget conversation turns against `OPENAI_MODEL=gpt-4o-mini`, recording usage and wall-clock latency.
  2. Repeat against `OPENAI_MODEL=gpt-5.6-luna` with the four helpers pinned.
  3. Compute mean cost per turn and p50/p95 latency for each.
- **Expected Result:** A table showing both models' cost per turn and latency, with an explicit verdict on whether the saving is real.
- **Failure Indicator:** Reasoning tokens are not visible in the usage payload, making the comparison unmeasurable. That is itself a finding worth reporting.

## Functional Requirements

- **FR-1:** The eval's `GENERATION_MODEL` must be `gpt-5.6-luna` and must equal the production answerer model.
- **FR-2:** The system must not change `backend/escalation.py:179` `DEFAULT_JUDGE_MODEL` or the `JUDGE_TEMPERATURE` default.
- **FR-3:** The system must not change `evals/retrieval/runner.py:217` `JUDGE_MODEL`.
- **FR-4:** `evals/gate/gate.yaml` must keep `generator_family: openai` and `judge_family: anthropic`.
- **FR-5:** At boot, when the resolved answerer model matches a known temperature-refusing family and any of `OPENAI_PLANNER_MODEL`, `OPENAI_SQL_MODEL`, `OPENAI_RERANK_MODEL`, or `OPENAI_SUBAGENT_MODEL` is unset, the system must log one warning naming each affected helper and its env var.
- **FR-6:** The warning in FR-5 must never block startup and must never alter model selection.
- **FR-7:** The warning in FR-5 must reuse `_TEMPERATURE_REFUSING_MODEL_PREFIXES` and `_NON_REASONING_CHAT_MARKER`, not a second list.
- **FR-8:** Documentation must state, per role, which model roles accept a reasoning model and which do not, citing the blocking file and line.
- **FR-9:** A deployment configured solely from the README must produce no FR-5 warning.
- **FR-10:** ADR-0012 must record the decision and the rejected alternatives.

## Non-Goals (Out of Scope)

- **Changing either judge.** `JUDGE_MODEL`, `JUDGE_TEMPERATURE`, and the offline `claude-haiku-4-5` judge all stay as they are.
- **Removing the three hardcoded `temperature=0.0` literals** in `planner.py`, `text_to_sql.py`, and `reranking.py`. That is a separate decision with its own risk.
- **Porting the planner or subagent to `/v1/responses`** or setting `reasoning_effort="none"` on them.
- **Migrating the four incompatible helpers to Luna** at all.
- **Implementing `score_with_ragas`.** It stays a scaffold; the corroboration and drift gates stay inert.
- **Building measurement for the runtime judge.** E7 substitutes the offline Claude judge for the real gates (`e7_runner.py:3312-3323`), so runtime judge quality remains unmeasured. Noted, not fixed here.
- **Upgrading the offline judge to `claude-sonnet-5`.** Considered and deferred; roughly $4/month for sharper false-resolve discrimination.
- **Any new token-accounting framework.** US-124 measures with existing tooling.

## Technical Considerations

- **Provider surface is unchanged.** Luna is OpenAI, so `backend/model_config.py`'s `openai|azure` provider axis needs no work. Only the per-call-site model selector changes.
- **The judge is protected structurally, not by discipline.** `escalation.py:249` documents that `get_judge_model()` does not chain through `OPENAI_MODEL`. Do not "helpfully" add that chaining.
- **`METADATA_MODEL` is left unset deliberately** so metadata extraction follows `OPENAI_MODEL` onto Luna. It is compatible: structured outputs, no temperature, no tools.
- **Chat mode.** `main.py` resolves `responses` mode by default for an OpenAI-proper answerer (`resolve_chat_mode_default`). Luna is reported to work on `/v1/responses`, which is the default path. The `completions` fallback must also work; US-119 covers both.
- **The five offline guard modules** in the `eval-harness-guards` job must keep passing. This change touches `backend/escalation.py` (US-121), which re-triggers that job by `paths:`.
- **Cost note.** Luna's headline $0.20/$1.20 understates real cost because reasoning tokens bill as output. Cached input is $0.02/MTok, which may matter given the support drafter re-sends retrieved context each turn.

## Success Metrics

- Support-widget and chat turns serve from `gpt-5.6-luna` with a zero increase in 5xx or gate-error rate.
- Measured cost per support conversation is lower than on `gpt-4o-mini` (verified by US-124, not assumed).
- p95 widget turn latency does not regress materially.
- The weekly E7 snapshot after cutover still reports a false-resolve rate within the pinned ceiling.
- A deployment configured only from the README produces no boot warning.

## Open Questions

- Does `gpt-5.6-luna` accept `chat.completions.parse` with a Pydantic `response_format` on the first-party OpenAI API? One report of Luna being unreachable via `/chat/completions` came from a GitHub Copilot proxy, not OpenAI directly. US-119 settles this.
- At default `medium` reasoning effort, is the support drafter fast enough for a customer waiting on a widget? If not, adding `reasoning_effort` to the drafter call becomes a code change outside this PRD's current scope.
- The first weekly E7 run after cutover will move because the **generator** changed. Should that snapshot carry a dated note in `docs/escalation-weekly/README.md` errata so a later reader does not misattribute the movement to a pipeline regression? Recommended, but it is a judgment call.
- Should `METADATA_MODEL` be pinned to `gpt-4o-mini` for the initial cutover and relaxed later, trading a smaller cost win for a narrower blast radius?
