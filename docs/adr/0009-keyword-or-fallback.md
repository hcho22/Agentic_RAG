# ADR 0009: Keyword search AND→OR fallback (fallback, not replacement)

- **Status:** Accepted
- **Date:** 2026-07-10
- **On:** ADR-0002 (workspace tenant isolation)

## Context

The lexical leg of hybrid retrieval is structurally dead. `public.keyword_search`
(live definition `supabase/migrations/20260624150100_keyword_search_workspace_filter.sql`)
builds its query with `websearch_to_tsquery('english', query)`, which ANDs every
non-stopword term of the input. For a short exact-token lookup (`ERR-4102`,
`WEBHOOK_RETRY_MAX`) that is correct. But the widget and assistant pass full
natural-language questions, and `websearch_to_tsquery` requires *every* content
word to be present in a chunk. One question word the chunk happens not to contain
zeroes the whole match.

The nightly eval (`docs/nightly/2026-07-09.md`) measures the damage: keyword-mode
recall@5 is 0.140 overall and 0.000 on paraphrase questions. Fused via equal-weight
RRF, this dead leg actively drags hybrid *below* its own vector leg (hybrid MRR
0.786 vs vector 0.796 overall; 0.453 vs 0.503 on the adversarial category). Hybrid
retrieval should never be worse than its best single leg.

This ADR covers the first of two fixes in the retrieval-quality pass: reviving the
lexical leg inside the SQL function so nothing above it changes. (The second,
adaptive fusion weighting, is ADR-0010.)

## Decision

`keyword_search` gains an **AND→OR fallback** entirely inside the SQL function.

- **AND is primary; OR only fills empty slots.** The current
  `websearch_to_tsquery` AND query (`tsq_and`) runs first. A second query
  (`tsq_or`) rebuilds the same input as an OR over its normalized lexemes -
  `to_tsquery('english', array_to_string(tsvector_to_array(to_tsvector('english', query)), ' | '))`.
  Every candidate chunk is tagged `match_tier` 1 (matched AND) or 2 (matched OR
  only), the final `order by` is `match_tier asc, similarity desc, id asc`, and the
  existing `limit match_count` is unchanged. So OR rows can only reach slots that
  AND-matching rows did not already fill.

- **Fallback, not replacement.** A query that already filled `match_count` under
  AND returns bit-for-bit identical output: the same rows, the same
  `ts_rank_cd(content_tsv, tsq_and)` similarity values, and the same order (the
  `limit` cuts before any tier-2 row is reached). The change is purely additive -
  it can only turn a previously-empty slot into a real hit, never displace or
  re-rank an existing AND hit. This is what lets US-114 land ahead of the adaptive
  fusion work (ADR-0010) without perturbing any query that was already healthy.

- **AND ranked above OR, deliberately.** An AND match means the chunk contains
  *all* the query's content words; an OR match means it contains at least one. The
  first is a strictly stronger signal, so tier ordering (not a blended score)
  keeps strong matches on top and treats OR purely as a recall floor. Blending the
  two `ts_rank_cd` scales into one ranking was rejected: the scales are not
  comparable across two different tsqueries, and a high OR score on a
  single-common-word chunk could then outrank a genuine AND match.

- **Stopword-only queries return empty, not error.** For an all-stopword or empty
  input, `to_tsvector` yields an empty vector, so the rebuilt OR query has zero
  nodes. A `numnode(...) > 0` guard maps that to `null::tsquery`, which matches
  nothing - the function returns no rows rather than raising. Building `tsq_or`
  from already-normalized lexemes (rather than the raw text) also means no user
  input reaches `to_tsquery` as parseable operator syntax.

- **The trust boundary is untouched.** The entire visibility predicate
  (owner-OR-ACL, the workspace-membership `EXISTS`, `d.deleted_at is null`, the
  `filter_workspace_id` narrowing filter, and the metadata filters) and the
  granting-principal projection are copied verbatim from the live definition. OR
  widens which chunks are *considered*, but every candidate still passes the
  identical gate before it can be returned. `role` / `is_bot` appear nowhere in
  the function (core invariant 1). The migration keeps the byte-identical
  7-parameter signature, return table, `security invoker`, and `grant execute`, so
  the backend caller (`backend/retrieval.py::keyword_search`) is unchanged.

## Consequences

- Keyword-mode recall rises materially (paraphrase leaves 0.000) with no interface
  change above the SQL function - the backend, rate limiter, and deflection
  pipeline see the same RPC.
- Any query that was already returning a full result set is unaffected: same rows,
  same similarities, same order. The fallback is invisible except where it helps.
- E4/E6 zero-leak is preserved by construction (the predicate is verbatim and OR
  only enlarges the pre-gate candidate set), and is re-verified by
  `python -m backend.test_au4_auth_attacks` plus the per-PR E4/E6 eval gates.
- The similarity scale is now tier-dependent (tier-1 rows carry AND ranks, tier-2
  rows carry OR ranks). Downstream consumers use these only for ordering within the
  keyword leg and as RRF rank inputs, both of which are order-only, so the mixed
  scale is safe. `cosine_similarity` is a `match_chunks` concern and is not touched
  here (the US-046 escalation-gate contract lives on the vector leg).

## Alternatives considered and rejected

- **Replace AND with OR outright.** Rejected - OR alone floods short exact-token
  lookups with weakly-related chunks and discards the strong all-terms-present
  signal. The fallback keeps AND's precision where AND already works.
- **Blend the AND and OR `ts_rank_cd` scores into a single ranking.** Rejected -
  the two ranks come from different tsqueries and are not comparable, so a blended
  score can let a one-common-word OR hit outrank a full AND hit. Tiering is
  unambiguous and preserves backward-compatible output.
- **Do the fallback in `backend/retrieval.py` (two RPC calls, merge in Python).**
  Rejected - it doubles the round-trips, splits the visibility predicate across two
  call sites, and leaks a retrieval-quality concern into the backend. Keeping it
  inside the function means the whole change is one migration with zero backend
  diff.
- **Use plainto_/phraseto_ or trigram similarity instead of an OR tsquery.**
  Rejected for this pass - the OR-over-normalized-lexemes construction reuses the
  existing `content_tsv` GIN index and the same `english` normalization as AND, so
  it needs no new index and no new failure modes. Trigram/reranker gains are
  measured separately in the US-117 bake-off.
