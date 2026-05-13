# Structured RAG: a semantic-layer query planner for text-to-SQL

## 1. The problem with naive text-to-SQL

Hand a modern LLM a schema and a natural-language question and it will, more often than not, produce a runnable SQL statement. That's not the same as producing the *right* one.

Consider a five-table CRM with an `orders` table that looks like this (abbreviated):

```sql
crm.orders(id, customer_id, status, subtotal, tax, shipping, discount, total, created_at, paid_at, shipped_at)
crm.refunds(id, order_id, amount, reason, created_at)
```

Now ask: **"what is our revenue this quarter?"**

A naive text-to-SQL tool sees five revenue-flavored columns on `orders` (`subtotal`, `tax`, `shipping`, `discount`, `total`) and a `refunds.amount` that doesn't appear in `orders`. The "right" SQL depends on what *revenue* means to the business, and there are at least four legitimate readings:

- `SUM(total)` — billed amount across all orders, including cancelled ones.
- `SUM(total) WHERE status <> 'cancelled'` — billed amount we actually expect to collect.
- `SUM(total) WHERE status <> 'cancelled'` minus `SUM(refunds.amount)` — net revenue after refunds.
- `SUM(subtotal)` — pre-tax, pre-shipping merchandise revenue.

The model will pick *one* of these and run it. Sometimes it guesses right. Sometimes it picks `SUM(total)` and counts cancelled orders. Sometimes it ignores refunds entirely. The choice is invisible to the user: they see a number, not a definition.

This is the gap structured-data agents have to close. The model isn't bad at SQL — it's that "revenue" doesn't have a single SQL definition.

## 2. Approach: a Cube-style semantic layer + a two-step planner

The fix isn't to make the LLM write better SQL. It's to take the *semantic choice* out of the SQL-generation step entirely.

Two artifacts:

**A hand-authored semantic layer** at `backend/semantic_layer.yaml`. Defines entities (the five CRM tables), dimensions (groupable columns with synonyms), metrics (aggregate expressions with hand-written SQL fragments and synonyms), and joins (the entity graph). A representative metric block:

```yaml
metrics:
  net_revenue:
    description: Gross revenue minus the sum of refund amounts. The canonical "what we actually earned" figure.
    sql_fragment: "(SELECT COALESCE(SUM(o.total), 0) FROM crm.orders o WHERE o.status <> 'cancelled') - (SELECT COALESCE(SUM(r.amount), 0) FROM crm.refunds r)"
    grain: order
    entities: [orders, refunds]
    kind: scalar
    synonyms: [net revenue, revenue net of refunds, net sales, earnings, take-home revenue]
```

Eight hero metrics — `gross_revenue`, `net_revenue`, `subtotal_revenue`, `aov`, `gross_margin`, `active_customers_90d`, `repeat_customers`, `order_count` — cover the ambiguity surface that drives most real BI questions.

**A two-step query path** in the agent loop. The model never sees the schema directly. Instead it sees two tools, called in order:

1. **`plan_query(question)`** — the LLM maps the question onto the semantic layer via OpenAI function-calling. Returns either `{status: "matched", plan: PlanSpec}` or `{status: "no_match", reason, suggested_fallback}`. The plan is a structured object: `{metrics, dimensions, filters, time_grain}`.

2. **`sql_search(plan)`** — a *deterministic* compiler turns the plan into SQL. No LLM call here. The compiler reads metric `sql_fragment`s from the YAML, attaches the joins it needs, splices in dimensions, applies filter clauses through parameterized bindings, and executes against a read-only Postgres role.

The architectural commitment that makes the whole thing work: `sql_search`'s tool schema *requires* a structured `plan` argument. The agent can't reach the SQL execution step without first running `plan_query` — OpenAI's function-calling validator rejects the call. Skipping the planner isn't a soft preference; it's not expressible.

