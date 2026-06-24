# Agentic RAG

A production-shaped Retrieval-Augmented Generation app where **per-document
sharing is a first-class part of the retrieval predicate, not a post-hoc
filter**. Multi-user from day one — every chunk carries an ACL, every
retrieval call runs under the viewer's JWT, every tool-call attribution
in the chat UI surfaces *why* the viewer can see a chunk.

Raw OpenAI SDK + Pydantic (no LLM frameworks), FastAPI backend,
React/Vite/Tailwind frontend, Supabase (Postgres + pgvector + Auth +
Storage + Realtime), LangSmith observability.

![Granting-principal badges in the chat UI](docs/img/granting-principal-badges.png)

*Tool-call attribution renders a per-chunk badge — "via owner" / "via direct
grant" / "via {group}" — so the viewer can see exactly which ACL rule
granted them access to each retrieved chunk.*

## The permissions story, in numbers

The retrieval path is evaluated in two cuts: a correctness eval that
proves the security property holds at small scale, and a scale benchmark
that characterises the recall curve as the visible set shrinks.

**Security — fraction of `no_access` runs that returned zero gold chunks**
(50 questions × 3 modes × 3 viewer setups, 14-chunk Acme corpus):

| Mode | Pre-filter | Post-filter |
|---|---|---|
| vector | **1.000** | 1.000 |
| keyword | **1.000** | 1.000 |
| hybrid | **1.000** | 1.000 |

Pre-filter is the load-bearing row — security is enforced in the SQL
predicate, not a Python drop after the fact (post-filter passes too but
could in principle leak via timing or payload size).

**Recall@5 across viewers, ef_search × selectivity sweep**
(15 multi-hop queries against a synthetic Wikipedia 10k-chunk corpus,
gold = top-5 at the most exhaustive sweep):

| Viewer | Visible chunks | Selectivity | ef_search=40 | ef_search=80 | ef_search=200 | ef_search=500 (gold) |
|---|---|---|---|---|---|---|
| viewer_50pct | 5,000 | 50.0% | 1.000 | 1.000 | 1.000 | 1.000 |
| viewer_10pct | 1,000 | 10.0% | 1.000 | 1.000 | 1.000 | 1.000 |
| viewer_1pct | 100 | 1.0% | 1.000 | 1.000 | 1.000 | 1.000 |

Every cell is 1.000 because at 10k chunks the Postgres planner sidesteps
HNSW entirely — it bitmap-scans `chunk_acl`, index-scans the visible
chunks, sorts exactly by embedding distance, and takes top-5. `EXPLAIN
ANALYZE` confirms; `ef_search` is a no-op in that plan. The eval
infrastructure (10k seed, viewer ACL setup, sweep, regression alarm) is
shipped; the recall curve surfaces at the corpus size where exact NN
over the filtered set becomes more expensive than HNSW + post-filter
(tens to hundreds of thousands of visible chunks per query). The
nightly workflow fails loudly if the configured recall floor is
breached. See [`docs/permissions-aware-rag.md`](docs/permissions-aware-rag.md)
§5b for the full plan output.

## Why this is hard

The naive approach to per-document sharing in a RAG retriever is to
leave the vector search alone and **post-filter** the results: pull
top-k chunks by similarity, then drop the ones the viewer can't see.
This fails on selective ACLs in a way that's easy to miss. The math:
if a viewer can see 5% of the corpus and we ask for top-10, the
*expected* number of visible chunks in that result is
`k × selectivity = 10 × 0.05 = 0.5` — half a chunk on average. The
viewer most often sees zero relevant chunks; multi-hop questions that
need two chunks become unanswerable. "Fetch more candidates and
post-filter harder" doesn't rescue it — at 5% selectivity you'd need
top-100 to expect five visible chunks, and post-filtering top-100
means embedding distance is no longer ranking the *visible* chunks
against each other. The fix is to push the ACL check **into** the SQL
predicate so the planner is choosing among visible candidates from the
start — which then opens a second gotcha around HNSW behaviour under
selective filters. The full write-up is in
[`docs/permissions-aware-rag.md`](docs/permissions-aware-rag.md).

## What else is in the box

