# ADR 0013: gpt-5-mini runtime deflection-gate judge, and the `reasoning_effort` knob

- **Status:** Accepted
- **Date:** 2026-08-21
- **On:** ADR-0006 (model-role separation), ADR-0003 (deterministic escalate-vs-answer control flow), ADR-0012 (Luna answerer, judges unchanged)

## Context

The runtime deflection-gate judge - the per-reply judge resolved by
`get_judge_model()` (`backend/escalation.py`), which drives BOTH
`faithfulness_gate` (grounding) and `answer_gate` (does the draft actually
answer) - shipped on `gpt-4o-mini` (`DEFAULT_JUDGE_MODEL`).

Two facts forced a re-look.
First, `gpt-4o-mini` is on a forced-retirement path, so a judge migration is
coming regardless.
Second, a benchmark that drove the REAL gate functions
(`faithfulness_gate` / `answer_gate`, unchanged, with their real system prompts,
the real `_judge_parse` structured-output call, and the real fail-closed control
flow `SEND iff faithful AND answers`) over the project's own escalate-vs-answer
labels in `evals/retrieval/escalation_gold.yaml` measured `gpt-4o-mini` as the
LOOSEST judge on the one axis these gates exist to defend:
it auto-sent **24%** of should-escalate turns (17% on the e7 gold alone) - the
Risk-#3 "false-resolve", a customer told a confident wrong or empty answer that
should have gone to a human.
Its faithfulness judge in particular waved ungrounded drafts through (it escalated
almost entirely via the answer gate).

`gpt-5-mini` was the highest-quality candidate in that benchmark:
it roughly **halves** the false-resolve rate (24% → 10% at default reasoning,
**→ 8%** at `reasoning_effort=minimal`), reaches the best accuracy (**0.94** on
the e7 gold), catches both the ungrounded-claim drafts and the restated-deferral
drafts the baseline sends, and stays cheap (a fraction of a cent per
conversation - cost was not a deciding axis; every candidate is sub-cent).

But `gpt-5-mini` carries two costs, and this ADR is where they are recorded as a
decision rather than discovered later:

1. **It cannot accept a `temperature`.** `gpt-5-mini` returns a `400` on any
   `temperature` value, including the pinned `0`. Because both gates splat the
   resolved sampling kwargs into every judge call and fail **closed** on any
   error, a `temperature` sent to `gpt-5-mini` 400s every call, escalates every
   turn, and - per **issue #105** - the latch site cannot tell that from a
   deliberate escalate, so affected conversations latch to `status='escalated'`
   permanently and repairing the config does not un-latch them. The only correct
   config is therefore `JUDGE_TEMPERATURE=none`, which omits the parameter.

2. **At default reasoning effort it is not latency-viable.** The two judge calls
   sit inline on the customer reply path; `gpt-5-mini` at default effort adds a
   2-call p95 of ~9.6s. Setting `reasoning_effort=minimal` drops that to ~3.4s
   (and, on this benchmark, *improves* accuracy 0.89 → 0.94 and cuts cost),
   making it shippable. The shipped `_judge_parse` did not pass `reasoning_effort`
   at all, so this required a code change.

## Decision

**Adopt `gpt-5-mini` as the runtime deflection-gate judge** (a captain decision),
with the required configuration:

