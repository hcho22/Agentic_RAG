# Permission-aware RAG: how Module 11 handles document sharing without breaking retrieval

> Status: shipped at v0 in Module 11 (US-037 → US-043). The numbers in
> sections 1 and 5 are auto-embedded between marker comments — to refresh
> after a runner change, run `python -m docs._embed_eval_summaries`.

## 1. The problem

The naive way to add per-document sharing to a RAG retriever is to leave
the vector search alone and **post-filter** the results: pull the top-k
chunks by similarity, then drop the ones the viewer can't see.

This works, until it doesn't. The failure mode is recall collapse on
viewers with sparse permissions. The math is simple: if a viewer can see
5% of the corpus and we ask for the top-10 most similar chunks, the
*expected* number of visible chunks in that result set is

> **E[visible | top-k] = k × selectivity = 10 × 0.05 = 0.5**

— half a chunk on average. The viewer most often sees zero relevant
chunks; they occasionally see one. Multi-hop questions that need two
chunks are essentially unanswerable. The fix isn't "fetch more candidates
and post-filter harder" — at 5% selectivity you'd need top-100 to expect
five visible chunks back, and post-filtering top-100 means embedding
distance is no longer ranking the *visible* chunks against each other.

The right fix is to push the permission check **into** the SQL predicate
so the SQL planner is choosing among visible candidates from the start
("pre-filter"). That sounds obvious until you try it on top of HNSW, at
which point the gotcha in section 4 shows up.

The Module 11 correctness eval proves the security property holds and
characterises the recall trade-off; the section 5 tables embed the
eval's output verbatim. The headline collapse from this math doesn't
appear in either of v0's empirical tables — the correctness eval's
14-chunk corpus is too small for ranking competition, and the scale
benchmark's 10k-chunk corpus is small enough that the Postgres planner
sidesteps HNSW altogether (section 5b walks through what `EXPLAIN`
shows). The math above is what the post-filter approach *would* look
like once the planner does walk HNSW; v0 ships the eval scaffolding so
the curve will surface the moment the corpus crosses that threshold.

## 2. The data model

Four small tables underpin permission-aware retrieval, all under
`public.`:

| Table | Purpose | Key columns |
|---|---|---|
| `principals` | Group identities (no users — those live in `auth.users`) | `id uuid pk`, `name text unique`, `kind = 'group'` |
| `principal_membership` | Which users belong to which group | `(principal_id, member_user_id)` pk |
| `chunk_acl` | Per-chunk grants — the source of truth for permissions | `(chunk_id, principal_type, principal_id)` pk |
| `profiles` | `auth.users(id, email)` mirror, for the granting-principal display | `id pk`, `email text` |

**Three load-bearing properties:**

**1. ACLs are additive to ownership.** A chunk's owner (its
`chunks.user_id`) always sees it; `chunk_acl` rows grant access to
*additional* principals. This means the existing single-user corpus
worked unchanged when the ACL system landed — no row backfill, no
"owner-row-per-chunk" bootstrapping. Ownership remains a property of the
row, not an entry in the grants table.

**2. `chunk_acl` is the sole source of truth.** There is no
`document_acl` "intent" table that would record "this document is shared
with viewer V" and then materialise per-chunk rows lazily. Doc-level
share operations (US-039 share endpoints, US-040 share dialog) iterate
the document's chunks and write one `chunk_acl` row per chunk, then
keep that materialisation consistent with re-chunking via the
**snapshot-and-replay** handler in `backend/permissions.py`: before
re-chunking, snapshot the document's grants into a `pending_acl_replay`
JSONB journal on `documents.metadata`; after `_reconcile_chunks` swaps
the chunk set, replay the journal against the new chunk IDs and clear
it. Crash-safe — a worker that dies between snapshot and replay
restarts and finishes the replay from the journal.

