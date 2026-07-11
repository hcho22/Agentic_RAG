# Demo corpora: three worked examples, not three interchangeable defaults

The kit ships three demo corpora.
They are **role-specific worked examples** - each one exists to demonstrate a different capability - not a menu of drop-in defaults you pick between.
Read this before you decide how to model your own data: the right example to imitate depends on which face of the kit you are shipping.

The honest headline first, because it governs everything below:

> **Swapping in your own corpus makes the example golden set's anchors fail loud.**
> The shipped golden set is a **format template to learn from, not a survives-the-swap artifact**.
> "Replace the corpus" and "author a new golden set" are the **same step**, not two.

This is a direct, designed consequence of content anchoring (US-107): a gold label is a verbatim span quoted from the shipped corpus, so pointing the eval at your docs makes those spans resolve to nothing and raises a hard `ZeroResolveError` (see `docs/golden-set-authoring.md` §2).
The guide below never implies the example questions will work on your documents.
They will not, on purpose.

---

## The three corpora at a glance

| Corpus | Role | Retrieval-answerable? | Seeder | Powers |
|---|---|---|---|---|
| **E-commerce** (default) | Permissions + escalation, small / fast / relatable | **Yes** - the golden set anchors to it | `db_seed/corpus_seed.py` | The default `seed → eval` green run (US-111), E4 leak matrix, E7 escalation |
| **Wikipedia 10k** | Scale benchmark - **filler only** | **No** - never golden-answerable | `db_seed/wikipedia_seed.py` | The permissions scale benchmark (recall@5 vs HNSW `ef_search`) |
| **CRM** | Text-to-SQL optional module (X1) | N/A - structured data, not vector retrieval | `db_seed/crm_seed.py` | The semantic-layer query planner (`docs/structured-rag.md`) |

Each corpus is owned by its **own sentinel user** so all three coexist in one database without colliding - the retrieval eval, the scale eval, and the structured-data agent each pin their own principal.

---

## 1. E-commerce (default) - permissions + escalation

**Role:** the small, fast, relatable corpus that makes the security and escalation claims legible.

Eight markdown documents / sixteen chunks in `db_seed/corpus/` (refund policy, returns process, shipping FAQ, warranty terms, loyalty program, product catalog, customer-service SOP, API & integration error reference).
Small enough to read end to end, relatable enough that a buyer immediately understands "this viewer should not see the enterprise refund exception."

This is the corpus the kit **ships green** against (US-111): a clean `seed → eval` reproduces the `1.000` no-leak security table on the first run with no buyer authoring, and the kit's own CI keeps it green.
Its golden set (`evals/retrieval/retrieval_gold.yaml` + `evals/retrieval/escalation_gold.yaml`) is authored in the content-anchor format and is the **worked example you learn the format from**.

Imitate this corpus when your face is a **permission-aware knowledge assistant or the support widget**: it demonstrates the E4 leak matrix (derived for free from the gold labels) and, for support buyers, the E7 escalation populations (`no_context` / `answerable_faithful` / `should_escalate`).

**When you swap it out:** every anchor in the shipped golden set was quoted from these eight documents, so the first eval against your corpus fails loud on those spans.
That failure is your prompt to author a new golden set on your own docs - see `docs/golden-set-authoring.md` (US-109) and the layered format (US-108).

---

## 2. Wikipedia 10k - scale benchmark, **filler only, never gold**

**Role:** retrieval *noise* at volume, to chart recall under permission selectivity - and nothing else.

`db_seed/wikipedia_seed.py` seeds a deterministic 10,000-chunk slab from `Salesforce/wikitext` (pinned by dataset revision) and powers the permissions scale benchmark in `evals/permissions_scale/`, which charts pre-filter `recall@5` against HNSW `ef_search` at three permission selectivities (50% / 10% / 1%).

The critical property, and the reason it has its own section: **Wikipedia is filler only and is never golden-answerable.**
No golden question is answered *from* a Wikipedia chunk.
The scale benchmark's "gold" is a deterministic hash-derived visible-chunks set per viewer (`evals/permissions_scale/scale_gold.yaml`), used to measure whether the right chunks survive the ACL pre-filter at scale - it is **not** a content-anchor answerability set like the e-commerce golden set.
When you combine Wikipedia with an answerable corpus, the **golden questions stay anchored to the real (e-commerce or your own) documents**; Wikipedia only supplies the surrounding volume that makes recall@k a meaningful measurement.

Imitate this corpus when you want to **benchmark retrieval at your production scale**: pour in high-volume filler to stress the pre-filter, but keep every *answerable* golden question anchored to your real documents, never to the filler.
Treating filler as gold would be exactly the under-labeling failure the completeness contract warns about (`docs/golden-set-authoring.md` §3).

---

## 3. CRM - the text-to-SQL optional module (X1)

**Role:** the worked example for the **optional structured-data module**, not the vector-retrieval path.

`db_seed/crm_seed.py` seeds a five-table `crm` schema (~200 customers, ~50 products, ~1000 orders, ~3000 order items, ~100 refunds) with a fixed RNG seed so the rows are byte-identical across runs.
It powers the semantic-layer query planner documented in `docs/structured-rag.md`: a Cube-style semantic layer plus a two-step planner that takes the *semantic choice* (what "revenue" means) out of SQL generation.

This corpus is a different shape from the other two: it is **structured relational data**, not a document corpus, and it exercises the text-to-SQL agent rather than the vector/hybrid retrieval evals.
There are no content anchors here - the eval axis is "did the planner pick the right measure/definition," not "did retrieval surface the right chunk."

Imitate this corpus only if you are shipping the **optional text-to-SQL module (X1)** over your own structured data; it is orthogonal to the permission-aware document retrieval the other two demonstrate.

---

## The swap is one step, not two

Because the demo corpora and their golden sets are joined at the anchor, there is no such thing as "replace the corpus but keep the questions":

1. **The seeder seeds a corpus and nothing eval-specific (US-110).**
   Point `db_seed/generic_seed.py` at a docs folder (plus an *optional* manifest of real workspaces / principals / grants) and it runs the production `chunk_text` + `embed_texts` paths and inserts `documents` + `chunks` (+ your real `chunk_acl` rows).
   It **never** bakes in the synthetic eval viewers or the derived ACL matrix - those are constructed transiently by the runner at eval time - so a production seed carries zero test principals.

2. **Author a new golden set on the swapped corpus (US-108 / US-109).**
   The example anchors fail loud against your docs (by design); you replace them with content anchors quoted from *your* chunks, following the layered format (base labels → the E4 matrix and E7 P1b derive for free → one escalation label per question only if you ship support).
   `docs/golden-set-authoring.md` is the step-by-step.

Do steps 1 and 2 together, or the first eval fails loud and tells you to do step 2 anyway.
That fail-loud coupling is the feature: it is impossible to ship a green run over a corpus whose golden set was written for a different corpus.

---

## Cross-references

- `docs/golden-set-authoring.md` - authoring a golden set on your own corpus; content anchoring, the completeness contract, single-family-weaker caveat (US-107, US-108, US-109).
- `docs/evals.md` §2.2 - the golden-set format reference; the permission/leak matrix and escalation populations.
- `docs/structured-rag.md` - the CRM / text-to-SQL semantic-layer planner (X1).
- `db_seed/manifest.example.yaml` - the optional real-grant manifest for the generic seeder (US-110).
- `db_seed/corpus_seed.py` / `db_seed/wikipedia_seed.py` / `db_seed/crm_seed.py` - the three demo seeders.