The result is that **semantic resolution and SQL generation are now separable concerns**. The LLM resolves "revenue" → `gross_revenue` vs `net_revenue` in step 1, where the semantic layer's synonyms and descriptions are right next to the choices. Step 2 just compiles. Given the same plan, you get the same SQL every run — a property that matters for both debuggability and reproducible evals.

A simplified flow:

```
question  ─► plan_query (LLM + function-calling)
                  │
                  ├─ matched ─► sql_search ─► compile(plan) ─► validate ─► execute ─► rows
                  └─ no_match ─► fall back to file_search / web_search / "out of scope"
```

## 3. Implementation notes

Three pieces of the implementation are worth calling out — they're where the architecture earns its keep.

**The planner's prompt is the semantic layer, rendered.** `backend/planner.py:_format_semantic_layer_block` flattens the YAML into a compact text block: every entity, dimension, and metric with its synonyms listed inline. The system prompt then instructs the model to call one of two function-call tools (`submit_matched_plan` or `submit_no_match`) with `tool_choice="required"`, so the model can't free-text its way out. A model that hallucinates a metric called `revenue_excluding_returns` is caught by a cross-check in `_validate_plan_against_layer`, which downgrades the matched response to a `no_match` rather than passing the bad metric forward to the compiler.

**The compiler is a graph traversal, not a generator.** `backend/sql_compiler.py:compile_plan` does no language modeling. It unions the entities referenced by metrics + dimensions + filter dimensions, picks a FROM root that has the most direct connections in the needed set (deterministic tiebreak by name), BFS over the join graph to attach LEFT JOINs in a stable order, and splices metric `sql_fragment`s alongside dimension column references in the outer SELECT. Time-kind dimensions wrap in `date_trunc(grain, col)` when the plan sets a `time_grain`. Filter values bind via asyncpg's positional parameters (`$1`, `$2`, …); `in` filters use `= ANY($1)` so the parameter count stays at one regardless of list size. The compiled SQL passes through the existing `validate_sql_safety` guard from US-023 as defense in depth — if the compiler ever drifts to emit a forbidden keyword, the safety check catches it before execution.

**Inline vs scalar metrics.** Most metrics compose with dimensions cleanly: `SUM(orders.total) FILTER (WHERE status <> 'cancelled')` joins to whatever entities the dimensions add, then groups by them. But some metrics (`net_revenue`, `repeat_customers`) need correlated subqueries to compute correctly, and those can't be combined with an outer `GROUP BY` without breaking the math. The YAML marks them `kind: scalar`, and the compiler refuses to combine a scalar metric with dimensions. The planner's prompt knows about this constraint and is supposed to route around it; if it doesn't, the compiler raises a `CompileError` that surfaces to the agent as a tool error.

A nuance worth noting: this design moves the *interesting* part of "AI text-to-SQL" up the stack. The LLM's job isn't to write SQL — it's to make a semantic choice (which metric, which dimension). The SQL is correct by construction once that choice is made. A skeptic could argue this is less impressive than end-to-end LLM SQL generation. The opposite is true: it's how every production BI agent (Cube, dbt-metrics, Cortex Analyst) actually works, because end-to-end LLM SQL doesn't survive contact with a wide schema.

## 4. Evaluation

A 30-question hand-authored eval compares the two paths against a hand-written gold reference SQL for each question, executed at eval-time against the seeded `crm` schema. Scoring is binary per question via normalized result-set match — rows sorted lexicographically by stringified cells, numerics rounded to 2dp, column names ignored.

**Question distribution:**

| Category | n | What it stresses |
|---|---|---|
| Metric ambiguity | 15 | The headline failure mode — "revenue", "active customer", "AOV" map to multiple plausible SQL expressions. |
| Join / dimension | 9 | Joins across `customers` ↔ `orders` ↔ `order_items` ↔ `products` through the semantic layer's join graph. |
| Time-grain / filter | 6 | `date_trunc(month, …)` bucketing and `BETWEEN` / status filters. |

**Methodology details:**