The trade-off: a viewer's grants are stored N times for an N-chunk
document. Storage is cheap; the alternative (intent table + planner
join) made the retrieval predicate one JOIN deeper, and the retrieval
predicate is the hottest query in the system. We picked storage over
read latency.

**3. Group nesting and workspace scoping are explicitly out.** A
principal cannot contain other principals (no group-of-groups). A grant
cannot be scoped to "the engineering workspace inside Acme org" (no
workspace tier above users). These are real production needs; section 6
says why they didn't ship in v0.

The `chunks` and `documents` RLS policies extend to grant access via
`chunk_acl` rows (`chunks_select_via_acl`, `documents_select_via_acl`)
so direct table queries — not just `match_chunks` — see the same
visibility set. Both policies wrap their subquery in a SECURITY DEFINER
helper (`_chunk_acl_grants_user`, `_document_has_acl_grant_for_user`)
to break a policy cycle: the doc-owner policy on `chunk_acl` queries
`chunks`, and the chunks-via-ACL policy on `chunks` queries `chunk_acl`.
Without the helper, the two policies recurse forever; with it, the inner
read runs as the function owner and skips RLS, but the predicate logic
is preserved.

## 3. The retrieval change

The pre-Module-11 `match_chunks` was a 4-line where clause:

```sql
where c.embedding is not null
  and d.deleted_at is null
  and (1 - (c.embedding <=> query_embedding)) >= match_threshold
```

— relying on the `chunks` and `documents` RLS policies (the function is
`SECURITY INVOKER`) to filter out cross-user rows. After US-037 the
predicate is restated explicitly inside the function, ORed with the ACL
branch:

```sql
where c.embedding is not null
  and d.deleted_at is null
  and (1 - (c.embedding <=> query_embedding)) >= match_threshold
  and (
    c.user_id = auth.uid()
    or exists (
      select 1
      from public.chunk_acl ca
      where ca.chunk_id = c.id
        and (
          (ca.principal_type = 'user' and ca.principal_id = auth.uid())
          or (
            ca.principal_type = 'group'
            and ca.principal_id in (
              select pm.principal_id
              from public.principal_membership pm
              where pm.member_user_id = auth.uid()
            )
          )
        )
    )
  )
```

Why restate the predicate when RLS already enforces it? Two reasons.
First, self-documenting: anyone reading the function sees the
visibility rule in one place rather than chasing five `create policy`
statements. Second, defence-in-depth — if a future RLS relaxation on
the `chunks` or `documents` tables widens visibility, the function still
filters correctly.

The `(principal_id, chunk_id)` index on `chunk_acl` is the load-bearing
one; it serves the EXISTS subquery's `principal_id = ?` lookup. The
composite primary key already supplies a `(chunk_id, …)` index for the
cascade-delete path and for `list_doc_shares` aggregation.

### Granting-principal precedence

US-041 added two new return columns — `granting_principal_id uuid` and
`granting_principal_display text` — that explain *why* the viewer can
see each chunk. The frontend uses `granting_principal_display` to render
a per-chunk badge ("via owner" / "via direct grant" / "via {group
name}"). When more than one rule grants the same chunk, precedence is:

1. Owner (`chunks.user_id = auth.uid()`)
2. Direct user grant (`principal_type='user' AND principal_id = auth.uid()`)
3. Group grant (via `principal_membership`)

Implementation is a `DISTINCT ON (c.id)` inner subquery with a
CASE-driven `ORDER BY` that applies the precedence; ties inside group
grants break on `chunk_acl.created_at ASC` then `principal_id ASC` so
the badge is stable run-over-run. The outer query re-sorts by HNSW
distance and applies LIMIT — the same shape `keyword_search` got in
the follow-up migration so the badge renders for keyword-only chunks
inside hybrid retrieval.

## 4. The HNSW interaction

Pushing the ACL predicate into the SQL predicate is correct — viewers
only see what they should — but it interacts badly with HNSW under
selective filters. This section names the gotcha and what was tuned.

