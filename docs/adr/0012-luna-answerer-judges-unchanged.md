# ADR 0012: Luna answerer, judges and temperature-pinned helpers unchanged

- **Status:** Accepted
- **Date:** 2026-08-20
- **On:** ADR-0006 (model-role separation), ADR-0001 (RAGAS as a parallel eval), ADR-0003 (deterministic escalate-vs-answer control flow)

## Context

Purvia set `gpt-4o-mini` as the default for nine distinct model roles. A
request to "change the LLM judge model for better output and cost savings"
prompted a review of where a model swap actually pays. The two highest-volume
generation paths - the main chat answerer (`OPENAI_MODEL`, `backend/main.py`)
and the support-widget answer drafter (`backend/escalation.py`) - scale with
customer traffic, so they are where a cheaper-per-token reasoning model
(`gpt-5.6-luna`, $0.20/$1.20 per MTok) earns its keep.

The original framing - swap the *judge* - does not hold. The judge populations
are tiny (single-digit rows, output capped by `JUDGE_MAX_TOKENS`), so the
offline judge costs roughly $2/month; there is no saving to capture there. More
importantly, the roles are not interchangeable: three of them cannot take a
reasoning model at all, and the two judges are load-bearing safety/measurement
surfaces that a reasoning model would actively break. This ADR records why the
answerer moves to Luna while both judges and four helpers stay on
`gpt-4o-mini`, so the split reads as a decision rather than an oversight.

## Decision

Serve the answerer and support drafter from `gpt-5.6-luna`, and keep the
runtime judge, the offline eval judge, and the four temperature-hardcoded
helpers on `gpt-4o-mini`.

The role separation is what makes this safe with almost no code:
`get_judge_model()` (`backend/escalation.py:245`) deliberately does **not**
chain through `OPENAI_MODEL` (the explanatory comment is at
`backend/escalation.py:248-249`), so moving the answerer cannot drag the gates
along. `DEFAULT_JUDGE_MODEL = "gpt-4o-mini"` sits at
`backend/escalation.py:179`.

**Why judge cost is not the lever.** The runtime gates are SAFETY gates that
must fail closed, which means they must be *deterministic* on identical input.
They pin `JUDGE_TEMPERATURE=0` (`DEFAULT_JUDGE_TEMPERATURE = 0.0`,
`backend/escalation.py:311`) because the 2026-08-03 investigation measured the
answer gate returning `answers=True` on **2 of 5 identical calls** for the same
`(question, draft)` pair (issue #104; the measurement comment is at
`backend/escalation.py:261-264`). A non-deterministic verdict is not a cosmetic
flake here: an escalate verdict latches `conversations.status` permanently. That
latch is DB-enforced by the one-way trigger in
`supabase/migrations/20260623130000_conversation_status_machine.sql`
(`active -> escalated -> resolved`, `resolved` terminal), so a sampled verdict
can silence a bot forever or resolve a ticket the bot never actually answered.
Luna is a reasoning model that forces `temperature=1` and matches
`_TEMPERATURE_REFUSING_MODEL_PREFIXES` (`backend/escalation.py:408`) via its
`gpt-5` prefix, so it cannot hold the pin and is disqualified from the runtime
gates.

**Why the offline eval judge stays cross-family.** The retrieval eval pairs an
OpenAI generator with an Anthropic judge on purpose (ADR-0001): same-model
scoring bias is avoided by keeping `judge_family != generator_family`. The eval
now generates with Luna (`GENERATION_MODEL = "gpt-5.6-luna"`,
`evals/retrieval/runner.py:217`, US-120) and judges with
`claude-haiku-4-5` (`evals/retrieval/runner.py:218`). Pointing the judge at Luna
too would set `judge_family == generator_family` and collapse the cross-family
independence that `evals/gate/gate.yaml:88-90`
(`corroboration.generator_family: openai` / `judge_family: anthropic`) and
`docs/golden-set-authoring.md` §6-7 present as a buyer-facing claim.

**Why the four helpers are pinned, not migrated.** Three helpers hardcode
`temperature=0.0` with no operator escape hatch, so they 400 on every request if
pointed at a temperature-refusing model: the planner
(`backend/planner.py:308`), text-to-SQL (`backend/text_to_sql.py:322`), and the
LLM reranker (`backend/reranking.py:238`). The document subagent is
Luna-incompatible for a separate reason (`tool_choice="auto"`). All four are
pinned back to `gpt-4o-mini` through the per-call-site selectors US-023 already
provides (`OPENAI_PLANNER_MODEL`, `OPENAI_SQL_MODEL`, `OPENAI_RERANK_MODEL`,
`OPENAI_SUBAGENT_MODEL`); US-121 adds a boot-time warning when an unpinned
helper would inherit a temperature-refusing answerer.

**`RAGAS_JUDGE_MODEL` is dead.** `RAGAS_JUDGE_MODEL = "gpt-4o-mini"` exists
(`evals/retrieval/ragas.py:75`) but `score_with_ragas` is a scaffold that
returns `[]` (`evals/retrieval/ragas.py:157`). There is no judge call to move,
so the value stays put pending a real implementation; the corroboration and
drift gates remain inert until then.

## Consequences

- A reader asking "why is `JUDGE_MODEL` still `gpt-4o-mini`?" has the answer
  here: the runtime pin (`JUDGE_TEMPERATURE=0` at
  `backend/escalation.py:311`, the 2-of-5 defect, the permanent
  `conversations.status` latch) plus the offline cross-family requirement
  (`evals/gate/gate.yaml:88-90`).
- A reader asking "why is `OPENAI_PLANNER_MODEL` pinned?" has it too: the
  hardcoded `temperature=0.0` at `backend/planner.py:308` (and its siblings at
  `backend/text_to_sql.py:322`, `backend/reranking.py:238`) 400 on Luna.
- The gates are protected structurally, not by discipline: because
  `get_judge_model()` does not chain through `OPENAI_MODEL`, a future answerer
  change cannot silently make the per-reply gate expensive or non-deterministic.
  Do not "helpfully" add that chaining.
- The eval generator moves in lockstep with the production answerer (FR-1), so
  the weekly numbers keep describing the shipped system. The first post-cutover
  E7 snapshot will move because the *generator* changed, not because of a
  pipeline regression.

## Alternatives considered and rejected

- **Luna as the runtime judge.** Rejected - it requires un-pinning
  `JUDGE_TEMPERATURE`, which reintroduces the measured 2-of-5 non-determinism on
  a safety verdict that permanently latches `conversations.status`.
- **Luna as the offline eval judge.** Rejected - it sets
  `judge_family == generator_family`, collapsing the cross-family independence
  (`evals/gate/gate.yaml:88-90`, `docs/golden-set-authoring.md` §6-7, ADR-0001)
  that the eval presents as a buyer-facing claim.
- **Full migration of the four helpers to Luna.** Rejected - it would rewrite
  the call surface (removing the hardcoded `temperature=0.0` safety literals at
  `backend/planner.py:308`, `backend/text_to_sql.py:322`,
  `backend/reranking.py:238`, and re-plumbing the subagent's `tool_choice`) for
  no traffic-proportional saving. That is a separate decision with its own risk.