- `JUDGE_MODEL=gpt-5-mini`
- `JUDGE_TEMPERATURE=none` (mandatory - see above; without it both gates fail
  closed and latch every conversation per issue #105)
- `JUDGE_REASONING_EFFORT=minimal` (without it, a ~9.6s-p95 latency regression on
  the reply path)

**This ADR ships the prerequisite code change only, not the production cutover.**
The actual `JUDGE_MODEL` / `JUDGE_TEMPERATURE` / `JUDGE_REASONING_EFFORT` flip is
an operator/deploy step; no `.env` or production config is changed here.

### The `reasoning_effort` code change

`escalation.py` gains a `JUDGE_REASONING_EFFORT` env resolver
(`get_judge_reasoning_effort()`) alongside `get_judge_model()` /
`get_judge_temperature()`, threaded into the one shared judge call through
`_judge_sampling_kwargs()` so BOTH gates pass it.
It mirrors the `JUDGE_TEMPERATURE` pattern with one deliberate difference:

- `get_judge_temperature()` has a shipped default value (`0.0`) and `none` is a
  typed-out opt-out that OMITS the parameter.
- `get_judge_reasoning_effort()` has **no** shipped default: unset (or blank) ⇒
  `None` ⇒ the kwarg is **OMITTED entirely**. It is splatted only when an
  operator explicitly sets it.

The omit-when-unset default is load-bearing and keeps the change a true no-op for
every existing deployment: `gpt-4o-mini` (the shipped judge) *rejects*
`reasoning_effort` outright as a non-reasoning model, and `gpt-5.4-mini` accepts
the parameter but *rejects the specific value* `minimal`.
An unset default therefore leaves every existing judge call byte-identical to its
pre-knob shape (`{"temperature": 0.0}`).
The module validates **no** value and keeps **no** allow-list: an explicitly set
value is passed through verbatim and the judge API is the authority that accepts
or rejects it - so a value one reasoning model takes and another refuses is the
operator's own deployment-matched choice, exactly as an out-of-range
`JUDGE_TEMPERATURE` is. A hardcoded value list here would go stale the moment a
new reasoning model shipped.

### The boot warning fires correctly for gpt-5-mini

`warn_if_judge_rejects_temperature()` (`escalation.py`) warns at boot when the
configured `JUDGE_MODEL` name matches a known temperature-refusing family AND the
temperature pin is still in effect.
`gpt-5-mini` matches `_TEMPERATURE_REFUSING_MODEL_PREFIXES` and DOES refuse
`temperature`, so this warning firing for it is **correct, not a false positive**:
it is telling an operator mid-upgrade to set `JUDGE_TEMPERATURE=none` before the
gates start 400ing.
Once the adopted config sets `JUDGE_TEMPERATURE=none`, the warning goes silent (it
returns early when the temperature is already omitted), which is the intended
end state. (Contrast ADR-0012 / the report's note on `gpt-5.4-mini`, where the
same warning would be a *false* positive because that model actually accepts
`temperature=0`.)

A second, symmetric warning covers the inverse migration slip.
`warn_if_judge_rejects_reasoning_effort()` (`escalation.py`) fires at boot when
`JUDGE_REASONING_EFFORT` is set but `JUDGE_MODEL` does **not** look like a known
reasoning family - the case of an operator who sets the value while leaving the
non-reasoning default `gpt-4o-mini` in place, which 400s on `reasoning_effort` and
latches conversations via the same issue #105 path.
It shares the one hand-maintained name list (`_name_is_temperature_refusing`),
takes the same widget-scoped `support_configured` gate, and is best-effort and
wrong in both directions (a reasoning model under an unrecognised name gets a
spurious warning), so its message stays conditional and tells the operator to
verify before changing config.
For the adopted `gpt-5-mini` config this warning stays silent (the model matches
the reasoning family), exactly as intended.

### The determinism trade (invariant 8), made deliberately

AGENTS.md invariant 8 pins `JUDGE_TEMPERATURE=0` specifically to REMOVE the
sampler as a source of send/escalate variance - the 2026-08-03 investigation
(issue #104) measured the answer gate returning `answers=True` on 2 of 5
identical calls for the same `(question, draft)`.
ADR-0012 rejected a reasoning-model runtime judge on exactly this ground.
This ADR **knowingly reverses that specific rejection for `gpt-5-mini`**:
because `gpt-5-mini` forces `JUDGE_TEMPERATURE=none`, the temperature-0 pin is
un-set and the sampler is re-introduced on the send/escalate verdict.
The benchmark measured the residual instability at 2 non-unanimous items / 27 at
`minimal` effort (vs 0/27 for the pinned baseline).
The captain accepts this: halving the dangerous false-resolve rate is judged the
higher-value outcome than a fully deterministic verdict that is loose 24% of the
time.
This is a **deliberate, scoped relaxation** of invariant 8's pin as an operator
configuration, NOT a change to the shipped code default: `DEFAULT_JUDGE_TEMPERATURE`
stays `0.0`, so a deployment that does not set `JUDGE_TEMPERATURE=none` still runs
pinned, and invariant 8's `none`-is-a-typed-out-opt-out escape hatch already
contemplated this path.

## Consequences

- The reasoning-effort knob is live for both gates and defaults to a no-op, so
  this change ships dark: no deployment behaviour moves until an operator sets
  `JUDGE_REASONING_EFFORT`. Coverage is pinned in `backend/test_faithfulness_gate.py`
  (semantics: omit-when-unset/blank, verbatim-when-set, and the byte-unchanged
  `gpt-4o-mini` default) and `backend/test_answer_gate.py` (the same knob threads
  through the second gate).
- Adopting `gpt-5-mini` un-pins the invariant-8 determinism guarantee for that
  deployment. A reader asking "why is the judge no longer temperature-pinned?"
  has the answer here: the model cannot hold the pin, and the false-resolve
  reduction is judged worth the residual sampling.
- A stricter judge escalates more turns to humans - a false-resolve reduction is
  an escalation-rate increase by construction. That rate shift, and any follow-up
  re-tuning of `ESCALATION_ANSWER_CUTOFF` / `ESCALATION_FAITHFULNESS_CUTOFF`, is
  **out of scope here** and gated on the E7 escalation-rate sweep against a live
  corpus, which is a pre-cutover validation step (it requires database access the
  code change does not).
- The boot warning firing for `gpt-5-mini` is now expected operator-facing
  behaviour on the upgrade path, resolved by setting `JUDGE_TEMPERATURE=none`.

## Alternatives considered and rejected

- **`gpt-5.4-mini` at `JUDGE_TEMPERATURE=0` (the determinism-preserving drop-in).**
  It uniquely keeps the temperature-0 pin (near-baseline latency, no
  `reasoning_effort` change) and is quality-equivalent to the baseline - but it
  does NOT deliver the false-resolve reduction (it still sends restated-deferral
  drafts like the baseline). Rejected because the whole point of the migration was
  to cut the dangerous false-resolve rate the gates exist to prevent.
- **`gpt-5-nano`.** Rejected outright: at default effort it is the slowest
  candidate (2-call p95 ~16.9s); at `minimal` effort it collapses into
  escalate-almost-everything (~60% false-escalate). No configuration is both fast
  and discriminating.
- **Doing nothing (stay on `gpt-4o-mini`).** A time-boxed option only, given the
  forced retirement, and it leaves the 24% false-resolve leak in place.
- **Hardcoding an allow-list of `reasoning_effort` values in `escalation.py`.**
  Rejected: it would reject a valid value the moment a new reasoning model shipped,
  the same staleness `_TEMPERATURE_REFUSING_MODEL_PREFIXES` warns about itself. The
  judge API validates the value instead.