**What HNSW does at query time.** pgvector's HNSW index is a
hierarchical proximity graph. A query starts at the top layer's entry
point, greedily walks toward the query vector, then descends and
repeats. The walk maintains a candidate priority queue; `ef_search`
(default 40) is its size. The walk terminates when the closest
unvisited candidate is farther than the worst kept candidate. Lower
`ef_search` = fewer candidates considered = faster but lower-recall
search; higher `ef_search` = more thorough graph walk, slower, higher
recall.

**Why selective filters hurt recall.** When the query has a `WHERE`
predicate (the permission check), the planner can use the HNSW index
for the `ORDER BY ... LIMIT` part and apply the filter as it walks. If
the filter rejects most candidates — at 1% selectivity, 99 out of every
100 candidates the walk sees fail the predicate — the walk can exhaust
its `ef_search` budget without finding `match_count` rows that pass.
Worse: the walk's "stop when closest unvisited > worst kept" termination
condition uses *all* visited candidates, not just the visible ones, so
the walk can stop short of the actually-relevant region of the graph.

The empirical shape: at high selectivity (50% visible), recall is flat
across `ef_search` because there are plenty of visible chunks anywhere
the walk goes. At low selectivity (1% visible), recall collapses at low
`ef_search` and recovers as the walk gets more thorough. The scale
benchmark in section 5 charts this curve.

