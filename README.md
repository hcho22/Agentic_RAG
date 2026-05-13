# Agentic RAG

A multi-user, production-oriented Retrieval-Augmented Generation app built in 8 progressive modules. Raw OpenAI SDK + Pydantic (no LLM frameworks), FastAPI backend, React/Vite/Tailwind frontend, Supabase (Postgres + pgvector + Auth + Storage + Realtime), LangSmith observability.

## Repository layout

```
backend/      FastAPI service (Dockerfile, railway.toml, fly.toml)
frontend/     React + Vite + Tailwind (vercel.json)
supabase/     Migrations + local CLI config
.claude/      Agent task specs (not needed to run the app)
```

## Local development

Prerequisites: **Node 20+**, **Python 3.11+**, Supabase project, OpenAI API key.

```bash
# 1. Supabase schema
cd supabase && supabase db push

# 2. Backend
cd backend
cp .env.example .env   # fill in the values below
pip install -r requirements.txt
uvicorn main:app --reload --port 8000

# 3. Frontend
cd frontend
cp .env.example .env   # fill in VITE_SUPABASE_* + VITE_BACKEND_URL
npm install
npm run dev            # http://localhost:5173
```

## Environment variables

### Backend (`backend/.env`)

| Var | Required | Notes |
| --- | --- | --- |
| `SUPABASE_URL` | yes | `https://<project>.supabase.co` |
| `SUPABASE_ANON_KEY` | yes | Used to call GoTrue for JWT validation |
| `SUPABASE_SERVICE_ROLE_KEY` | yes | Reserved for system-level ops; never used to touch user data (RLS is enforced via user JWT) |
| `OPENAI_API_KEY` | yes | |
| `OPENAI_MODEL` | no | Default `gpt-4o-mini` |
| `OPENAI_VECTOR_STORE_ID` | no | Enables `file_search` retrieval when set |
| `FRONTEND_ORIGIN` | yes (prod) | Comma-separated list of allowed origins. In prod set to your Vercel URL(s); defaults to `http://localhost:5173` for dev |
| `LANGSMITH_API_KEY` | no | When set, traces ship to LangSmith |
| `LANGSMITH_PROJECT` | no | Default `agentic-rag` |
| `LANGSMITH_TRACING` | no | `true`/`false`; auto-set based on API key presence |
| `PORT` | no | Injected by Railway/Fly at runtime |
| `ANALYTICS_DATABASE_URL` | no (Module 7) | Postgres URL for the `analytics_readonly` role used by the text-to-SQL baseline |
| `CRM_DATABASE_URL` | no (Module 9) | Postgres URL for the `crm_readonly` role used by the semantic-layer-aware SQL search. Falls back to `ANALYTICS_DATABASE_URL` |
| `CRM_SEED_DATABASE_URL` | no (Module 9) | Writable Postgres URL used only by `python -m db_seed.crm_seed`. Falls back to `DATABASE_URL` |
| `ALLOWED_SQL_SCHEMAS` | no | Comma-separated schema allowlist for SQL tools. Default `analytics,crm` |
| `SQL_QUERY_TIMEOUT_MS` | no | Statement timeout for SQL tools. Default 10000 |

### Frontend (`frontend/.env`)

| Var | Required | Notes |
| --- | --- | --- |
| `VITE_SUPABASE_URL` | yes | Same as backend `SUPABASE_URL` |
| `VITE_SUPABASE_ANON_KEY` | yes | Same as backend `SUPABASE_ANON_KEY` |
| `VITE_BACKEND_URL` | yes | Backend origin — `http://localhost:8000` for dev, your Railway/Fly URL in prod |

## Deploy

The app deploys to **Vercel** (frontend) + **Railway or Fly** (backend) + **Supabase** (DB/Auth/Storage). No code changes are required — only env vars.

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
3. Set *Service Root Directory* to `backend/`. Railway will pick up `backend/Dockerfile` and `backend/railway.toml` automatically.
4. Under *Variables*, set: `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_VECTOR_STORE_ID`, `FRONTEND_ORIGIN`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT`.
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

Open the Vercel URL, sign up, create a thread, send a message. The response should stream token-by-token, and a trace should appear in LangSmith tagged with your `user_id` and `thread_id`.

## Modules

See `.claude/agent/tasks/prd-agentic-rag.md` for the full 8-module plan and per-story acceptance criteria.