- **Chat with streaming** — OpenAI Responses or Chat Completions API, configurable per-request, streamed token-by-token to the UI. Tool calls and results persist alongside messages.
- **Drag-and-drop ingestion** — `.txt / .md / .pdf / .docx / .html` parsed via docling, chunked, embedded, indexed. Live status updates via Supabase Realtime. Document-level metadata (title, authors, topics, dates) extracted via LLM structured outputs.
- **Hybrid retrieval** — vector (pgvector HNSW) + keyword (Postgres full-text) fused via Reciprocal Rank Fusion. Optional reranker layer: Cohere, Voyage, or LLM-as-judge. All retrieval runs under user JWT — RLS enforces per-user visibility.
- **Per-document sharing** — share documents with individual users or groups via the per-chunk ACL system. Share dialog in the ingestion UI. Per-chunk badges in chat tool attribution show *why* the viewer can see each chunk.
- **Workspace tenant isolation** — a hard tenant boundary *above* per-document sharing: a chunk is visible only if the viewer is a member of its document's workspace, AND-ed into the same `SECURITY INVOKER` retrieval predicate (resolved from the viewer's JWT, never a backend-passed tenant id) and mirrored in the table RLS. Existing data lives in one operator-managed Default Workspace; the boundary bites once a second workspace exists. See [`docs/adr/0002-workspace-tenant-isolation.md`](docs/adr/0002-workspace-tenant-isolation.md).
- **Structured RAG (text-to-SQL)** — `query_database` tool over an allowlisted read-only schema, with a semantic-layer-aware compiler so the LLM doesn't have to know table internals.
- **Web search fallback** — `web_search` tool when local retrieval is insufficient.
- **Sub-agents** — `spawn_document_agent` launches a sub-agent with isolated context and purpose-specific tools.
- **Retrieval eval suite** — 50-question golden set, runner that exercises vector / keyword / hybrid against the real backend functions, recall@k / MRR / nDCG@5 metrics, optional generation + LLM-judge step. PR CI posts a delta-vs-`main` comment; nightly publishes snapshots to `docs/nightly/`.
- **RAGAS metrics** — the four canonical RAG-eval scores (Faithfulness, Answer Relevancy, Context Precision, Context Recall) computed weekly alongside the custom Claude judge and published to `docs/ragas-weekly/`.
- **Permissions scale benchmark** — Wikipedia 10k synthetic corpus, ef_search sweep across three permission selectivities, nightly workflow with regression alarm.

## Documentation

Long-form writeups for the parts of the system that benefit from prose
explanation — the kind of context a code review won't recover:

| Doc | What it covers |
| --- | --- |
| [`docs/permissions-aware-rag.md`](docs/permissions-aware-rag.md) | The post-filter recall problem, the four-table data model, the SQL predicate, the HNSW interaction, the eval tables, deliberate v0 scope cuts (group nesting, write-vs-read tiers). |
| [`docs/adr/0002-workspace-tenant-isolation.md`](docs/adr/0002-workspace-tenant-isolation.md) | Phase 2 — the Workspace tenant boundary layered above owner-OR-ACL: where the boundary is enforced (membership clause inside the retrieval predicate, never a backend-passed tenant id), how existing data migrates into a Default Workspace, the alternatives rejected, and the **Identity Boundary** (AU3) — what an integrator may swap in the auth stack (federation-edge only) versus the welded Supabase-JWT pass-through floor. |
| [`docs/evals.md`](docs/evals.md) | Corpus design, the 50-question golden set, what each metric measures and what it *doesn't*, a worked example of CI catching a regression (Δ -0.510 on `recall@5` from a one-line chunk-size change), a frank list of the eval's limitations, and the **E7 escalation eval** (§6) - the deflection-pipeline golden set, why its deterministic legs gate per-PR while the LLM-judged legs run weekly, and the false-resolve ceiling as a pinned safety invariant. |
| [`docs/structured-rag.md`](docs/structured-rag.md) | The semantic-layer-aware text-to-SQL compiler, allowlisted schemas, the read-only role boundary. |
| [`docs/ingestion-parser-adapters.md`](docs/ingestion-parser-adapters.md) | Write your own `DocumentParser` — the load-bearing markdown-string contract, the edits to add one (subclass + `PARSER` validation + `build_parser`), `PARSER` selection, proving the round-trip, and Unstructured.io as the canonical buyer-written adapter. |

