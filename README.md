# Agentic RAG

A multi-user, production-oriented Retrieval-Augmented Generation app built across 11 progressive modules. Raw OpenAI SDK + Pydantic (no LLM frameworks), FastAPI backend, React/Vite/Tailwind frontend, Supabase (Postgres + pgvector + Auth + Storage + Realtime), LangSmith observability.

## What's in the box

- **Chat with streaming** — OpenAI Responses or Chat Completions API, configurable per-request, streamed token-by-token to the UI. Tool calls and results persist alongside messages.
- **Drag-and-drop ingestion** — `.txt / .md / .pdf / .docx / .html` parsed via docling, chunked, embedded, indexed. Live status updates via Supabase Realtime. Document-level metadata (title, authors, topics, dates) extracted via LLM structured outputs.
- **Hybrid retrieval** — vector (pgvector HNSW) + keyword (Postgres full-text) fused via Reciprocal Rank Fusion. Optional reranker layer: Cohere, Voyage, or LLM-as-judge. All retrieval runs under user JWT — RLS enforces per-user visibility.
- **Per-document sharing** — share documents with individual users or groups via the per-chunk ACL system. Share dialog in the ingestion UI. Per-chunk badges in chat tool attribution show *why* the viewer can see each chunk ("via owner" / "via direct grant" / "via {group}").
- **Structured RAG (text-to-SQL)** — `query_database` tool over an allowlisted read-only schema, with a semantic-layer-aware compiler so the LLM doesn't have to know table internals.
- **Web search fallback** — `web_search` tool when local retrieval is insufficient.
- **Sub-agents** — `spawn_document_agent` launches a sub-agent with isolated context and purpose-specific tools.
- **Retrieval eval suite** — 50-question golden set, runner that exercises vector / keyword / hybrid against the real backend functions, recall@k / MRR / nDCG@5 metrics, optional generation + LLM-judge step. PR CI posts a delta-vs-`main` comment; nightly publishes snapshots to `docs/nightly/`.
- **Permissions scale benchmark** — Wikipedia 10k synthetic corpus, ef_search sweep across three permission selectivities, nightly workflow with regression alarm.

## Repository layout

```
backend/                FastAPI service (Dockerfile, railway.toml, fly.toml)
frontend/               React + Vite + Tailwind (vercel.json)
supabase/               Migrations + local CLI config
evals/retrieval/        50-question golden set + runner + CI workflow integration
evals/permissions_scale/ Wikipedia 10k corpus benchmark + nightly workflow
evals/structured_rag/   Text-to-SQL eval (Module 9)
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
| `OPENAI_API_KEY` | yes | |
| `OPENAI_MODEL` | no | Default `gpt-4o-mini` |
| `OPENAI_VECTOR_STORE_ID` | no | Enables `file_search` retrieval when set |
| `FRONTEND_ORIGIN` | yes (prod) | Comma-separated list of allowed CORS origins. Defaults to `http://localhost:5173` for dev |
| `CHAT_MODE_DEFAULT` | no | `responses` (default) or `chat_completions` |
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
| `ANALYTICS_DATABASE_URL` | no (Module 7) | Postgres URL for the `analytics_readonly` role used by the text-to-SQL baseline |
| `CRM_DATABASE_URL` | no (Module 9) | Postgres URL for the `crm_readonly` role used by the semantic-layer-aware SQL search. Falls back to `ANALYTICS_DATABASE_URL` |
| `CRM_SEED_DATABASE_URL` | no (Module 9) | Writable Postgres URL used only by `python -m db_seed.crm_seed`. Falls back to `DATABASE_URL` |
| `ALLOWED_SQL_SCHEMAS` | no | Comma-separated schema allowlist for SQL tools. Default `analytics,crm` |
| `SQL_QUERY_TIMEOUT_MS` | no | Statement timeout for SQL tools. Default 10000 |
| `ANTHROPIC_API_KEY` | only for eval generation | Required by `evals/retrieval/runner.py --include-generation` (the LLM judge runs Claude). Never read by the live backend |

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

## Documentation

Long-form writeups for the parts of the system that benefit from prose explanation:

| Doc | What it covers |
| --- | --- |
| [`docs/evals.md`](docs/evals.md) | Module 10: corpus, golden set, metrics, what they don't measure, a worked example of catching a regression in CI |
| [`docs/permissions-aware-rag.md`](docs/permissions-aware-rag.md) | Module 11: the post-filter recall problem, the data model, the SQL change, the HNSW interaction, the eval tables, and the deliberate v0 scope cuts |
| [`docs/structured-rag.md`](docs/structured-rag.md) | Module 9: the semantic layer, the SQL compiler, allowlisted schemas, the read-only role |

The eval tables in `docs/evals.md` and `docs/permissions-aware-rag.md` are auto-embedded from the runner-generated `summary.md` files via marker comments. To refresh after a runner change:

```bash
python -m evals.retrieval.runner          # populates evals/retrieval/summary.md
python -m evals.permissions_scale.runner  # populates evals/permissions_scale/summary.md (after wikipedia_seed)
python -m docs._embed_eval_summaries      # injects into docs/permissions-aware-rag.md
```

## Eval suite

Two CI workflows wrap the eval runners:

- **`.github/workflows/retrieval-eval.yml`** — runs on PRs that touch retrieval / chunking / embeddings / migrations / the runner itself. Executes the 50-question golden set against PR head AND `main`, posts a delta-vs-`main` comment. Comment-only — never fails the build.
- **`.github/workflows/retrieval-eval-nightly.yml`** — daily 02:00 UTC. Publishes snapshots to `docs/nightly/<DATE>.md` + `.json`.
- **`.github/workflows/permissions-scale-eval.yml`** — daily 03:00 UTC + manual `workflow_dispatch`. Runs the Wikipedia 10k seed + ef_search sweep; publishes to `docs/permissions-scale-nightly/<DATE>.md`. Fails loudly if the configured recall floor is breached.

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

## Modules

See `.claude/agent/tasks/prd-agentic-rag.md` for the full 11-module plan and per-story acceptance criteria.

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
