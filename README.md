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
(60 questions × 3 modes × 3 viewer setups, 16-chunk Acme corpus):

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
- **Hybrid retrieval** — vector (pgvector HNSW) + keyword (Postgres full-text) fused via Reciprocal Rank Fusion, with query-adaptive fusion weighting (`HYBRID_FUSION_ALPHA=auto`) that tilts identifier-dense queries toward the lexical leg and leaves prose at the legacy equal-weight midpoint. Optional reranker layer: Cohere, Voyage, or LLM-as-judge. All retrieval runs under user JWT — RLS enforces per-user visibility.
- **Per-document sharing** — share documents with individual users or groups via the per-chunk ACL system. Share dialog in the ingestion UI. Per-chunk badges in chat tool attribution show *why* the viewer can see each chunk.
- **Workspace tenant isolation** — a hard tenant boundary *above* per-document sharing: a chunk is visible only if the viewer is a member of its document's workspace, AND-ed into the same `SECURITY INVOKER` retrieval predicate (resolved from the viewer's JWT, never a backend-passed tenant id) and mirrored in the table RLS. Existing data lives in one operator-managed Default Workspace; the boundary bites once a second workspace exists. See [`docs/adr/0002-workspace-tenant-isolation.md`](docs/adr/0002-workspace-tenant-isolation.md).
- **Structured RAG (text-to-SQL)** — `query_database` tool over an allowlisted read-only schema, with a semantic-layer-aware compiler so the LLM doesn't have to know table internals.
- **Web search fallback** — `web_search` tool when local retrieval is insufficient.
- **Sub-agents** — `spawn_document_agent` launches a sub-agent with isolated context and purpose-specific tools.
- **Retrieval eval suite** — 60-question golden set, runner that exercises vector / keyword / hybrid against the real backend functions, recall@k / MRR / nDCG@5 metrics, optional generation + LLM-judge step. PR CI posts a delta-vs-`main` comment; nightly publishes snapshots to `docs/nightly/`.
- **RAGAS metrics** — the four canonical RAG-eval scores (Faithfulness, Answer Relevancy, Context Precision, Context Recall) computed weekly alongside the custom Claude judge and published to `docs/ragas-weekly/`.
- **Permissions scale benchmark** — Wikipedia 10k synthetic corpus, ef_search sweep across three permission selectivities, nightly workflow with regression alarm.

## Documentation

Long-form writeups for the parts of the system that benefit from prose
explanation — the kind of context a code review won't recover:

| Doc | What it covers |
| --- | --- |
| [`docs/permissions-aware-rag.md`](docs/permissions-aware-rag.md) | The post-filter recall problem, the four-table data model, the SQL predicate, the HNSW interaction, the eval tables, deliberate v0 scope cuts (group nesting, write-vs-read tiers). |
| [`docs/adr/0002-workspace-tenant-isolation.md`](docs/adr/0002-workspace-tenant-isolation.md) | Phase 2 — the Workspace tenant boundary layered above owner-OR-ACL: where the boundary is enforced (membership clause inside the retrieval predicate, never a backend-passed tenant id), how existing data migrates into a Default Workspace, the alternatives rejected, and the **Identity Boundary** (AU3) — what an integrator may swap in the auth stack (federation-edge only) versus the welded Supabase-JWT pass-through floor. |
| [`docs/evals.md`](docs/evals.md) | Corpus design, the 60-question golden set, what each metric measures and what it *doesn't*, a worked example of CI catching a regression (Δ -0.510 on `recall@5` from a one-line chunk-size change), a frank list of the eval's limitations, and the **E7 escalation eval** (§6) - the deflection-pipeline golden set, why its deterministic legs gate per-PR while the LLM-judged legs run weekly, and the false-resolve ceiling as a pinned safety invariant. |
| [`docs/golden-set-authoring.md`](docs/golden-set-authoring.md) | Author your own golden set on your own corpus - the **completeness contract** (why under-labeling gold manufactures a false security pass the green table hides, since `no_access = all_non_gold`), content anchoring (US-107), the E4 matrix / E7 P1b derived for free, the one support-face escalation label, and why a single-family eval is a **weaker proof** you must not cite to a client as "proven." |
| [`docs/demo-corpora.md`](docs/demo-corpora.md) | The three demo corpora as **role-specific worked examples**, not interchangeable defaults - e-commerce (default, permissions + escalation), Wikipedia 10k (scale-benchmark **filler only, never gold**), CRM (text-to-SQL optional module, X1) - and the honest framing that swapping in your own corpus makes the example anchors **fail loud**, so "replace the corpus" and "author a new golden set" are the same step. |
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
evals/retrieval/        60-question golden set + E7 escalation golden set + runners + CI workflow integration
evals/permissions_scale/ Wikipedia 10k corpus benchmark + nightly workflow
evals/structured_rag/   Text-to-SQL eval
evals/gate/             Gate-class registry (US-101) + pinned-security loader/asserts (US-102): security outputs are pinned-fail, un-downgradable, silenced only by deleting the eval; buyer-authored gate.yaml declaration carries the RAGAS gates' project bindings (cells/thresholds/cross-family judge map, US-103) + a per-suite off|comment|fail verdict layer mapping severity to a CI action over the 3 quality suites (US-104) + the determinism-to-CI-placement rule (placement.py, US-105): a per_pr: section opts deterministic gates into per-PR merge-blocking and structurally rejects a per-PR fail on a non-deterministic (LLM-judged) gate
db_seed/                Deterministic seeders for the eval corpora (+ generic_seed for your own/production corpus)
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
| `SUPABASE_SERVICE_ROLE_KEY` | yes | Reserved for system-level ops (share API owner-lookup, ingestion, support-bot provisioning via the GoTrue admin API - US-069, `backend/support_bot.py`, the backend-mediated conversation-token surface - issuance + the `resume_conversation` RPC, US-071, `backend/conversation_tokens.py`, the anonymous public widget-key resolution gate - US-072, `backend/widget_keys.py`, and the read-only support-bot identity lookups behind the share-to-bot surface - resolving/blocking the bot on the document share endpoints - US-086, `backend/support_bot.py`); never used to touch user data on the retrieval path (RLS enforced via user JWT). The public widget endpoints fail closed with a 503 when it is unset |
| `SUPABASE_JWT_SECRET` | only for support bot | The project JWT secret GoTrue signs with. The support bot self-signs its short-lived bot token with it so `auth.uid()`/RLS resolve it natively (US-068, `backend/supabase_jwt.py`); a knowledge-assistant-only deploy leaves it blank. NEW signing surface - keep server-side only, never embed client-side |
| `SUPPORT_BOT_EMAIL_DOMAIN` | no | Internal, non-routable email domain for the per-workspace support bot's `auth.users` row (US-069, `backend/support_bot.py`). Default `bots.support.internal`. The bot row is admin-created with `email_confirm=true` and no password, so the address never logs in or receives mail |
| `OPENAI_API_KEY` | yes | |
| `OPENAI_MODEL` | no | Default `gpt-4o-mini` |
| `OPENAI_VECTOR_STORE_ID` | no | Enables `file_search` retrieval when set |
| `PARSER` | no | Ingestion parser: `docling` (default) / `llamaparse` / `unstructured`. Invalid value fails fast at startup. To add your own, see [docs/ingestion-parser-adapters.md](docs/ingestion-parser-adapters.md) |
| `LLAMA_CLOUD_API_KEY` | only if `PARSER=llamaparse` | LlamaParse cloud key; checked at startup, not first ingest |
| `FRONTEND_ORIGIN` | yes (prod) | Comma-separated list of allowed CORS origins for the **authenticated** app surface (`/api/*`, `/healthz`). Defaults to `http://localhost:5173` for dev. The public widget surface (`/widget/*`) does NOT use this - it has its own posture keyed off each active widget key's registered origins (US-074) |
| `WIDGET_CORS_ORIGIN_CACHE_TTL` | no | Seconds the public-widget CORS layer caches the union of active-key registered origins before re-reading under the service role. Default 30; must be `> 0`. Issuing/revoking a key invalidates the cache immediately on that instance; the TTL is the cross-instance backstop (US-074) |
| `RATE_LIMITER` | no | Backend for the public-widget abuse/cost-DoS rate limiter (US-075 seam, `backend/rate_limiting.py`). `postgres` (default - durable counter rows reached over PostgREST via service-role RPCs) or `redis`. No in-memory backend by design (it would under-count per replica and reset on restart). Fails closed at startup on a misconfigured backend; the limiter is only built when support is configured (`SUPABASE_SERVICE_ROLE_KEY` set) |
| `REDIS_URL` | only if `RATE_LIMITER=redis` | Redis connection URL for the Redis limiter backend. The `redis` package is an optional dependency (not in `requirements.txt`; `pip install redis`). Checked at startup |
| `WIDGET_RATE_LIMIT_WINDOW_SECONDS` | no | Sliding-window length (seconds) for the public-widget per-key + per-session/IP rate limits. Default 60; must be `> 0` (US-076) |
| `WIDGET_RATE_LIMIT_PER_KEY` | no | Max requests per `public_key` per window, aggregated across every session/IP. Default 300; must be `> 0`. A breach refuses with a 429 + `Retry-After`, having done no retrieval/LLM work (US-076) |
| `WIDGET_RATE_LIMIT_PER_SESSION` | no | Max requests per session/IP (best-effort left-most `X-Forwarded-For` hop) per window, across every key. Default 30; must be `> 0`. Defense-in-depth - the per-key window and an edge/WAF limiter (P5) are the harder bounds (US-076) |
| `WIDGET_RATE_LIMIT_MESSAGE_COST` | no | Rate-limit `cost` a customer message turn charges against both windows (vs `cost=1` for a key-resolution open), since a message creates a row, issues a token, and drives a retrieval+LLM pipeline. Default 5; must be `> 0` (US-078) |
| `WIDGET_BREAKER_PER_WORKSPACE` | no | Per-workspace circuit-breaker ceiling: max aggregate requests for one workspace's whole bot per window, drawn down on a `ws:<id>` bucket via the same `RateLimiter` (US-077, `backend/circuit_breaker.py`). Default 600 - deliberately above organic single-workspace traffic, a cost-runaway backstop, not the everyday throttle (that is US-076). When tripped the turn returns a generic deferral with no retrieval/LLM and escalates to a human; must be `> 0` |
| `WIDGET_BREAKER_WINDOW_SECONDS` | no | Sliding-window length (seconds) for the per-workspace circuit breaker. Defaults to `WIDGET_RATE_LIMIT_WINDOW_SECONDS`; must be `> 0` (US-077) |
| `WIDGET_SSE_KEEPALIVE_SECONDS` | no | Interval (seconds) between `: keepalive` comments on the customer-reply SSE (`GET /widget/conversations/{id}/events`) so an idle channel survives proxy idle-timeouts. Default 25; must be `> 0` (US-081) |
| `WIDGET_SSE_REVALIDATE_SECONDS` | no | How often (seconds) the customer-reply SSE re-checks its opaque token while open (`slide=False` - a read that never extends the 24h window); when the conversation is resolved its token is purged, the re-check fails, and the stream emits `event: close` and ends. Default 60; must be `> 0` (US-081) |
| `WIDGET_FANOUT_DATABASE_URL` | no | Optional DIRECT asyncpg DSN for the shared Postgres, enabling the multi-instance customer-SSE fan-out bridge (`backend/conversation_bridge.py`, US-081). Unset → single-instance in-process fan-out only (the common kit deployment; the bridge is never built). Set it on a horizontally-scaled backend so an agent reply written on one instance reaches a customer SSE held on another over Postgres `LISTEN/NOTIFY` (no Redis/queue infra). Independent of the PostgREST path - `LISTEN` needs a real connection PostgREST cannot hold |
| `CHAT_MODE_DEFAULT` | no | `responses` or `completions`. Defaults to `responses` on an `openai` answerer, `completions` on any other provider. `responses` is OpenAI-only and fails closed at startup on a non-`openai` answerer — see [docs/model-surface.md](docs/model-surface.md) |
| `CHAT_HISTORY_MAX_TURNS` | no | Default 10 |
| `RETRIEVAL_MODE` | no | `hybrid` (default) / `vector` / `keyword`. Safety escape hatch — production uses hybrid |
| `SEARCH_SIMILARITY_THRESHOLD` | no | Cosine threshold for `match_chunks` filter. Default 0.3 |
| `HYBRID_RRF_K` | no | RRF damping constant. Default 60 |
| `HYBRID_FUSION_ALPHA` | no | Hybrid fusion vector-leg weight. `auto` (default) picks a per-query weight from `predict_alpha`, tilting identifier-dense queries toward the lexical leg; a fixed float in `[0, 1]` pins every query (`0.5` reproduces legacy equal-weight RRF exactly). Junk/out-of-range fails at startup (US-116) |
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
| `GENERIC_SEED_DATABASE_URL` | no | Writable Postgres URL used only by `python -m db_seed.generic_seed` (seed your own corpus + optional real-grant manifest). Falls back to `DATABASE_URL` |
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
| `DELETE` | `/api/documents/{id}/shares/{principal_id}` | Revoke a grant. The support bot is refused by `POST /share` and hidden from `GET /shares`; it is published/unpublished only through the `publish-to-bot` trio below (US-086) |
| `GET` | `/api/documents/{id}/publish-to-bot` | Owner-only: report whether the doc is published to the workspace's public support widget - `{published, bot_provisioned}` (`bot_provisioned=false` ⇒ support not enabled for the workspace) (US-086) |
| `POST` | `/api/documents/{id}/publish-to-bot` | Owner-only: the distinct, explicitly-confirmed "publish to the public widget" action - grants the support bot via the SAME per-chunk `chunk_acl` mechanism as a user share (retrieval unchanged), so the doc's synthesized answer becomes reachable to anyone who can reach the widget. Requires `status='ready'`; a bot-less workspace is a 409 (never a silent provision). The loud consequence confirmation is enforced in the UI (US-086) |
| `DELETE` | `/api/documents/{id}/publish-to-bot` | Owner-only: unpublish - revoke the bot's `chunk_acl` grants. Idempotent and fail-safe (204 even with no bot or an unpublished doc) (US-086) |
| `POST` | `/api/search` `/api/search/keyword` `/api/search/hybrid` `/api/search/rerank` | Direct retrieval probes (debugging / eval) |
| `POST` | `/api/sql` | Text-to-SQL via the semantic-layer compiler |
| `POST` | `/api/web-search` | Web fallback |
| `POST` | `/api/subagent` | Spawn a document sub-agent |
| `POST` | `/api/support/widget-keys` | Admin: issue a new (non-secret) widget public key for a workspace you administer; the first key issued lazily provisions the workspace support bot. Rejects an empty/blank `allowed_origins` with a 400 - a key with no registered origin is inactive under the US-073 fail-closed gate, so it is refused at creation rather than minted dead (US-072/US-073) |
| `GET` | `/api/support/widget-keys?workspace_id=…` | Admin: list a workspace's widget keys (active + revoked) for the `/support/settings` UI (US-072) |
| `POST` | `/api/support/widget-keys/{key_id}/revoke` | Admin: revoke a widget key - a one-way `revoked_at` latch that blocks new conversations but never terminates a live one (US-072) |
| `POST` | `/widget/keys/resolve` | Public widget: resolve a non-secret `public_key` on open, gating on not-revoked then the per-key registered-origin allowlist (fail-closed - an empty allowlist or a missing/unlisted request `Origin` is refused with the same opaque 404) under the service role; returns `{"active": true}` or 404 and leaks no workspace topology. Rate-limited per-session/IP (charged first, before the resolve) and per-key (after the resolve), 429 + `Retry-After` on breach (US-072/US-073/US-076) |
| `POST` | `/widget/conversations/resume` | Public widget: resume an anonymous support conversation from its opaque per-conversation token (`X-Conversation-Token`, not a JWT) - slides the 24h window (US-071) |
| `GET` | `/widget/conversations/{id}/transcript` | Public widget: fetch a conversation's transcript, authorized by the same opaque token; read-only, never slides the window (US-071) |
| `POST` | `/widget/conversations/messages` | Public widget: a customer message. With no `X-Conversation-Token` header it is the FIRST message - re-runs the not-revoked + origin gates, lazily creates the `conversations` row (`status='active'`, `channel='widget'`, bot wired) + opaque token, and returns that raw token ONCE in the `X-Conversation-Token` response header; with the header present it RESUMES the existing conversation, creating no row (widget open alone creates nothing). Returns the request-scoped SSE the bot answer streams over: after a `conversation` event it runs the ADR-0003 deterministic deflection turn AS the workspace bot (behind the US-077 per-workspace breaker), persists the bot reply (role='assistant', `tool_calls` null), and streams it as `delta` events before `done`; on the escalate branch (weak retrieval, unfaithful draft, tripped breaker, or any failure) it streams the fixed generic deferral, never a confident answer (US-079). A deliberate escalate (the pipeline deciding `escalated`, or a tripped breaker) latches the conversation to `status='escalated'`; thereafter the bot is silent - every later customer message is persisted and routed to the human queue with the pipeline NEVER run (`conversation` + `done` only), and a confident answer that races a mid-flight escalation is suppressed (US-080). Rate-limited at the heavier `WIDGET_RATE_LIMIT_MESSAGE_COST` (US-078) |
| `POST` | `/widget/conversations/escalate` | Public widget: explicit user-initiated "talk to a human" (US-091's button) - authed by the opaque per-conversation token (`X-Conversation-Token`, not a JWT), runs NO deflection pipeline (UI-initiated, never a model tool), and latches the conversation to `status='escalated'` via the SAME path the model-mediated decision uses, so the bot goes silent thereafter. The `escalated_at` latch is owned by the US-067 DB trigger; re-escalating is an idempotent no-op. A missing/expired/resolved token -> 401 (start fresh); a latch-write failure -> 502 (the button can retry). Rate-limited per-session (US-080) |
| `POST` | `/widget/conversations/{id}/agent-reply` | Operator dashboard: a workspace agent (human teammate) posts a reply into a support conversation - the human leg, and the ONE authenticated `/widget/*` route (authed by the agent's REAL Supabase JWT via `get_user`, NOT the anonymous opaque token; the `_WIDGET_AUTHENTICATED_SUFFIXES` exception that rides the authenticated `/api/*` CORS posture, US-074). The read + write run under the agent's own JWT, so the US-066 workspace-membership RLS IS the authorization: a member may reply, a cross-workspace agent gets an opaque 404 (RLS-hidden). Writes `role='assistant'` (the US-066 CHECK has no `'agent'` role, so no migration), `tool_calls` null; permitted regardless of status - in particular on `escalated`, where the bot is silent (US-080) and the agent is the only message source. After the durable write the reply is fanned to the customer's live backend SSE through the in-process registry (`backend/conversation_fanout.py`), never Supabase Realtime; 0 open SSEs just means nothing live - the reply is durable and recovered via the transcript on reconnect. The customer-facing GET SSE channel (`GET /widget/conversations/{id}/events`) + the multi-instance `LISTEN/NOTIFY` bridge that feed this same registry are delivered by US-081 (US-082, ADR-0004/0008) |
| `GET` | `/widget/conversations/{id}/events` | Public widget: the customer's long-lived SSE for async human-agent replies (US-081) - the customer leg's live push. Authed by the opaque per-conversation token (`X-Conversation-Token`, not a JWT; consumed via fetch + ReadableStream, never `EventSource`, to keep the token out of the URL/logs), bound to the path `id` so a token for X can never open Y's stream (missing/expired/resolved/mismatched -> opaque 401). Emits `event: ready` then one `event: message` per agent reply (customer-safe body only - id/role/content/created_at), drained from the in-process fan-out registry (`backend/conversation_fanout.py`) which is fed by the local `agent-reply` publish and, on a multi-instance deployment, the `LISTEN/NOTIFY` bridge (`backend/conversation_bridge.py`) publishing into it. A `: keepalive` comment every `WIDGET_SSE_KEEPALIVE_SECONDS` survives proxy idle-timeouts; the token is re-validated every `WIDGET_SSE_REVALIDATE_SECONDS` (`slide=False`, never sliding the window) and the stream emits `event: close` when the conversation is resolved (its token purged, US-071). Per-session rate-limited at open (US-076; no `public_key` on this path); the client keeps the transcript poll as a backstop so a dropped SSE never loses a reply (US-081) |
| `GET` | `/healthz` | Liveness check |

## Embedding the support widget

A buyer embeds the support widget with one tiny loader `<script>`:

```html
<script
  src="https://YOUR-KIT-ORIGIN/widget.js"
  data-public-key="wk_pk_xxx"
  data-brand-color="#2563eb"
  data-greeting="Hi! Ask us anything."
  data-title="Acme Support"
  data-position="bottom-right"
  async></script>
```

The loader injects a **cross-origin iframe served from the kit's own origin** and
talks to the host page via `postMessage` only (`window.SupportWidget.open()` /
`close()` / `toggle()`; an `onUnread` callback + a `supportwidget:unread`
CustomEvent). The conversation token + session live in the iframe's own-origin
`localStorage`, so the host page's JS - or an XSS on it - cannot read them. The
`data-public-key` is the non-secret US-072 key. The in-iframe chat UI (message
list, composer, streamed answers, agent replies, unread badge) is themed entirely
from the loader's `init` config (`data-brand-color` / `data-greeting` /
`data-title` / `data-launcher-icon` / `data-position`) - no host CSS. A
web-component / shadow-DOM embed is deliberately rejected (same JS realm). Full
architecture, the message contract, theming, and local cross-origin verification:
[docs/widget-embed.md](docs/widget-embed.md). Shell = US-083; chat UI + theming =
US-084; the live customer-SSE push for agent replies is US-081 (`GET
/widget/conversations/{id}/events`, a fetch-based SSE, with the transcript poll
retained as a backstop; a multi-instance backend sets `WIDGET_FANOUT_DATABASE_URL`
to carry replies between instances over Postgres `LISTEN/NOTIFY`).

Workspace **admins** manage the widget from the in-app `/support/settings` route
(US-090) - the one admin-gated support surface (`role='admin'`), deliberately
distinct from the membership-gated queue below. Issuing a workspace's first widget
key there is what **enables support** (it lazily provisions the workspace bot), so
there is no separate enable toggle. Admins issue keys with a label + registered
origins (an empty allowlist is inactive/fail-closed; `*` is a dev-only wildcard),
copy the non-secret `public_key` and its embed snippet, rotate a key (issue-new +
revoke-old, composed client-side), and revoke one. Share-to-bot (below) is also
managed here for the admin's own documents. The UI gate is cosmetic; the hard
boundary is the `widget_keys` admin RLS enforced under the caller's own JWT
(US-072), so a non-admin's issue is a Postgres 403 and their key list reads back
empty.

Workspace members pick up the human handoffs from the authenticated in-app
operator queue at `/support/queue` (US-087): it lists the active workspace's
`status='escalated'` conversations, oldest-first, and live-updates them via each
member's own Supabase Realtime under their real JWT - membership-gated, not
role-gated (any member of the workspace, `role` in no gate). Selecting a
conversation opens a two-pane transcript view (US-088): the agent reads the full
`conversation_messages` transcript (a direct Supabase read under their own JWT +
membership RLS, so a non-member reads zero rows; a light 5s poll keeps it current
since `conversation_messages` is not on the Realtime publication), posts a reply
(via `POST /widget/conversations/{id}/agent-reply` above), and clicks **Resolve**
to latch the conversation to the terminal `status='resolved'` - which purges the
customer's reconnect token (US-071) and drops the row from the queue. An agent can
optionally **claim** a conversation (US-089) so it dims in every other agent's live
queue - cutting accidental double-replies - and shows "Claimed by {you|email}" (the
claimer's email resolved from `profiles`). The claim is advisory only: last-write-wins,
and it NEVER gates the reply or Resolve controls, so any workspace member may still
reply to a claimed conversation. It is a plain `claimed_by` / `claimed_at` UPDATE that
rides the same `conversations_update_member` membership RLS as Resolve and propagates
live over the existing `conversations` Realtime feed - no new endpoint, migration, or
env.

The bot answers only from documents a workspace owner has explicitly **published
to the widget** - a separate, confirmed "publish to the public support widget"
action (`POST /api/documents/{id}/publish-to-bot`, surfaced in the document share
dialog and on the admin `/support/settings` page) kept structurally apart from
normal teammate sharing, so a doc's synthesized answer can never be made
customer-reachable by accidentally granting the bot in the ordinary share box
(US-086).

## Eval suite

The CI workflows wrap the eval runners:

- **`.github/workflows/retrieval-eval.yml`** - runs on PRs that touch retrieval / chunking / embeddings / escalation / migrations, the API-edge surface (`backend/main.py` / `backend/permissions.py`), the AU4 suite, or the runner itself. A lightweight `changes` job (dorny/paths-filter) splits the diff so each deterministic gate runs only for a relevant change. Retrieval-path changes run the **`retrieval-eval` job**: the 60-question golden set against PR head AND `main` with a delta-vs-`main` comment (advisory - it never fails the build), plus two **hard gates** - the **E6 second-workspace zero-leak eval** (`--include-e6`) and the **E7 escalation tripwire** (`e7_runner --include-p1b`, US-059): the *deterministic* deflection legs (P1a/P1b retrieval-gate decisions + the P1b non-disclosure byte-equality assertion, no LLM). API-edge changes run the separate **`au4-security-invariants` job** - the deterministic AU4 API-layer auth-attack suite (`backend/test_au4_auth_attacks.py`), its own job because it imports `backend/main.py` (docling/torch); it exits non-zero on any cross-workspace leak or broken opaque-token binding. All three gates are deterministic, so a real verdict can't flake and each may hard-block the merge (US-105: determinism, not buyer preference, decides what may block); a transient E6 execution error is surfaced loudly but stays non-blocking.
- **`.github/workflows/ship-green.yml`** — runs on PRs and on `push: main` that touch the green-determining surface (the shipped `db_seed/corpus/` + seeder, the golden set + runner, the gate + E4 security assert, the production chunk/embed/retrieval paths, or the migrations), plus manual `workflow_dispatch`. This is the **"kit ships green out of the box"** guarantee (US-111): a clean `python -m db_seed.corpus_seed` → `python -m evals.retrieval.runner --viewers all` on the shipped artifacts with zero buyer authoring, then a hard assert that the `security_no_access` table is **1.000** across every `no_access` cell (reusing the same pinned `evals/gate/security.py` invariant, with an `e4_structurally_blind` guard that refuses a vacuous pass). Unlike `retrieval-eval.yml` (an advisory *delta-vs-main* comment) this is an **absolute** green assertion, and by also running on `push: main` it catches a demo corpus that rots directly on main. E4-only by design — E6 is already the per-PR hard gate and the LLM-judged legs are scheduled-only (US-105).
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
python -m evals.retrieval.runner                      # all three modes; exits 1 on an E4 no_access zero-leak breach (pinned security invariant, US-102)
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
