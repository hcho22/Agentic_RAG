# ADR 0010: Deterministic per-query alpha fusion (adaptive RRF weighting)

- **Status:** Accepted
- **Date:** 2026-07-11
- **On:** ADR-0002 (workspace tenant isolation), ADR-0003 (deterministic escalate-vs-answer control flow), ADR-0009 (keyword AND→OR fallback)

## Context

ADR-0009 revived the lexical leg so it returns signal instead of nothing. But
fusion was still equal-weight RRF: every query blends vector and keyword 50/50.
That is wrong in both directions. A neutral prose question ("how do refunds
work?") wants the vector leg to lead; an exact-token lookup (`ERR-4102`,
`WEBHOOK_RETRY_MAX`, a quoted phrase) wants the lexical leg to lead. A single
fixed weight cannot serve both, and the nightly eval
(`docs/nightly/2026-07-09.md`) shows the cost: hybrid MRR trails its own vector
leg overall (0.786 vs 0.796) and badly on the adversarial category (0.453 vs
0.503). Hybrid should never be worse than its best single leg.

US-115 landed the mechanism as an inert seam (core invariant 9): a pure
`predict_alpha(query)` and a weighted `_rrf_fuse`, both unit-tested, neither
wired. This ADR records the decision US-116 makes when it wires them into
`hybrid_search`.

## Decision

`hybrid_search` fuses with a **per-query, deterministically-computed weight**.

- **Weight comes from a feature function, never a model.** `predict_alpha(query)`
  reads four cheap features off the raw query string — quoted-phrase presence,
  identifier-shaped token count (snake_case, UPPER_SNAKE, code-like digit+symbol
  mixes, camelCase), digit density, and token count — and returns the vector-leg
  weight. No I/O, no LLM call. This mirrors aimee's `kb_fusion_predict_alpha` and
  keeps fusion inside ADR-0003's "deterministic control flow, never a model
  decision" stance: the same query always produces the same ranking, which is
  what makes the eval reproducible and the deflection path auditable.

- **The weights are `(alpha, 1 - alpha)`, vector ranking first.** `_rrf_fuse`
  gives ranking `i` a contribution of `2 * w_i / (k + r)`, so `(0.5, 0.5)`
  collapses to the legacy `1 / (k + r)` exactly. Neutral prose maps to
  `alpha = 0.5`, i.e. the adaptive path is *inert* on prose and only tilts as
  lexical cues appear.

- **`alpha` is clamped to [0.3, 0.7]; this clamp is load-bearing, not cosmetic.**
  Keyword-only rows carry `cosine_similarity = None` (they have no embedding). The
  US-046 escalation gate thresholds on the *raw cosine* of the fused top-k. If an
  identifier-heavy query drove `alpha` near 0, keyword-only rows would dominate
  the top-k, shrinking the cosine list the gate reads and potentially flipping an
  answer-vs-escalate decision on the widget path. Clamping the keyword leg's share
  to at most 0.7 guarantees the vector leg always retains enough presence to keep
  cosine-bearing rows in contention. Fusion weighting itself never mutates a
  per-row cosine (FR-5) — the clamp guards the *composition* of the top-k, not the
  values.

- **`HYBRID_FUSION_ALPHA` is the ops escape hatch.** The env knob takes `auto`
  (default, per-query `predict_alpha`) or a fixed float in [0, 1] that pins every
  query. `0.5` reproduces legacy equal-weight RRF byte-for-byte, so an operator
  who suspects adaptive fusion in an incident can revert it with an env change and
  no deploy. Validation follows `get_rrf_k()`: junk or out-of-range raises rather
  than silently defaulting.

- **The deflection pipeline inherits alpha by design.**
  `escalation.run_deflection_pipeline` calls `hybrid_search` directly, so the
  support bot gets adaptive fusion for free. This is deliberate: lexical customer
  queries (error codes, config keys pasted from logs) are exactly where the widget
  most needs the lexical leg up. The coupling is safe because it is guarded on
  four sides — the [0.3, 0.7] clamp (cosine list can't be starved), the per-PR
  E7 P1a/P1b escalation tripwire, the weekly LLM-judged escalation sweep, and the
  `HYBRID_FUSION_ALPHA=0.5` pin. Adaptive fusion changes *which* grounded chunks
  surface, not *whether* the gate fires; the gate's own thresholds are unchanged.

## Consequences

- Hybrid ordering is now query-shaped: identifier/quoted-phrase queries tilt
  toward the lexical leg, prose stays at the vector-led legacy behavior. The
  target is hybrid recall@5 and MRR ≥ vector on every golden-set category
  (including the new `lexical` one) and adversarial hybrid MRR back above 0.503.
- No interface change above `hybrid_search`: the backend `/api/chat` path, the
  `/api/search` endpoint, the rate limiter, and the deflection pipeline all call
  the same function with the same signature. The only new surface is the env knob.
- The escalation gate contract (US-046) is preserved: per-row cosine values are
  untouched, and the clamp bounds how far the fused top-k composition can drift.
  E4/E6 zero-leak is unaffected — fusion weighting is a ranking artifact over rows
  that already passed the (verbatim, ADR-0009) visibility predicate.
- Fixing the weight to `0.5` is a supported, tested configuration, not a fallback:
  `test_us116_alpha_wiring.py` pins that it reproduces legacy RRF exactly.

## Alternatives considered and rejected

- **A learned/LLM fusion weight.** Rejected — it reintroduces a model decision
  into the retrieval-and-escalation control flow (against ADR-0003), makes the
  eval non-reproducible, and adds a per-query latency and failure surface for a
  gain a cheap feature function already captures.
- **A single retuned fixed weight.** Rejected — no constant serves both prose and
  exact-token queries; that is the exact failure equal-weight RRF already shows.
  A fixed float remains available via the env knob for ops, not as the default.
- **An unclamped alpha (full [0, 1]).** Rejected — it lets identifier-heavy
  queries starve the escalation gate's cosine list and risks flipping widget
  answer-vs-escalate decisions. The [0.3, 0.7] clamp trades a little tilt range
  for a hard guarantee the gate always sees cosine-bearing rows.
- **Excluding the deflection path from adaptive fusion.** Rejected — the widget is
  where lexical customer queries concentrate, so opting it out would forgo the
  change's biggest win. The clamp plus the E7 tripwire and the ops pin make the
  inherited coupling safe rather than something to avoid.