The eval tables in `docs/permissions-aware-rag.md` are auto-embedded
from the runner-generated `summary.md` files via marker comments:

```bash
python -m evals.retrieval.runner          # populates evals/retrieval/summary.md
python -m evals.permissions_scale.runner  # populates evals/permissions_scale/summary.md (after wikipedia_seed)
python -m docs._embed_eval_summaries      # injects into docs/permissions-aware-rag.md
```

## Repository layout

```
backend/                FastAPI service (Dockerfile, railway.toml, fly.toml)
frontend/               React + Vite + Tailwind (vercel.json)
supabase/               Migrations + local CLI config
evals/retrieval/        50-question golden set + E7 escalation golden set + runners + CI workflow integration
evals/permissions_scale/ Wikipedia 10k corpus benchmark + nightly workflow
evals/structured_rag/   Text-to-SQL eval
db_seed/                Deterministic seeders for the eval corpora
docs/                   Long-form writeups (evals, structured RAG, permissions-aware RAG)
.github/workflows/      PR + nightly eval workflows
.claude/                Agent task specs (not needed to run the app)
```

## Local development

Prerequisites: **Node 20+**, **Python 3.11+**, **Docker Desktop** (for local Supabase), Supabase CLI, OpenAI API key.

```bash
# 1. Start the local Supabase stack (Postgres + pgvector + GoTrue + Storage + Studio)
#    Brings up Docker containers and applies all migrations in supabase/migrations/.
supabase start
supabase status                # note API_URL, SERVICE_ROLE_KEY, DB_URL for env files

# 2. Backend
cd backend
cp .env.example .env           # fill in the values below
pip install -r requirements.txt
uvicorn main:app --reload --port 8000

# 3. Frontend
cd ../frontend
cp .env.example .env           # fill in VITE_SUPABASE_* + VITE_BACKEND_URL
npm install
npm run dev                    # http://localhost:5173
```

To run against hosted Supabase instead of local, push migrations with `supabase db push --linked` and point `SUPABASE_URL` / `VITE_SUPABASE_URL` at the hosted project URL — no other code changes.

## Environment variables

### Backend (`backend/.env`)