- **Naive baseline** uses `backend.text_to_sql.generate_sql_naive` — the same function the Module 7 `query_database` tool used to call. Sees an `information_schema.columns` dump of the `crm` schema (no metric definitions, no synonyms). LLM generates one SELECT, the safety validator checks it, asyncpg executes.
- **Semantic** uses the same pipeline the agent uses in production: `plan_query` → `compile_plan` → `validate_sql_safety` → asyncpg execute.
- **Gold** runs hand-written reference SQL from `evals/structured_rag/gold.yaml`. Each reference query is independently authored — neither the naive prompt nor the semantic compiler ever sees it.
- Both paths use `gpt-4o-mini` for the LLM step. The eval is fully deterministic: the seed RNG (`20260513`) is fixed, gold SQL is checked in, and the semantic compiler is byte-stable given the same plan.

**Run yourself:**

```bash
export CRM_DATABASE_URL=postgresql://crm_readonly:crm_readonly_dev_only@localhost:54322/postgres
export OPENAI_API_KEY=sk-...
python -m supabase.seed.crm_seed       # one-time seed
python -m evals.structured_rag.runner
```

The runner writes `evals/structured_rag/results.json` (full per-question detail) and `evals/structured_rag/summary.md` (the headline number, per-category table, and three naive-vs-semantic before/after examples).

<!-- BEGIN EVAL_SUMMARY (regenerated by evals/structured_rag/runner.py) -->

_Generated by `python -m evals.structured_rag.runner` at 2026-05-13T13:12:58 (eval ran in 74.4s)._

**Headline:** naive **10.0%** vs semantic **80.0%** — Δ **+70.0pp** on n=30 questions.

### Per-category accuracy

| Category | n | Naive | Semantic | Δ |
|---|---|---|---|---|
| join | 9 | 0.0% | 88.9% | +88.9pp |
| metric | 15 | 6.7% | 93.3% | +86.6pp |
| time | 6 | 33.3% | 33.3% | +0.0pp |

### Per-question outcome

| ID | Category | Naive | Semantic | Question |
|---|---|---|---|---|
| q01 | metric | ❌ | ✅ | What is our total revenue? |
| q02 | metric | ❌ | ✅ | What is our net revenue? |
| q03 | metric | ❌ | ✅ | What is our gross revenue? |
| q04 | metric | ❌ | ✅ | What is our revenue after refunds? |
| q05 | metric | ❌ | ✅ | What is our subtotal revenue, before tax and shipping? |
| q06 | metric | ❌ | ✅ | What is our merchandise revenue? |
| q07 | metric | ❌ | ✅ | What is our average order value? |
| q08 | metric | ❌ | ✅ | How many orders have we processed? |
| q09 | metric | ❌ | ✅ | How many transactions do we have? |
| q10 | metric | ❌ | ❌ | What is our gross margin? |
| q11 | metric | ❌ | ✅ | How many repeat customers do we have? |
| q12 | metric | ✅ | ✅ | What is the total billed amount across non-cancelled orders? |
| q13 | metric | ❌ | ✅ | What is our take-home revenue? |
| q14 | metric | ❌ | ✅ | How many returning customers placed more than one order? |
| q15 | metric | ❌ | ✅ | What are our total sales? |
| q16 | join | ❌ | ✅ | What is our gross revenue by country? |
| q17 | join | ❌ | ❌ | How many orders did we have by status? |
| q18 | join | ❌ | ✅ | What is our gross revenue by customer segment? |
| q19 | join | ❌ | ✅ | What is the average order value by country? |
| q20 | join | ❌ | ✅ | What is our gross margin by product category? |
| q21 | join | ❌ | ✅ | What is our subtotal revenue by country? |
| q22 | join | ❌ | ✅ | What is the order count by country? |
| q23 | join | ❌ | ✅ | What is our gross revenue by order status? |
| q24 | join | ❌ | ✅ | What is the order count by country and segment? |
| q25 | time | ❌ | ✅ | What is our gross revenue by month? |
| q26 | time | ❌ | ❌ | What is our gross revenue by quarter? |
| q27 | time | ❌ | ❌ | How many orders did we have between January and March 2026? |
| q28 | time | ✅ | ❌ | How many paid orders do we have? |
| q29 | time | ❌ | ✅ | What is the order count by year? |
| q30 | time | ✅ | ❌ | What is the order count for shipped orders only? |