**What we tuned.** The `match_chunks` function takes an optional
`ef_search int` parameter; when set, it does
`PERFORM set_config('hnsw.ef_search', ef_search::text, true)` (the
`true` makes the change local to the transaction) before the SELECT.
Production calls leave it null and the session/server default applies;
the scale benchmark (US-043) sweeps it and writes the recall curve.
Production pre-filter recall is acceptable on the typical sharing
distribution we expect (most users see most of their org's content);
operators who run into the recall floor on highly-sparse permission
distributions can bump the default and pay the latency.

**A note on when this even matters.** The phenomenon described above is
the *worst case*. The Postgres planner is not naïve: when the ACL
filter is selective enough that the visible-chunks set is small in
absolute terms (say, 100 out of 10k), the planner happily filters
first — bitmap-scan `chunk_acl` by `principal_id`, index-scan `chunks`
for those IDs, sort exactly by embedding distance, take top-k — and
ignores the HNSW index entirely. `ef_search` becomes a no-op in that
plan. The scale benchmark (section 5b) shows exactly this at 10k
chunks: every cell is 1.000 because no query actually walks HNSW. The
gotcha shows up at the scale where exact-NN over the filtered set is
*more* expensive than HNSW + post-filter — that is, when the visible
set is large in absolute terms but small relative to the corpus
(tens of thousands of visible chunks per query). The eval
infrastructure (seed, viewer setup, sweep, recall floor) is in place;
a >100k corpus run is the natural follow-up that should expose the
curve.

**Alternatives we explicitly didn't ship in v0.**

- **Partial index per principal** (`CREATE INDEX … WHERE
  user_can_see(...)`). Best query-time recall — the index *only*
  contains visible chunks, so HNSW walks them densely. Doesn't scale
  past a small number of distinct principals: each new group requires a
  new index, and pgvector index builds are not cheap. Reasonable for
  10–100 stable groups; not for the long tail of per-user direct
  grants.
- **IVFFlat instead of HNSW.** IVFFlat partitions the vector space into
  Voronoi cells; queries probe `nprobe` cells. Filter-aware query
  planning is *easier* on IVFFlat than HNSW because cell-by-cell scan
  composes naturally with predicate filters. Trade-off: IVFFlat needs
  the full corpus to build the cells and rebuild on significant data
  drift; recall is generally lower than tuned HNSW at the same query
  cost. Considered, deferred — the production retrieval path uses HNSW
  and the cost of swapping the index type wasn't justified by the
  recall delta we're seeing.
- **Two-stage retrieval** — fetch a wide candidate pool with no filter
  via HNSW, then filter, then re-rank. Rejected because the "wide pool"
  has to be very wide at low selectivity (back to the section 1 math)
  and the re-rank step would have to re-embed candidates the HNSW walk
  already visited. The pre-filter approach gets the same recall floor
  without the second pass.

## 5. The numbers

### 5a. Correctness eval (US-042) — security, recall trade-off, non-regression

50 questions × 3 modes × 3 viewer setups × 2 filter strategies, run
against the 14-chunk Acme corpus. The recall trade-off table shows
+0.000 deltas everywhere — expected on a 14-chunk corpus, where the
visible set is large enough that gold rarely gets pushed below top-5.
The eval's load-bearing claim here is **security**, not recall
differentiation: the section 5b note explains why the recall collapse
doesn't show up at v0's 10k-chunk scale either.

<!-- BEGIN EVAL_SUMMARY:retrieval -->

### Headline (mean across 50 questions)

| Mode | recall@5 | MRR | nDCG@5 |
|---|---|---|---|
| vector | 0.860 | 0.772 | 0.779 |
| keyword | 0.110 | 0.120 | 0.112 |
| hybrid | 0.860 | 0.759 | 0.769 |

### Per-category breakdown

| Mode | Category | recall@5 | MRR |
|---|---|---|---|
| vector | single_chunk | 0.900 | 0.825 |
| vector | multi_hop | 0.933 | 0.850 |
| vector | adversarial | 0.600 | 0.437 |
| vector | paraphrase | 1.000 | 1.000 |
| keyword | single_chunk | 0.250 | 0.250 |
| keyword | multi_hop | 0.033 | 0.067 |
| keyword | adversarial | 0.000 | 0.000 |
| keyword | paraphrase | 0.000 | 0.000 |
| hybrid | single_chunk | 0.900 | 0.825 |
| hybrid | multi_hop | 0.933 | 0.850 |
| hybrid | adversarial | 0.600 | 0.370 |
| hybrid | paraphrase | 1.000 | 1.000 |

### RAGAS comparison

<!-- EVAL_SUMMARY_RAGAS_START -->

_(RAGAS not run on this snapshot — pass --include-ragas to enable)_

<!-- EVAL_SUMMARY_RAGAS_END -->

### Security (US-042) — fraction of no_access runs that returned 0 gold chunks

| Mode | Pre-filter | Post-filter |
|---|---|---|
| vector | 1.000 | 1.000 |
| keyword | 1.000 | 1.000 |
| hybrid | 1.000 | 1.000 |

### Recall trade-off (US-042) — partial_access recall@5: pre-filter vs post-filter

| Mode | Category | Pre | Post | Δ (pre−post) |
|---|---|---|---|---|
| vector | overall | 0.900 | 0.900 | +0.000 |
| vector | single_chunk | 0.900 | 0.900 | +0.000 |
| vector | multi_hop | 1.000 | 1.000 | +0.000 |
| vector | adversarial | 0.700 | 0.700 | +0.000 |
| vector | paraphrase | 1.000 | 1.000 | +0.000 |
| keyword | overall | 0.110 | 0.110 | +0.000 |
| keyword | single_chunk | 0.250 | 0.250 | +0.000 |
| keyword | multi_hop | 0.033 | 0.033 | +0.000 |
| keyword | adversarial | 0.000 | 0.000 | +0.000 |
| keyword | paraphrase | 0.000 | 0.000 | +0.000 |
| hybrid | overall | 0.900 | 0.900 | +0.000 |
| hybrid | single_chunk | 0.900 | 0.900 | +0.000 |
| hybrid | multi_hop | 1.000 | 1.000 | +0.000 |
| hybrid | adversarial | 0.700 | 0.700 | +0.000 |
| hybrid | paraphrase | 1.000 | 1.000 | +0.000 |

### Non-regression (US-042) — full_access recall@5 vs Module-10 baseline

| Mode | Actual | Baseline | Δ | Within ±0.005? |
|---|---|---|---|---|
| vector | 0.860 | 0.670 | +0.190 | ✗ |
| keyword | 0.110 | 0.110 | +0.000 | ✓ |
| hybrid | 0.860 | 0.670 | +0.190 | ✗ |

<!-- END EVAL_SUMMARY:retrieval -->

What to read here:

- **Security** — every cell at 1.000 means no `no_access` viewer
  retrieved any gold chunk under either filter strategy. Pre-filter
  enforces this in SQL; post-filter enforces it via Python drop. Both
  pass; the pre-filter row is the load-bearing one (post-filter could
  in principle leak via timing, payload size, etc.).
- **Recall trade-off** — partial-access viewer recall@5, pre vs post,
  per (mode × category). Δ = pre − post; positive means pre-filter
  wins. The current corpus shows 0.000 because the visible set is too
  large for post-filter to push gold below top-5.
- **Non-regression** — full-access viewer recall@5 vs the rebaselined
  Module-10 numbers. ✓ means within ±0.005, which is the noise floor
  from OpenAI embedding jitter.

### 5b. Scale benchmark (US-043) — recall@5 vs ef_search × selectivity

15 multi-hop queries × 3 viewers (5000 / 1000 / 100 visible chunks of
10k) × 4 `ef_search` values, run against the wikipedia 10k synthetic
corpus. Recall is computed against the viewer's own top-5 at the
highest `ef_search` (=500), so the ef_search=500 column is 1.000 by
construction; the curve at 40 / 80 / 200 is what the table reports.

<!-- BEGIN EVAL_SUMMARY:permissions_scale -->

### Permissions scale: recall@5 vs ef_search × selectivity

_Wikipedia corpus, 10,000 chunks; mean across 15 multi-hop queries; 47.93s wall._

_Gold = top-5 returned at ef_search=500 (the most exhaustive sweep); lower ef_search values are scored by overlap with that set._

| Viewer | Visible chunks | Selectivity | ef_search=40 | ef_search=80 | ef_search=200 | ef_search=500 (gold) |
|---|---|---|---|---|---|---|
| viewer_50pct | 5,000 | 50.0% | 1.000 | 1.000 | 1.000 | 1.000 |
| viewer_10pct | 1,000 | 10.0% | 1.000 | 1.000 | 1.000 | 1.000 |
| viewer_1pct | 100 | 1.0% | 1.000 | 1.000 | 1.000 | 1.000 |

<!-- END EVAL_SUMMARY:permissions_scale -->

**Reading the result.** Every cell is 1.000 — at 10k chunks the recall
collapse does **not** manifest, because the Postgres planner doesn't
walk the HNSW index at all. `EXPLAIN ANALYZE` on the viewer_1pct query
(100 visible chunks of 10k) shows:

```
Limit  (cost=699.84..699.85 rows=1 width=24) (actual time=1.610..1.612 rows=5 loops=1)
  ->  Sort  (cost=699.84..699.85 rows=1 width=24) (actual time=1.610..1.610 rows=5 loops=1)
        Sort Method: top-N heapsort  Memory: 25kB
                          Index Cond: (id = ca.chunk_id)
```

The plan: bitmap-scan `chunk_acl` to get the 100 visible chunk_ids,
index-scan `chunks` for those 100 rows, sort *exactly* by embedding
distance, take top-5. No HNSW walk; `set hnsw.ef_search = …` is a no-op
when the index isn't used. The same shape repeats at the 10% and 50%
viewers — the planner sees the predicate is selective enough that exact
NN over the filtered set is cheaper than HNSW + post-filter.

This is the *correct* result, not a bug in the eval. At 10k chunks the
HNSW gotcha simply doesn't apply: the planner does the smart thing and
the v0 retrieval path is fine. The phenomenon section 4 names is real,
but it kicks in at corpus sizes where exact NN over the filtered set
becomes too expensive — tens to hundreds of thousands of visible chunks
per query. A 100k+ corpus benchmark is the natural follow-up; v0 ships
the eval infrastructure (10k seed, viewer ACL setup, ef_search sweep)
and the `--enforce-floor` regression alarm so the day the planner *does*
flip to HNSW for some workload, we'll see the curve in the table and
the nightly will fail loudly. Today the floor is set at recall@5 ≥ 0.10
for `(viewer_1pct, ef_search=40)`; with the planner choosing exact NN
the actual is 1.000.

## 6. Out of scope (deliberate)

The v0 cuts below were not skipped from forgetfulness. Each was
considered, sized, and traded off against the surface area of the
shipped feature. Naming them explicitly is the alternative to letting
a reviewer wonder.

| Cut | Why deferred |
|---|---|
| **Per-chunk override UI** | Doc-level grants serve 95% of the share intent; per-chunk grants happen behind the scenes via snapshot-and-replay but expose no end-user surface. Adding a UI multiplies the design space (revoke a single chunk? show denied chunks?) without changing the underlying data model — chunk_acl already supports per-chunk grants if a future story needs them. |
| **Share autocomplete** | The share dialog accepts a free-text email or group name and resolves server-side. Autocomplete needs an index over `auth.users` (privacy-sensitive; users shouldn't see other users' emails just to share with them) and a debounced typeahead component. Reasonable v1 — the v0 dialog is functional with copy-paste. |
| **Bulk operations** | "Share these 50 documents with this group" is one click in the dialog UX but N share-API calls under the hood. The doc-level snapshot-and-replay path already handles per-doc atomicity; a bulk endpoint mostly adds an outer transaction. Easy to add when a customer asks. |
| **Audit-log UI** | `chunk_acl.granted_by` records who granted each row; there's no UI surface that exposes the audit trail. Compliance-driven feature; defer until a compliance customer asks. |
| **Role hierarchies** | No "owner / editor / viewer" tiering. Every grant is read access. Hierarchies require a `role` column on `chunk_acl` and a precedence table for "what does an editor see vs an owner". Real ask in shared workspaces; v0 doesn't model write-vs-read because the only write surface (re-ingestion) is owner-only by design. |
| **Write-vs-read permission tiers** | Grants are read-only — no shared-edit flow. Closely related to role hierarchies; would need a re-ingestion / re-chunking authorisation check that distinguishes "can read" from "can mutate". |
| **Nested group membership** | A principal cannot contain other principals. The `principal_membership` join in `match_chunks` would become a recursive CTE, and depth-bounded recursion in PG is fine but the cost-of-nesting question (how deep? what's the worst-case fan-out?) needs production-shaped data to answer. v0 ships flat groups. |
| **Workspace scoping** | No tenancy tier above users. Multi-tenant deployment would route each tenant to its own Postgres schema or its own DB; that's a deployment-shape question, not a retrieval one. The retrieval path doesn't change. |

## 7. Refresh + reproduce

To regenerate the section 5 numbers after a runner change:

```bash
# Correctness eval — populates evals/retrieval/summary.md
python -m evals.retrieval.runner

# Scale benchmark — populates evals/permissions_scale/summary.md.
# Requires the wikipedia 10k seed first (~$0.10 OpenAI, ~3 min wall).
python -m db_seed.wikipedia_seed
python -m evals.permissions_scale.runner

# Embed both summaries into this doc (idempotent).
python -m docs._embed_eval_summaries
```

The embed script reads each `summary.md`, strips its outer
`EVAL_SUMMARY` markers, and replaces the bracketed region in this doc.
Adding a new embed target later means dropping a marker pair into the
doc and one line into `EMBEDS` in `docs/_embed_eval_summaries.py`.