| Var | Required | Notes |
| --- | --- | --- |
| `SUPABASE_URL` | yes | `https://<project>.supabase.co` (hosted) or `http://127.0.0.1:54321` (local) |
| `SUPABASE_ANON_KEY` | yes | Used to call GoTrue for JWT validation |
| `SUPABASE_SERVICE_ROLE_KEY` | yes | Reserved for system-level ops (share API owner-lookup, ingestion); never used to touch user data on the retrieval path (RLS enforced via user JWT) |
| `SUPABASE_JWT_SECRET` | only for support bot | The project JWT secret GoTrue signs with. The support bot self-signs its short-lived bot token with it so `auth.uid()`/RLS resolve it natively (US-068, `backend/supabase_jwt.py`); a knowledge-assistant-only deploy leaves it blank. NEW signing surface - keep server-side only, never embed client-side |
| `OPENAI_API_KEY` | yes | |
| `OPENAI_MODEL` | no | Default `gpt-4o-mini` |
| `OPENAI_VECTOR_STORE_ID` | no | Enables `file_search` retrieval when set |
| `PARSER` | no | Ingestion parser: `docling` (default) / `llamaparse` / `unstructured`. Invalid value fails fast at startup. To add your own, see [docs/ingestion-parser-adapters.md](docs/ingestion-parser-adapters.md) |
| `LLAMA_CLOUD_API_KEY` | only if `PARSER=llamaparse` | LlamaParse cloud key; checked at startup, not first ingest |
| `FRONTEND_ORIGIN` | yes (prod) | Comma-separated list of allowed CORS origins. Defaults to `http://localhost:5173` for dev |
| `CHAT_MODE_DEFAULT` | no | `responses` or `completions`. Defaults to `responses` on an `openai` answerer, `completions` on any other provider. `responses` is OpenAI-only and fails closed at startup on a non-`openai` answerer — see [docs/model-surface.md](docs/model-surface.md) |
| `CHAT_HISTORY_MAX_TURNS` | no | Default 10 |
| `RETRIEVAL_MODE` | no | `hybrid` (default) / `vector` / `keyword`. Safety escape hatch — production uses hybrid |
| `SEARCH_SIMILARITY_THRESHOLD` | no | Cosine threshold for `match_chunks` filter. Default 0.3 |
| `HYBRID_RRF_K` | no | RRF damping constant. Default 60 |
| `RERANKER` | no | `none` (default) / `cohere` / `voyage` / `llm` |
| `COHERE_API_KEY` | only if `RERANKER=cohere` | |
| `VOYAGE_API_KEY` | only if `RERANKER=voyage` | |
| `RERANK_INPUT_K` | no | Pool size fed into the reranker. Default 20 |
| `LANGSMITH_API_KEY` | no | When set, traces ship to LangSmith |
| `LANGSMITH_PROJECT` | no | Default `agentic-rag` |
| `LANGSMITH_TRACING` | no | `true`/`false`; auto-set based on API key presence |
| `PORT` | no | Injected by Railway/Fly at runtime |
| `ANALYTICS_DATABASE_URL` | no | Postgres URL for the `analytics_readonly` role used by the text-to-SQL baseline |
| `CRM_DATABASE_URL` | no | Postgres URL for the `crm_readonly` role used by the semantic-layer-aware SQL search. Falls back to `ANALYTICS_DATABASE_URL` |
| `CRM_SEED_DATABASE_URL` | no | Writable Postgres URL used only by `python -m db_seed.crm_seed`. Falls back to `DATABASE_URL` |
| `ALLOWED_SQL_SCHEMAS` | no | Comma-separated schema allowlist for SQL tools. Default `analytics,crm` |
| `SQL_QUERY_TIMEOUT_MS` | no | Statement timeout for SQL tools. Default 10000 |
| `ANTHROPIC_API_KEY` | only for eval generation | Required by `evals/retrieval/runner.py --include-generation` (the LLM judge runs Claude). Never read by the live backend |

#### Model surface (provider / model selection)

Bring your own model host. Provider binds **per role** (answerer / embedder /
judge); model binds **per call-site**. Two targets are tested — `openai` and
`azure` — and `openai` accepts a `base_url` for any OpenAI-compatible endpoint.
The embedder/judge inherit the answerer config unless overridden, so a
single-provider deploy sets only the answerer (bare) vars. **Full reference,
role-fallback precedence, worked Azure example, capability matrix, and the
embedder re-index procedure: [docs/model-surface.md](docs/model-surface.md).**

| Var | Required | Notes |
| --- | --- | --- |
| `LLM_PROVIDER` | no | Answerer provider: `openai` (default) or `azure` |
| `OPENAI_BASE_URL` | no | Any OpenAI-compatible endpoint (supported-but-untested) |
| `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_API_VERSION` / `AZURE_OPENAI_API_KEY` | only if `provider=azure` | All three required — `provider=azure` fails closed at startup if any is missing |
| `AZURE_OPENAI_DEPLOYMENT` | no | Azure deployment **name** (≠ model id); unset → per-call model id is the deployment |
| `EMBEDDER_PROVIDER` / `EMBEDDER_API_KEY` / `EMBEDDER_BASE_URL` / `EMBEDDER_AZURE_OPENAI_*` | no | Embedder-role overrides; fall back to the answerer config (deployment is per-role, not inherited) |
| `JUDGE_PROVIDER` / `JUDGE_API_KEY` / `JUDGE_BASE_URL` / `JUDGE_AZURE_OPENAI_*` | no | Runtime-judge-role overrides; same fallback rules as the embedder |
| `EMBEDDER_MODEL` | no | Embedder model. Falls back to `EMBEDDING_MODEL` → `text-embedding-3-small` |
| `METADATA_MODEL` / `OPENAI_PLANNER_MODEL` / `OPENAI_SQL_MODEL` / `OPENAI_SUBAGENT_MODEL` / `OPENAI_RERANK_MODEL` | no | Per-call-site model selectors within the answerer provider; each falls back to `OPENAI_MODEL` |

### Frontend (`frontend/.env`)