### Naive→Semantic before/after

**q01 — What is our total revenue?**

Naive SQL:
```sql
SELECT SUM(total) AS total_revenue FROM crm.orders WHERE status = 'completed' LIMIT 200
```
Semantic plan: `{"metrics": ["gross_revenue"], "dimensions": [], "filters": [], "time_grain": null}`

Semantic SQL:
```sql
SELECT SUM(crm.orders.total) FILTER (WHERE crm.orders.status <> 'cancelled') AS gross_revenue
FROM crm.orders
LIMIT 200
```

**q02 — What is our net revenue?**

Naive SQL:
```sql
SELECT SUM(total) - SUM(discount) AS net_revenue FROM crm.orders WHERE status = 'completed' LIMIT 200
```
Semantic plan: `{"metrics": ["net_revenue"], "dimensions": [], "filters": [], "time_grain": null}`

Semantic SQL:
```sql
SELECT (SELECT COALESCE(SUM(o.total), 0) FROM crm.orders o WHERE o.status <> 'cancelled') - (SELECT COALESCE(SUM(r.amount), 0) FROM crm.refunds r) AS net_revenue
```

**q03 — What is our gross revenue?**

Naive SQL:
```sql
SELECT SUM(total) AS gross_revenue FROM crm.orders WHERE status = 'completed' LIMIT 1
```
Semantic plan: `{"metrics": ["gross_revenue"], "dimensions": [], "filters": [], "time_grain": null}`

Semantic SQL:
```sql
SELECT SUM(crm.orders.total) FILTER (WHERE crm.orders.status <> 'cancelled') AS gross_revenue
FROM crm.orders
LIMIT 200
```
<!-- END EVAL_SUMMARY -->

## 5. What this system can't do

The architecture is honest about its scope. Three categories of question fall outside it:

**Free-form row inspection.** Questions like "show me the orders from Tuesday" or "list the top five customers by name" ask for raw rows, not metrics. The semantic layer has no notion of "top by name" because there's no metric for "customer name" — names are identifiers, not aggregates. The planner returns `no_match` with `suggested_fallback: "file_search"` so the agent either falls back to document search or tells the user the question is out of scope.

**Novel metrics not in the layer.** "What's our customer health score" or "compute LTV using the last 12 months of activity" require metric definitions the YAML doesn't have. The planner refuses to invent metrics — the cross-check in `_validate_plan_against_layer` rejects any matched plan that references a metric outside the layer. Adding the metric is a YAML edit + a startup-time live-DB validation, not a prompt-engineering exercise.

**Multi-step reasoning.** "Compare this quarter's revenue to last quarter's, and tell me which segment grew fastest" is two SELECTs and a comparison. The current planner emits one PlanSpec per turn; multi-step decomposition would need either a planner that returns a list of plans or a sub-agent that runs the planner multiple times. Neither is implemented. The agent currently handles this by either picking one of the two questions or returning a single-quarter answer with a note.

**Free-form aggregation grain mismatches.** The schema doesn't include order-line-level revenue as a metric — `gross_revenue` is at order grain, `gross_margin` is at order_item grain. Asking "revenue by product category" wants order-grain revenue allocated across line items, which has no canonical definition in the layer. The planner returns `no_match`; the right fix is a new metric definition.

The pattern across all four categories: the limitation is in the *semantic layer*, not the architecture. Adding a metric, a synonym, or a join is a hand-edit with live-DB validation. That's the explicit trade-off — the system is correct on its declared surface and refuses outside it, rather than generating plausible-looking SQL whose correctness no one can verify.