| Var | Required | Notes |
| --- | --- | --- |
| `VITE_SUPABASE_URL` | yes | Same as backend `SUPABASE_URL` |
| `VITE_SUPABASE_ANON_KEY` | yes | Same as backend `SUPABASE_ANON_KEY` |
| `VITE_BACKEND_URL` | yes | Backend origin — `http://localhost:8000` for dev, your Railway/Fly URL in prod |

## API surface

The backend exposes:

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/chat` | Streaming chat, tool-using agent loop |
| `GET` | `/api/config` | Frontend bootstrap (chat mode default, etc.) |
| `POST` | `/api/documents/{id}/ingest` | Trigger / re-trigger ingestion for an uploaded document |
| `POST` | `/api/documents/{id}/share` | Grant a user or group access to a document |
| `GET` | `/api/documents/{id}/shares` | List existing grants (owner-only) |
| `DELETE` | `/api/documents/{id}/shares/{principal_id}` | Revoke a grant |
| `POST` | `/api/search` `/api/search/keyword` `/api/search/hybrid` `/api/search/rerank` | Direct retrieval probes (debugging / eval) |
| `POST` | `/api/sql` | Text-to-SQL via the semantic-layer compiler |
| `POST` | `/api/web-search` | Web fallback |
| `POST` | `/api/subagent` | Spawn a document sub-agent |
| `GET` | `/healthz` | Liveness check |

## Eval suite

The CI workflows wrap the eval runners:

- **`.github/workflows/retrieval-eval.yml`** — runs on PRs that touch retrieval / chunking / embeddings / escalation / migrations / the runner itself. Executes the 50-question golden set against PR head AND `main`, posts a delta-vs-`main` comment. The delta comment is advisory — it never fails the build. The PR run additionally executes two **hard gates**: the **E6 second-workspace zero-leak eval** (`--include-e6`) — a detected cross-workspace leak (or a structurally blind positive control) fails the build — and the **E7 escalation tripwire** (`e7_runner --include-p1b`, US-059): the *deterministic* deflection legs (P1a/P1b retrieval-gate decisions + the P1b non-disclosure byte-equality assertion, no LLM), where a P1a/P1b gate clear or a non-disclosure mismatch fails the build. Both are deterministic, so a real verdict can't flake; a transient E6 execution error is surfaced loudly but stays non-blocking.
- **`.github/workflows/escalation-eval-weekly.yml`** — Sundays 06:00 UTC + manual `workflow_dispatch`. Runs the **full** E7 deflection sweep including the LLM-judged P2/P3 legs + the knob sweep; publishes to `docs/escalation-weekly/<DATE>.md` + `.json`. A measured false-resolve rate above the buyer's ceiling (the pinned safety number) fails the *scheduled* workflow and files an issue — it never blocks a merge (a judge wobble must not red-bar a PR; US-059).
- **`.github/workflows/retrieval-eval-ragas-weekly.yml`** — Sundays 04:00 UTC + manual `workflow_dispatch`. Scores the four canonical RAGAS metrics weekly; publishes to `docs/ragas-weekly/<DATE>.md`; files an issue on a red gate finding.
- **`.github/workflows/retrieval-eval-nightly.yml`** — daily 02:00 UTC. Publishes snapshots to `docs/nightly/<DATE>.md` + `.json`.
- **`.github/workflows/permissions-scale-eval.yml`** — daily 03:00 UTC + manual `workflow_dispatch`. Runs the Wikipedia 10k seed + ef_search sweep; publishes to `docs/permissions-scale-nightly/<DATE>.md`. **Fails the workflow if the configured recall floor is breached** — this is the regression alarm for the day the planner flips to HNSW for some workload.

To run the eval locally:

```bash
# One-time corpus seed
export CORPUS_SEED_DATABASE_URL=postgresql://postgres:postgres@localhost:54322/postgres
export SUPABASE_URL=http://127.0.0.1:54321
export SUPABASE_SERVICE_ROLE_KEY=<from `supabase status`>
export OPENAI_API_KEY=sk-...
python -m db_seed.corpus_seed

# Eval runs
python -m evals.retrieval.runner                      # all three modes
python -m evals.retrieval.runner --mode vector        # single mode (faster)
python -m evals.retrieval.runner --include-generation # adds LLM-judge faithfulness/helpfulness (needs ANTHROPIC_API_KEY)
python -m evals.retrieval.runner --include-e6         # adds the E6 second-workspace zero-leak gate (exits 1 on a cross-workspace leak)
python -m evals.retrieval.e7_runner --include-p1b     # E7 escalation tripwire - the deterministic per-PR gate (P1a/P1b retrieval gate + non-disclosure byte-equality, no LLM; exits 1 on a gate clear or non-disclosure mismatch). The P1b leg also needs DATABASE_URL set. Add --include-p2 --include-p3 --sweep for the weekly LLM-judged legs (needs ANTHROPIC_API_KEY)
```

## Deploy

The app deploys to **Vercel** (frontend) + **Railway or Fly** (backend) + **Supabase** (DB/Auth/Storage). No code changes required — only env vars.

### 1. Supabase

1. Create a project at [supabase.com](https://supabase.com).
2. Link and push the schema:
   ```bash
   cd supabase
   supabase link --project-ref <your-ref>
   supabase db push
   ```
3. Enable Google and GitHub OAuth providers in *Authentication → Providers*.
4. Grab `SUPABASE_URL`, `anon` key, and `service_role` key from *Settings → API*.

### 2. Backend — Railway (recommended)

1. Push the repo to GitHub.
2. Create a Railway project → *New Service* → *Deploy from GitHub repo*.
3. Set *Service Root Directory* to `backend/`. Railway picks up `backend/Dockerfile` and `backend/railway.toml` automatically.
4. Under *Variables*, set: `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_VECTOR_STORE_ID`, `FRONTEND_ORIGIN`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT`. Add `RERANKER` + the matching API key if you want a reranker on by default.
5. Deploy. Note the generated `*.up.railway.app` URL — that's your `VITE_BACKEND_URL`.
6. Hit `/healthz` to confirm the service is up.

### 2b. Backend — Fly.io (alternative)

```bash
cd backend
fly launch --copy-config --no-deploy        # picks up fly.toml + Dockerfile
fly secrets set \
  SUPABASE_URL=... SUPABASE_ANON_KEY=... SUPABASE_SERVICE_ROLE_KEY=... \
  OPENAI_API_KEY=... OPENAI_VECTOR_STORE_ID=... \
  FRONTEND_ORIGIN=https://<your-vercel-url> \
  LANGSMITH_API_KEY=...
fly deploy
```

### 3. Frontend — Vercel

1. *Add New Project* → import the GitHub repo.
2. Set *Root Directory* to `frontend/`. Vercel picks up `frontend/vercel.json` (Vite preset, SPA rewrites).
3. Set env vars: `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`, `VITE_BACKEND_URL` (← your Railway/Fly URL).
4. Deploy. Copy the production URL back into the backend's `FRONTEND_ORIGIN` and redeploy the backend so CORS allows it.

### 4. Verify

Open the Vercel URL, sign up, create a thread, send a message. The response should stream token-by-token, and a trace should appear in LangSmith tagged with your `user_id` and `thread_id`. Upload a document at `/ingestion`, watch it transition `pending → processing → ready`, then ask the chat about its contents.

## How it was built

The system landed in 11 progressive modules; the full plan + per-story
acceptance criteria live in `.claude/agent/tasks/prd-agentic-rag.md`.

| Module | What landed |
| --- | --- |
| 1 | App shell, auth, threads, streaming chat, LangSmith |
| 2 | BYO retrieval (vector via match_chunks RPC), per-thread memory |
| 3 | Content-hashing dedup on documents and chunks |
| 4 | LLM structured-output metadata extraction at ingestion |
| 5 | Multi-format ingestion (txt/md/pdf/docx/html via docling) |
| 6 | Hybrid retrieval (RRF) + reranker layer (cohere / voyage / llm) |
| 7 | Additional tools — `query_database`, `web_search` |
| 8 | Sub-agents — `spawn_document_agent` |
| 9 | Structured RAG with semantic-layer-aware text-to-SQL |
| 10 | Retrieval eval suite (golden set, metrics, PR CI delta, nightly) |
| 11 | Permission-aware retrieval (per-chunk ACLs, share dialog, granting-principal badges) |
