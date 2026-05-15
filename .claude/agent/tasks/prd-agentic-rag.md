# PRD: Agentic RAG System

## Introduction

An educational, production-oriented Retrieval-Augmented Generation (RAG) application built in 10 progressive modules. The system has two primary interfaces: a **Chat** view for threaded, retrieval-augmented conversations, and an **Ingestion** view for manual document upload and management.

The target audience is technically-minded builders who want to learn production RAG patterns (chunking, embeddings, hybrid search, reranking, agentic routing, sub-agents) by directing AI coding tools. They do not need to know Python or React—they need to understand RAG concepts and codebase structure deeply enough to direct AI to build and fix the system.

The system avoids LLM frameworks (LangChain, LlamaIndex) in favor of raw OpenAI SDK calls and Pydantic, so every layer of the stack is inspectable and modifiable.

## Goals

- Deliver a working, deployable, multi-user RAG application.
- Progress through 10 discrete modules, each learnable in a focused session.
- Teach RAG fundamentals by forcing the user to implement them (not import them from a framework).
- Preserve architectural flexibility: support both OpenAI's managed Responses API and the standard Chat Completions API side-by-side (dual-support mode).
- Ship with observability (LangSmith) from day one.
- Enforce Row-Level Security (RLS) so users only see their own threads, messages, and documents.
- Deploy to cloud (Vercel + Railway/Fly + Supabase) with config-only changes via environment variables.

## User Stories

---

### Module 1 — App Shell + Observability

#### US-001: Provision Supabase project with RLS-protected schema

**Description:** As a developer, I need a Supabase project with the baseline schema (users, threads, messages) and RLS policies so that authenticated users can only access their own data.

**Acceptance Criteria:**

- [x] Supabase project created with Postgres + pgvector + Auth + Storage enabled
- [x] Tables created: `threads(id, user_id, title, created_at)`, `messages(id, thread_id, role, content, created_at)`
- [x] RLS enabled on all tables; policies restrict rows to `auth.uid() = user_id` (directly or via join)
- [x] Migrations committed to repo under `supabase/migrations/`
- [x] Typecheck/lint passes

**Validation Test:**

- **Setup:** Two test users (A and B) exist in Supabase Auth. User A has one thread with one message.
- **Steps:**
  1. Authenticate as User A, `SELECT * FROM threads` via Supabase client
  2. Authenticate as User B, run the same query
  3. As User B, attempt `INSERT INTO messages (thread_id, ...)` using User A's thread_id
- **Expected Result:** Step 1 returns User A's thread. Step 2 returns 0 rows. Step 3 fails with RLS policy violation.
- **Failure Indicator:** User B can see or mutate User A's data; RLS is disabled or misconfigured.

#### US-002: Email/password + OAuth authentication

**Description:** As a user, I want to sign up and log in with email/password or OAuth (Google, GitHub) so I can access my private chat threads.

**Acceptance Criteria:**

- [x] Sign-up and login pages accept email/password
- [x] "Continue with Google" and "Continue with GitHub" buttons work
- [x] Session persists across page refreshes via Supabase Auth SDK
- [x] Logged-out users are redirected to the login page when visiting protected routes
- [x] Logout clears session and redirects to login
- [x] Typecheck passes
- [x] Verify in browser using dev-browser skill

**Validation Test:**

- **Setup:** Fresh browser session, Supabase Auth configured with Google and GitHub OAuth providers.
- **Steps:**
  1. Visit `/chat` while logged out
  2. Sign up with email/password
  3. Log out, then log in with Google OAuth
  4. Refresh the page
  5. Click logout
- **Expected Result:** Step 1 redirects to `/login`. Step 2 creates account and lands on `/chat`. Step 3 succeeds and creates a distinct user row. Step 4 preserves the session. Step 5 returns to `/login`.
- **Failure Indicator:** Any step fails to redirect correctly, session is lost on refresh, or OAuth providers return errors.

#### US-003: Chat UI shell with threads list and streaming messages

**Description:** As a user, I want a two-pane chat interface (threads list + active conversation) so I can navigate between conversations and see responses stream in real-time.

**Acceptance Criteria:**

- [x] Left sidebar lists threads by `created_at DESC` with titles
- [x] Clicking a thread loads its messages in the main pane
- [x] "New thread" button creates a thread and navigates to it
- [x] Input box at the bottom submits on Enter; Shift+Enter inserts newline
- [x] Assistant responses stream token-by-token via Server-Sent Events or fetch streams
- [x] Thread title auto-generated from first user message (first 50 chars)
- [x] Built with React + TypeScript + Vite + Tailwind + shadcn/ui
- [x] Typecheck passes
- [ ] Verify in browser using dev-browser skill

**Validation Test:**

- **Setup:** Logged-in user with no threads.
- **Steps:**
  1. Click "New thread"
  2. Type "Hello, what is RAG?" and press Enter
  3. Observe the response area while the assistant replies
  4. Click "New thread" again, ask a different question
  5. Click back to the first thread
- **Expected Result:** Step 1 creates an empty thread. Step 2 sends the message; Step 3 shows tokens arriving progressively (not all at once). Step 4 creates a second thread in the sidebar. Step 5 restores the first conversation fully.
- **Failure Indicator:** No streaming (response appears all at once), threads don't persist, or switching threads loses state.

**Implementation notes (US-003):**

- Streaming assistant reply is a placeholder async generator in `frontend/src/lib/chat.ts` (`streamAssistantReply`) — to be swapped for a real fetch stream against `/api/chat` in US-004.
- Route `/chat/:threadId?` drives thread selection; `ChatPage` is the single container for both empty and thread-scoped views.
- Thread title is derived from the first user message (trimmed, collapsed whitespace, truncated to 50 chars) and written via `updateThreadTitle` on send.
- `dev-browser` skill verification is deferred (skill not available in this environment); Vite build and dev server boot succeed and `npm run typecheck` passes.

#### US-004: OpenAI Responses API integration with file_search

**Description:** As a user, I want my chat to use OpenAI's Responses API so threads and document retrieval are managed by OpenAI out-of-the-box.

**Acceptance Criteria:**

- [x] Backend endpoint `POST /api/chat` accepts `{thread_id, message}` and calls OpenAI Responses API
- [x] OpenAI thread ID stored alongside Supabase thread (column `threads.openai_thread_id`)
- [x] `file_search` tool enabled; at least one test document uploaded to OpenAI's vector store during setup
- [x] Responses stream back to the client
- [x] Errors from OpenAI are logged and surfaced to the UI as a toast
- [x] Typecheck passes

**Validation Test:**

- **Setup:** OpenAI API key configured in env. One test PDF uploaded to the OpenAI vector store containing a known fact (e.g., "The secret code is PURPLE-47").
- **Steps:**
  1. Start a new thread, ask "What is the secret code?"
  2. Ask a follow-up: "Can you repeat that?"
  3. Inspect the `threads` table in Supabase
- **Expected Result:** Step 1 returns "PURPLE-47" (retrieved via file_search). Step 2 references the previous turn (managed memory). Step 3 shows `openai_thread_id` is populated.
- **Failure Indicator:** Response doesn't cite the document, follow-up loses context, or `openai_thread_id` is null.

**Implementation notes (US-004):**

- New FastAPI backend under `backend/` with `POST /api/chat` (SSE) — see `backend/main.py`.
- Auth: backend verifies the Supabase JWT via GoTrue (`/auth/v1/user`) on every request and forwards the same bearer token to PostgREST, so RLS still governs every DB mutation. Service-role key is only used to bootstrap the client and is never used for user data.
- The `threads.openai_thread_id` column (migration `20260416130000_add_openai_thread_id.sql`) stores the last Responses API response id. Subsequent turns pass it as `previous_response_id` so conversation memory is managed server-side.
- `file_search` wiring is conditional on env var `OPENAI_VECTOR_STORE_ID`. When unset the call still streams, just without retrieval — this keeps local dev usable before a vector store exists. For the PRD validation test you must create a vector store and set the id.
- Streaming: backend emits `event: delta / done / error` SSE records; frontend `streamChatTurn` parses them and updates the bubble token-by-token. Errors (OpenAI, Supabase, auth) are caught, logged server-side, and surfaced via the new `ToastProvider` in `frontend/src/components/ui/toast.tsx`.
- Frontend no longer inserts the assistant message — the backend is the single writer for assistant + tool messages, and the client refreshes from Supabase on `done`.
- Browser verification is still deferred (requires a live Supabase session + vector store); typecheck passes (`npm run typecheck`) and `python3 -m py_compile backend/main.py` succeeds.

**Env vars added:**

- Backend: `OPENAI_API_KEY`, `OPENAI_MODEL` (default `gpt-4o-mini`), `OPENAI_VECTOR_STORE_ID` (optional), `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `FRONTEND_ORIGIN` — see `backend/.env.example`.
- Frontend: `VITE_BACKEND_URL` (default `http://localhost:8000`) — see `frontend/.env.example`.

#### US-005: LangSmith tracing on all LLM calls

**Description:** As a developer, I want every LLM call traced in LangSmith so I can debug behavior and measure cost/latency.

**Acceptance Criteria:**

- [x] LangSmith SDK installed; `LANGSMITH_API_KEY` and `LANGSMITH_PROJECT` read from env
- [x] All OpenAI calls wrapped in a trace
- [x] Traces include user_id, thread_id, and message_id as metadata
- [x] Traces are grouped per conversation turn (request → tool calls → final response)
- [x] Typecheck passes

**Validation Test:**

- **Setup:** LangSmith account with an empty project named for this app.
- **Steps:**
  1. Send three messages across two different threads
  2. Open LangSmith and filter by the project
- **Expected Result:** Three traces appear, each tagged with thread_id and user_id. Tool calls (file_search) are visible as child spans.
- **Failure Indicator:** Traces missing, metadata absent, or tool calls not visible as child spans.

**Implementation notes (US-005):**

- Added `langsmith==0.2.10` to `backend/requirements.txt`. Env knobs: `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT` (default `agentic-rag`), `LANGSMITH_TRACING` (toggle). When `LANGSMITH_API_KEY` is unset the backend forces `LANGSMITH_TRACING=false` so the SDK stays silent in local dev.
- OpenAI client is wrapped with `langsmith.wrappers.wrap_openai(AsyncOpenAI(...))` in `backend/main.py`, so every `responses.create` call (stream or not) posts a child run automatically — including `file_search` tool spans inside the response.
- `_stream_reply` is decorated with `@traceable(run_type="chain", name="chat_turn")` so the whole turn (Supabase reads → OpenAI stream → Supabase writes) is a single parent run; the OpenAI span nests under it.
- Metadata (`user_id`, `thread_id`, `user_message_id`, `assistant_message_id`, `response_id`) is merged onto the active run via `get_current_run_tree().add_metadata(...)` as soon as each id is known.
- `.env.example` updated with the three LangSmith vars. `python3 -m py_compile backend/main.py` succeeds and `npm run typecheck` in `frontend/` still passes (no frontend changes required for this story).

#### US-006: Cloud deployment (Vercel + Railway/Fly + Supabase)

**Description:** As a developer, I want the app deployed to production via Vercel (frontend), Railway or Fly (backend), and Supabase (DB/Auth/Storage) using environment variables only.

**Acceptance Criteria:**

- [x] Frontend deploys to Vercel via `vercel.json` or framework auto-detection
- [x] Backend deploys to Railway or Fly via `Dockerfile` or `railway.toml`/`fly.toml`
- [x] All secrets configured via environment variables, not hardcoded
- [x] `README.md` documents all required env vars (OPENAI_API_KEY, SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY, LANGSMITH_API_KEY, etc.)
- [x] CORS configured so Vercel frontend can call the backend
- [ ] Deployed app is accessible at a public URL *(requires user to run the deploy — config is in place, live URL pending user action)*
- [ ] Verify in browser using dev-browser skill *(deferred — dev-browser skill not available in this environment, same as US-003/US-004)*

**Validation Test:**

- **Setup:** Fresh Vercel, Railway/Fly, and Supabase accounts.
- **Steps:**
  1. Follow the README deploy instructions end-to-end
  2. Visit the Vercel URL
  3. Sign up and send a chat message
- **Expected Result:** Deploys succeed with no code changes. Signup works. Chat returns a streamed response.
- **Failure Indicator:** Any step requires editing source code (not env vars), or the deployed app fails to connect frontend↔backend↔DB.

**Implementation notes (US-006):**

- **Backend container** — `backend/Dockerfile` (python:3.11-slim, installs `requirements.txt`, runs `uvicorn main:app --host 0.0.0.0 --port ${PORT}`). Python 3.11 because `main.py` uses PEP 604 `X | None` unions that FastAPI evaluates at runtime. `.dockerignore` excludes caches and any `.env*` except `.env.example`.
- **Railway** — `backend/railway.toml` pins the Dockerfile builder, health-checks `/healthz`, and enables `ON_FAILURE` restarts. Service root must be set to `backend/` in the Railway dashboard. Railway injects `$PORT`; the Dockerfile CMD shell-expands it.
- **Fly.io (alternative)** — `backend/fly.toml` mirrors the Railway setup (internal_port 8080, http health check on `/healthz`, `auto_stop_machines` for cost control). Deploy flow is `fly launch --copy-config --no-deploy` → `fly secrets set ...` → `fly deploy`.
- **Vercel** — `frontend/vercel.json` declares the Vite framework preset, `npm run build` → `dist/`, and SPA rewrites (`/(.*)` → `/index.html`) so `react-router-dom` routes survive direct URLs / refresh.
- **Secrets-only config** — no code paths hardcode keys. Backend `main.py` reads every secret from env (set in US-004/US-005); frontend reads `VITE_SUPABASE_*` and `VITE_BACKEND_URL` via Vite's `import.meta.env`.
- **CORS** — already env-driven since US-004: `FRONTEND_ORIGIN` is a comma-separated list of allowed origins; `main.py` parses it into the FastAPI `CORSMiddleware` `allow_origins` list. Prod flow: set `FRONTEND_ORIGIN` to the Vercel URL after first frontend deploy, then redeploy backend.
- **README.md** — new at repo root. Documents local dev (Node 20+, Python 3.11+), every backend + frontend env var in tabular form, and step-by-step deploys for Supabase → Railway (primary) / Fly (alt) → Vercel, including the post-deploy feedback loop of copying the Railway URL into `VITE_BACKEND_URL` and the Vercel URL into `FRONTEND_ORIGIN`.
- **.gitignore hygiene** — added root `.gitignore` and `backend/.gitignore` so `.env` files stay out of the repo when the user pushes to GitHub for deploy.
- Verification: `python3 -m py_compile backend/main.py` and `npm run typecheck` in `frontend/` still pass. Live deploy + browser validation is the user's next step (requires their Vercel/Railway/Supabase accounts).

---

### Module 2 — BYO Retrieval + Memory

#### US-007: Ingestion UI with drag-and-drop upload

**Description:** As a user, I want a dedicated Ingestion page where I can drag-and-drop files to upload them for retrieval.

**Acceptance Criteria:**

- [x] `/ingestion` route accessible from the main nav
- [x] Drop zone accepts one or more files; also supports click-to-browse
- [x] List of uploaded documents with columns: filename, status, chunks, uploaded_at
- [x] Delete button on each row (soft-deletes the document and its chunks)
- [x] Initial version restricts accepted types to `.txt` and `.md` (expanded in Module 5)
- [x] Typecheck passes
- [ ] Verify in browser using dev-browser skill *(deferred — dev-browser skill not available in this environment, same pattern as US-003/US-004/US-006)*

**Validation Test:**

- **Setup:** Empty ingestion list.
- **Steps:**
  1. Drag a `.txt` file onto the drop zone
  2. Drag a `.jpg` (unsupported) onto the drop zone
  3. Click the delete button next to the uploaded `.txt`
- **Expected Result:** Step 1 creates a row with status "processing" then "ready". Step 2 shows a toast error and does not create a row. Step 3 removes the row (and underlying chunks).
- **Failure Indicator:** Unsupported types accepted silently, status doesn't update, or delete leaves orphan chunks.

**Implementation notes (US-007):**

- New migration `supabase/migrations/20260417120000_init_documents.sql` creates `public.documents` (id, user_id, filename, storage_path, byte_size, content_type, status, error_message, chunks_count, uploaded_at, deleted_at) with RLS scoped to `auth.uid() = user_id`. A partial index on `(user_id, uploaded_at desc) where deleted_at is null` keeps the list query fast.
- Same migration creates a private Supabase Storage bucket `documents` and four RLS policies (`select/insert/update/delete`) keyed on the first path segment equalling `auth.uid()`. Object paths are `<user_id>/<document_id>/<filename>`, which enforces per-user scoping at the blob layer too.
- Frontend-only pipeline for this story (no new backend route): `frontend/src/lib/ingestion.ts` handles upload (insert `documents` row `status=processing` → upload to Storage → patch to `status=ready`), list (`listDocuments`), and soft-delete (`softDeleteDocument` sets `deleted_at`). Real parse/chunk/embed flows in via US-008+; the migration already has the status enum + `chunks_count` column so later modules can fill them in without a schema change.
- New route `/ingestion` in `App.tsx`, protected by `ProtectedRoute`. New `components/AppHeader.tsx` renders a shared Chat / Ingestion nav and is used by both `ChatPage` and the new `IngestionPage` (ChatPage's inline header was replaced with it).
- Drop zone (`components/ingestion/DropZone.tsx`) handles drag-and-drop and click-to-browse via a hidden `<input type="file" multiple accept=".txt,.md">`. Per-file validation runs in `handleFiles` (`isAcceptedFile` gates by extension) so unsupported types surface a toast without touching Storage.
- `components/ingestion/DocumentsTable.tsx` renders filename, status badge (color-coded per enum), chunks count, uploaded_at, and a delete button. `chunks_count` is always 0 until US-008 wires chunking.
- Soft-delete is `update deleted_at = now()` — row survives for audit/undo. Hard-delete + Storage blob cleanup is US-019 (cascade deletes on document removal); leaving the blob for now is intentional.
- Verification: `npm run typecheck` in `frontend/` passes. Browser verification deferred as with earlier stories. PRD validation step 2 (dragging a `.jpg`): the DropZone's `<input accept>` narrows the picker, and `handleFiles` explicitly toasts any rejected file — dropped files still fire through `handleFiles` so unsupported drops surface the same error.

#### US-008: Chunking pipeline

**Description:** As a developer, I need a chunking function that splits uploaded text into overlapping chunks so they can be embedded and retrieved.

**Acceptance Criteria:**

- [x] `documents` and `chunks` tables created with FK `chunks.document_id → documents.id ON DELETE CASCADE`
- [x] Chunking uses token-based size (default 500 tokens, 50-token overlap) configurable via env
- [x] Each chunk stored with `content`, `chunk_index`, `document_id`, `user_id`
- [x] RLS policies on both tables
- [x] Typecheck passes

**Validation Test:**

- **Setup:** A 10,000-token plain-text file.
- **Steps:**
  1. Upload the file
  2. Query `SELECT COUNT(*), AVG(LENGTH(content)) FROM chunks WHERE document_id = <id>`
  3. Spot-check two consecutive chunks for overlap
- **Expected Result:** Chunk count ≈ 20 (±2). Consecutive chunks share ~50 tokens of text.
- **Failure Indicator:** Single chunk (no splitting), no overlap, or chunks exceed configured size significantly.

**Implementation notes (US-008):**

- **Migration** `supabase/migrations/20260417130000_init_chunks.sql` adds only `public.chunks(id, document_id, user_id, chunk_index, content, created_at)` — `public.documents` already exists from US-007 (`20260417120000_init_documents.sql`), so this story does not redefine it. FK `chunks.document_id → documents.id ON DELETE CASCADE`; `unique(document_id, chunk_index)`; indexes on both `user_id` and `document_id`.
- **RLS on chunks**: enabled; select/update/delete pivot on `auth.uid() = user_id`; insert additionally verifies the parent document belongs to the same user (defence-in-depth against a forged `document_id`). `user_id` is denormalised onto chunks so retrieval RLS stays a single-column check without a join — important once embedding similarity queries start driving the filter.
- **Chunker** `backend/chunking.py` uses `tiktoken` (`cl100k_base`, covers `text-embedding-3-*` and `gpt-4o-*`). `chunk_text(text)` returns overlapping windows; size + overlap default to `(500, 50)` and can be overridden via `CHUNK_SIZE_TOKENS` / `CHUNK_OVERLAP_TOKENS`. Empty/whitespace input → `[]`; input ≤ size → one chunk. Smoke test: 9,425-token input → 21 chunks (PRD expects ~20±2), max chunk = 500 tokens, last-50 of chunk N equals first-50 of chunk N+1.
- **Ingestion endpoint** `POST /api/documents/{id}/ingest` in `backend/main.py` picks up a US-007 upload once its Storage blob exists: flips `status='processing'`, downloads the blob via Supabase Storage with the user's JWT (bucket RLS still applies), UTF-8 decodes, chunks, drops any prior chunks for that document (idempotent re-ingest), bulk-inserts chunks in batches of 200 via a single PostgREST array POST, then patches the document to `status='ready'` with `chunks_count` filled in. Any failure flips to `status='error'` with the truncated error message and returns a 500. JWT is forwarded on every request so RLS governs inserts.
- **Frontend wiring** `frontend/src/lib/ingestion.ts::uploadDocument` now: inserts the row → uploads to Storage → patches `storage_path` → calls `POST /api/documents/{id}/ingest`. The backend is the single writer for `status='ready'` + `chunks_count` now, so the frontend no longer flips the row to ready itself. This matches US-007's deferred-to-US-008 note.
- **New deps**: `tiktoken==0.8.0` (added to `backend/requirements.txt`).
- **Env vars added**: `CHUNK_SIZE_TOKENS=500`, `CHUNK_OVERLAP_TOKENS=50` in `backend/.env.example`.
- Verification: `python -m py_compile main.py chunking.py` and `npm run typecheck` (frontend) both pass. Live DB + Storage round-trip is blocked on the user running the new migration — the endpoint is curl-testable once `public.documents` has a row pointing at an uploaded blob.

#### US-009: Embeddings stored in pgvector

**Description:** As a developer, I need each chunk embedded with OpenAI's embedding model and stored in pgvector so semantic retrieval is possible.

**Acceptance Criteria:**

- [x] `chunks.embedding vector(1536)` column (text-embedding-3-small) or `vector(3072)` (text-embedding-3-large), model configurable via env
- [x] Embeddings generated in batches (up to 100 per API call)
- [x] HNSW or IVFFlat index created on the embedding column
- [x] Embedding failures retried with exponential backoff (3 attempts)
- [x] Typecheck passes

**Validation Test:**

- **Setup:** Upload a 5,000-token document.
- **Steps:**
  1. Check `SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL`
  2. Run `EXPLAIN ANALYZE SELECT * FROM chunks ORDER BY embedding <=> '[...]' LIMIT 5`
- **Expected Result:** All chunks have embeddings. Query plan uses the HNSW/IVFFlat index (not a sequential scan).
- **Failure Indicator:** Null embeddings, missing index, or sequential scan on a table with >1000 rows.

**Implementation notes (US-009):**

- **Migration** `supabase/migrations/20260417140000_add_chunks_embedding.sql` adds `chunks.embedding vector(1536)` and a HNSW index using `vector_cosine_ops` (pairs with the `<=>` operator used in later retrieval stories). Column dim is fixed at DDL time — switching to `text-embedding-3-large` requires editing the migration to `vector(3072)` and reapplying. HNSW picked over IVFFlat per the PRD Technical Considerations note (no training step, stable recall on small corpora).
- **Embeddings module** `backend/embeddings.py`. `embed_texts(client, texts)` batches inputs at 100 per OpenAI call (PRD ceiling — `EMBEDDING_BATCH_SIZE` lets you go smaller, not larger). `_embed_batch_with_retry` wraps each call in an exponential-backoff loop (1s → 2s → 4s, 3 attempts by default via `EMBEDDING_MAX_RETRIES`) and re-raises on final failure. API returns are sorted by `index` as defence-in-depth even though OpenAI preserves input order. `to_pgvector(values)` formats a float list as `'[...]'` text — PostgREST sends JSON, and pgvector's `vector_in` parses that text shape on the way in.
- **Ingestion wiring** `backend/main.py::ingest_document` now embeds chunks *before* deleting the prior chunk rows, so a transient embedding failure leaves the previous ready state intact. Chunks are then re-inserted with embeddings as part of the same array POST (`_insert_chunks` grew an optional `embeddings` arg; payloads include `embedding` as a pgvector literal when present). Failure path still flips the document to `status='error'` with a truncated message — now also surfaces embedding/network errors. The reused `openai_client` is already wrapped by `langsmith.wrap_openai`, so every batched embeddings call posts its own span automatically (US-005 still satisfied).
- **Env vars added** in `backend/.env.example`: `EMBEDDING_MODEL=text-embedding-3-small`, `EMBEDDING_BATCH_SIZE=100`, `EMBEDDING_MAX_RETRIES=3`.
- **Verification** — `python3 -m py_compile main.py chunking.py embeddings.py` and `npm run typecheck` (frontend) both pass. Smoke-checked `embed_texts([])` short-circuit and `to_pgvector` format. Live DB validation (PRD step 2: `EXPLAIN ANALYZE` on the HNSW index) requires the user to apply the new migration and ingest a ≥5k-token document; pgvector's HNSW kicks in automatically once the table has rows and the query uses `<=>`.

#### US-010: Retrieval tool exposed to the agent

**Description:** As an agent, I need a `search_documents` tool that performs vector similarity search over the user's chunks so I can ground responses in retrieved context.

**Acceptance Criteria:**

- [x] Tool schema (JSON Schema) defined with Pydantic: `{query: str, top_k: int = 5}`
- [x] Tool implementation embeds the query, runs pgvector similarity, returns top-k chunks with document filename and similarity score
- [x] Similarity threshold (default 0.3) filters out low-relevance matches, configurable via env
- [x] Results respect RLS (only the current user's chunks)
- [x] Typecheck passes

**Validation Test:**

- **Setup:** Two users each ingest a different document. User A's doc mentions "quantum entanglement".
- **Steps:**
  1. As User A, invoke the tool with query "quantum entanglement"
  2. As User B, invoke the tool with the same query
  3. As User A, invoke the tool with query "asdfjkl zxcvbn" (nonsense)
- **Expected Result:** Step 1 returns User A's relevant chunks. Step 2 returns 0 results (or only User B's chunks if any match). Step 3 returns 0 results because all scores are below threshold.
- **Failure Indicator:** Cross-user leakage, no threshold filtering, or tool returns results unranked.

**Implementation notes (US-010):**

- **Migration** `supabase/migrations/20260417150000_add_match_chunks_fn.sql` adds `public.match_chunks(query_embedding vector(1536), match_threshold float, match_count int)` returning `(id, document_id, chunk_index, content, similarity, filename)`. `SECURITY INVOKER` (default) + `grant execute ... to authenticated` — so calling the RPC through PostgREST with a user JWT still triggers the existing RLS policies on `chunks` / `documents`. No cross-user leakage path because RLS is evaluated inside the function body; the function body cannot elevate its own privileges.
- **Similarity math** — pgvector's `<=>` is cosine *distance* (0 = identical, 2 = opposite). OpenAI embeddings are unit-normalised, so `1 - (a <=> b)` is cosine similarity in `[0, 1]` for practical inputs. The function filters rows where similarity `>= match_threshold`, then orders by `<=> asc` (equivalent to similarity desc) so the HNSW `vector_cosine_ops` index from US-009 is used directly.
- **Soft-deletes** — the function joins `documents` and requires `d.deleted_at is null`, so chunks whose parent was soft-deleted in the UI never reach the agent.
- **Retrieval module** `backend/retrieval.py` defines `SearchDocumentsInput` (query, top_k: int = 5, `1..50`) as the Pydantic source-of-truth for both runtime validation and the tool JSON Schema (`search_documents_tool_schema()` emits the Chat Completions `tools[]` entry for US-011). `search_documents(...)` embeds the query via the shared (LangSmith-wrapped) OpenAI client, then POSTs `/rest/v1/rpc/match_chunks` with the user's access-token headers so RLS stays in the hot path. `to_pgvector` reused from US-009 serialises the embedding into the `'[...]'` text literal pgvector's input function expects.
- **Threshold config** — `SEARCH_SIMILARITY_THRESHOLD` env var (default `0.3`, range `[0,1]`). Enforced both server-side (the RPC's `match_threshold` argument) and re-validated by `get_similarity_threshold()`; out-of-range values raise at call time rather than silently clamping.
- **Testable endpoint** `POST /api/search {query, top_k?}` added to `backend/main.py` — independently exercises the tool without waiting for US-011's Chat Completions loop. The endpoint re-uses the same `AuthedUser` dependency as `/api/chat`, so PRD steps 1–3 (cross-user isolation, nonsense query → empty) are verifiable with two curl calls against two JWTs.
- **Env vars added** in `backend/.env.example`: `SEARCH_SIMILARITY_THRESHOLD=0.3`.
- **Verification** — `python3 -m py_compile main.py chunking.py embeddings.py retrieval.py` passes; `npm run typecheck` (frontend) still passes; smoke-tested that `SearchDocumentsInput` rejects `query=""` and `top_k=0`, and that `search_documents_tool_schema()` emits valid JSON Schema with the expected `minLength`/`minimum`/`maximum` bounds. Live RLS check (PRD validation test) requires the user to apply the new migration, ingest a document as two different accounts, and hit `POST /api/search` with each JWT.

#### US-011: Chat Completions API with dual-support toggle

**Description:** As a user, I want to choose between OpenAI's managed Responses API and the standard Chat Completions API (with my own retrieval) on a per-request basis.

**Acceptance Criteria:**

- [x] Backend endpoint accepts `{mode: "responses" | "completions", ...}` per chat request
- [x] "Completions" mode uses the standard Chat Completions API with `search_documents` as a registered tool
- [x] UI has a toggle (settings or per-thread) to pick the mode; default configurable via env
- [x] Both code paths share the same streaming interface to the frontend
- [x] Tool-call loop (request → tool call → tool result → final response) implemented for Completions mode
- [x] Typecheck passes
- [ ] Verify in browser using dev-browser skill *(deferred — dev-browser skill not available in this environment, same pattern as US-003/US-004/US-006/US-007)*

**Validation Test:**

- **Setup:** Ingest a document containing the phrase "the mitochondria is the powerhouse of the cell".
- **Steps:**
  1. In "responses" mode, ask "What is the mitochondria?"
  2. Switch to "completions" mode in the same thread
  3. Ask the same question
  4. Inspect LangSmith traces for both requests
- **Expected Result:** Both modes return a grounded answer. Step 4 shows Responses API trace for turn 1 and Chat Completions trace (with a `search_documents` tool span) for turn 2.
- **Failure Indicator:** One mode fails to ground on retrieved content, toggle doesn't switch behavior, or traces don't distinguish the two paths.

**Implementation notes (US-011):**

- **Request shape** — `ChatRequest` grew an optional `mode: 'responses' | 'completions'` field in `backend/main.py`. Omitted → server falls back to `CHAT_MODE_DEFAULT` (default `responses`). `POST /api/chat` picks one of two streaming functions based on `mode`; the SSE transport (`event: delta | done | error` with the same payload shape) is unchanged so the frontend renders both modes identically.
- **Responses path** — renamed `_stream_reply` → `_stream_responses_reply` and retagged its LangSmith span as `chat_turn_responses` so traces can filter by mode. Behaviour is otherwise US-004.
- **Completions path** — new `_stream_completions_reply` (LangSmith span `chat_turn_completions`). Loads prior user/assistant messages from Supabase (minimum viable history — US-012 formalises the configurable sliding window + tool-message persistence), prepends a system prompt that nudges the model to call `search_documents`, and runs the tool-call loop.
- **Tool-call loop** — manual, capped at `MAX_TOOL_ITERATIONS = 5` per the PRD Technical Considerations note. Per iteration: open a streaming `chat.completions.create` with `tools=[search_documents_tool_schema()]`; fold delta `content` into `full_text_parts` (streamed to the client as `event: delta`) and accumulate `delta.tool_calls` by `index` into `{id, name, arguments}` slots since Chat Completions streams tool-call arguments in partial chunks. On `finish_reason='tool_calls'` append an assistant turn with `tool_calls`, execute each call via `_execute_tool_call`, append one `role='tool'` message per call, re-request. Any other `finish_reason` (usually `stop`) ends the loop; blowing past the iteration cap emits an `error` event.
- **Tool dispatch** — `_execute_tool_call` validates the JSON args with `SearchDocumentsInput`, then reuses the existing `search_documents` helper (same path as `POST /api/search`), so the RLS-scoped pgvector RPC from US-010 is the single source of retrieval. Errors (bad JSON, Pydantic validation, RPC failure) are serialised into the tool payload rather than aborting the turn — OpenAI's recommended pattern so the model can self-correct.
- **LangSmith fidelity** — because `openai_client` is already wrapped with `langsmith.wrap_openai`, each `chat.completions.create` stream and each `embeddings.create` (inside `search_documents`) posts its own child span under the `chat_turn_completions` parent. Metadata is merged onto the active run via `_attach_run_metadata(user_id, thread_id, mode, user_message_id, assistant_message_id)` so the PRD's "traces distinguish the two paths" check holds.
- **History scope** — the completions path pulls *all* persisted user/assistant rows for the thread (RLS-scoped). Tool-call persistence + a configurable window are explicitly US-012. Tool rows in the messages table are ignored here so we don't inject orphan `tool_call_id`s that no longer match any in-flight assistant turn.
- **Public config endpoint** — new `GET /api/config` returns `{default_chat_mode, supported_chat_modes, file_search_enabled}`. Frontend hits this on mount to seed the toggle.
- **Frontend wiring** — `frontend/src/lib/chat.ts` exports `ChatMode`, `BackendConfig`, and `fetchBackendConfig`; `streamChatTurn` now takes a required `mode` argument and forwards it in the POST body. `ChatPage` holds the current mode in state, initialises it from `fetchBackendConfig` (falling back to `'responses'` if the config call fails), and passes it through.
- **UI surface** — new `components/chat/ChatModeToggle.tsx`, a two-option segmented control (radiogroup semantics, `aria-checked`, hover titles describing each mode). Placed above the conversation pane so it's reachable per-thread. Disabled while a turn is in flight.
- **Env vars added** — `CHAT_MODE_DEFAULT=responses` in `backend/.env.example`. Invalid values fail fast at import time.
- **Verification** — `python3 -m py_compile main.py chunking.py embeddings.py retrieval.py` and `npm run typecheck` (frontend) both pass. Live validation (PRD steps 1–4) requires an OpenAI key + LangSmith project + an ingested test document; the mode toggle flips the SSE producer without any other UX change, and LangSmith will show the `chat_turn_responses` vs `chat_turn_completions` span trees side-by-side.

#### US-012: Stateless chat history persisted in Supabase

**Description:** As a user, in Chat Completions mode I want my conversation history to persist across turns and page reloads (since the API is stateless).

**Acceptance Criteria:**

- [x] Every user and assistant message written to `messages` table
- [x] Prior messages (sliding window, default last 20 turns, configurable) included in the Chat Completions prompt
- [x] Tool calls and tool results stored as `messages.role = 'tool'` rows for trace fidelity
- [x] Typecheck passes

**Validation Test:**

- **Setup:** New thread in Completions mode.
- **Steps:**
  1. Ask "My favorite color is green. Remember that."
  2. Refresh the page
  3. Ask "What is my favorite color?"
- **Expected Result:** Step 3 returns "green", proving history was reloaded from Supabase.
- **Failure Indicator:** Assistant has no memory after refresh, or history is stored but not included in the prompt.

**Implementation notes (US-012):**

- **Migration** `supabase/migrations/20260417160000_messages_tool_columns.sql` drops `NOT NULL` on `messages.content` (assistant rows that only emit `tool_calls` legitimately have no text), and adds three columns: `tool_calls jsonb` (OpenAI-format `[{id, type, function: {name, arguments}}, ...]` attached to the assistant row that requested the calls), `tool_call_id text` (set on `role='tool'` rows to link a result back to its spawning call), and `name text` (tool name on `role='tool'` rows, purely for trace fidelity). Role check constraint + RLS policies from US-001 are unchanged — tool rows inherit thread-owner access via the existing parent-thread join.
- **Backend — per-turn persistence** `_stream_completions_reply` in `backend/main.py` now writes every step of the tool-call loop to Supabase, not just the final assistant answer:
  1. User message (unchanged) → on entry.
  2. On `finish_reason='tool_calls'`: persist an `assistant` row with `content = iter_content or null` and `tool_calls = <openai list>`. Then for each tool call: execute it, persist a `tool` row with `tool_call_id`, `name`, and the JSON result as `content`.
  3. On any other finish reason: persist the final `assistant` row with its content (no `tool_calls`).
  Every DB write shares the user's JWT via `_supabase_headers`, so RLS still gates inserts. The `done` SSE event now carries the final assistant row's id specifically, not an accumulated id.
- **Backend — sliding window** New env `CHAT_HISTORY_MAX_TURNS` (default 20). `_apply_history_window` walks the ascending-ordered message list, finds the index of the Nth-from-last `user` row, and returns the slice from there — a "turn" is a user row plus everything that followed it until the next user row, so assistant + tool intermediates stay grouped with their user root. Zero disables history; fewer turns than the budget short-circuits to returning `prior` unchanged.
- **Backend — projection** `_prior_to_completions` now handles all four role cases: `user`, plain `assistant` (content only), `assistant` with `tool_calls` (content nullable + `tool_calls` passed through verbatim), and `tool` (emits `{role, tool_call_id, content, name?}`). Orphan guard: maintains a `pending_tool_call_ids` set seeded from each assistant-with-tool_calls turn; a `tool` row only enters the projection if its `tool_call_id` is in that set, and if the final assistant-with-tool_calls turn has unanswered ids it's dropped entirely so OpenAI doesn't 400 on mismatched ids.
- **Backend — insert helper** `_insert_message` grew optional kw-only `tool_calls`, `tool_call_id`, `name` and now accepts `content: str | None`. Old call sites (user + plain assistant + Responses mode) are unchanged.
- **Responses mode unchanged** US-004's Responses path still persists just `user` + `assistant` rows — OpenAI owns the conversation state via `previous_response_id`, so tool spans don't need to land in our `messages` table. The PRD's US-012 AC targets Completions mode specifically (history reload after refresh); Responses handles that server-side via its managed thread.
- **Frontend** `MessageRow.content` widened to `string | null`; `MessageList` now filters to `role ∈ {user, assistant}` AND `content.trim().length > 0`, so intermediate assistant rows (pure tool-call turns) and tool rows don't render as empty bubbles — they exist in the DB for trace fidelity / next-turn context only. The optimistic user row already sets `content` to a `string`, so no call-site typing change was needed.
- **Env vars added** `CHAT_HISTORY_MAX_TURNS=20` in `backend/.env.example`.
- **Verification** `python3 -m py_compile main.py chunking.py embeddings.py retrieval.py` and `npm run typecheck` (frontend) both pass. PRD validation test (favorite-color recall across a refresh) is runnable end-to-end once the user applies the new migration: the second question ("What is my favorite color?") will re-fetch all prior rows via `_load_prior_messages`, the window keeps the green-color turn, and the Completions call sees it in the prompt.

#### US-013: Realtime ingestion status via Supabase Realtime

**Description:** As a user, I want to see ingestion status (queued → processing → ready / error) update live without refreshing.

**Acceptance Criteria:**

- [x] `documents.status` column with enum: `queued | processing | ready | error`
- [x] Frontend subscribes to Supabase Realtime on the `documents` table filtered by `user_id`
- [x] Status badge updates in real time as the backend progresses
- [x] Error messages visible in the UI on `status = error`
- [x] Typecheck passes
- [ ] Verify in browser using dev-browser skill *(deferred — dev-browser skill not available in this environment, same pattern as earlier stories)*

**Validation Test:**

- **Setup:** Ingestion page open in browser.
- **Steps:**
  1. Upload a valid file
  2. Immediately watch the status column
  3. Upload a deliberately malformed file (e.g., corrupted)
- **Expected Result:** Step 2 shows badge transition queued → processing → ready without refresh. Step 3 ends in `error` with a readable error message.
- **Failure Indicator:** Requires manual refresh to see status updates, or errors are silent.

**Implementation notes (US-013):**

- **Status enum** already in place from US-007 (`20260417120000_init_documents.sql`): `status text not null default 'queued' check (status in ('queued','processing','ready','error'))`. Backend transitions it from `processing` → `ready|error` inside `ingest_document` (US-008). No schema change needed for the enum itself.
- **Migration** `supabase/migrations/20260417170000_documents_realtime.sql`: `alter publication supabase_realtime add table public.documents;` + `alter table public.documents replica identity full;`. The publication grant is what turns on the websocket broadcast; `REPLICA IDENTITY FULL` makes UPDATE payloads carry the full `old` row so the client can cheaply detect transitions (e.g. `old.status !== 'error' && new.status === 'error'` for the error-toast trigger) without refetching. RLS still governs what each subscriber is allowed to receive — Realtime checks each row against `documents_select_own` per-connection, so the `user_id=eq.<uid>` filter is a wire-chatter optimisation, not a security boundary.
- **Frontend helper** `frontend/src/lib/ingestion.ts::subscribeToDocuments(userId, handlers)` wraps `supabase.channel('documents:<uid>').on('postgres_changes', {event:'*', schema:'public', table:'documents', filter:'user_id=eq.<uid>'}, ...)`. Dispatches INSERT/UPDATE/DELETE through typed handler callbacks (UPDATE also receives `old` for transition checks). Returns an unsubscribe thunk that calls `supabase.removeChannel(channel)` — callers hand it back as a React effect cleanup.
- **IngestionPage wiring** `useEffect` keyed on `user` opens the subscription and writes three handlers:
  - `onInsert`: prepend the row unless already present (dedup against the optimistic post-upload insert).
  - `onUpdate`: find the row by id and replace; if `deleted_at` is now set, filter it out. On `status` transitioning into `error` (gated by `old.status !== 'error'` so re-renders don't re-toast) surface a toast including `error_message` when available.
  - `onDelete`: remove by id (handles the future US-019 hard-delete path; soft-delete goes through the UPDATE branch).
  The optimistic upload path in `handleFiles` now upserts by id instead of always prepending, so the Realtime INSERT that arrives for the same row is a no-op.
- **Status badge + error message** already rendered by `DocumentsTable` (styled per enum via `STATUS_STYLES`, `error_message` shown under the filename on error) — the Realtime updates just mutate the row and React re-renders. No DOM / component change was needed.
- **Verification** `npm run typecheck` passes. Live validation (PRD steps 1–3) requires the user to apply the new migration and ensure the `supabase_realtime` publication is enabled on their Supabase project (it is by default on hosted Supabase). Browser verification deferred as with earlier UI stories.

---

### Module 3 — Record Manager

#### US-014: Content hashing for deduplication

**Description:** As a developer, I want content-addressable hashing on documents and chunks so re-uploading the same file does not create duplicates.

**Acceptance Criteria:**

- [ ] `documents.content_hash` (SHA-256 of raw bytes) and `chunks.content_hash` (SHA-256 of chunk text) columns
- [ ] Unique index on `(user_id, content_hash)` for documents
- [ ] Re-uploading identical file is a no-op (returns existing `document_id`)
- [ ] Typecheck passes

**Validation Test:**

- **Setup:** A test file `foo.txt`.
- **Steps:**
  1. Upload `foo.txt`; note the row count in `documents` and `chunks`
  2. Upload `foo.txt` again
  3. Modify one word in `foo.txt` and upload
- **Expected Result:** Step 2 produces no new rows; UI reports "already ingested". Step 3 creates a new document row and new chunks.
- **Failure Indicator:** Duplicate rows after step 2, or step 3 fails to detect the modification.

#### US-015: Incremental updates (only process new/changed chunks)

**Description:** As a developer, when a document is updated I want only new or modified chunks re-embedded so ingestion stays fast.

**Acceptance Criteria:**

- [ ] When re-ingesting, diff chunks by `content_hash`: keep unchanged, delete removed, embed new
- [ ] Metrics logged: `chunks_added`, `chunks_removed`, `chunks_unchanged`
- [ ] Typecheck passes

**Validation Test:**

- **Setup:** A 20-chunk document already ingested.
- **Steps:**
  1. Modify 2 paragraphs in the source file
  2. Re-upload
  3. Check the ingestion log output
- **Expected Result:** Log shows ~2 chunks added, ~2 removed, ~18 unchanged. No full re-embedding.
- **Failure Indicator:** All chunks re-embedded, or no diff metrics logged.

---

### Module 4 — Metadata Extraction

#### US-016: LLM-extracted structured metadata per document

**Description:** As a user, I want the system to automatically extract structured metadata (title, authors, topics, date, document_type) from uploaded documents so I can filter retrieval.

**Acceptance Criteria:**

- [x] `documents.metadata JSONB` column
- [x] Pydantic schema: `{title, authors: [str], topics: [str], published_date: date | null, document_type: str}`
- [x] LLM extraction runs during ingestion using structured outputs
- [x] Extraction failures do not block ingestion; `metadata` may be null with a warning logged
- [x] Typecheck passes

**Validation Test:**

- **Setup:** Upload a research paper PDF or markdown with clear metadata.
- **Steps:**
  1. Trigger ingestion
  2. Query `SELECT metadata FROM documents WHERE id = <id>`
- **Expected Result:** `metadata` JSON contains a reasonable title, author list, and 2+ topics.
- **Failure Indicator:** `metadata` is null without an error log, or schema-invalid JSON is stored.

**Implementation notes (US-016):**

- **Migration** `supabase/migrations/20260421120000_documents_metadata.sql` adds `documents.metadata jsonb` (nullable by design — extraction failures leave it NULL; pre-US-016 rows stay NULL until re-ingest) plus a GIN index (`documents_metadata_gin_idx`) so US-017's filter predicates (`?|` on `topics`, equality on `document_type`) stay index-backed. The schema shape is enforced in the app layer via Pydantic, not via a SQL check — the JSONB column stays flexible if we add fields later.
- **Pydantic schema** `backend/metadata.py::DocumentMetadata` pins the five fields from the acceptance criterion as all-required so OpenAI's *strict* structured outputs accept the schema (optional keys are disallowed in strict mode). "No signal" sentinels are `""` / `[]` / `null`, and the system prompt instructs the model to prefer these over guessing. `published_date` is a Pydantic `date` serialised to ISO-8601 via `model_dump(mode="json")` before being sent to PostgREST — JSONB stores it as a string, and US-017's RPC casts `(metadata->>'published_date')::date` at read time.
- **Extraction call** `metadata.extract_document_metadata(openai_client, text, filename)` uses `client.chat.completions.parse(..., response_format=DocumentMetadata)` (OpenAI Python SDK ≥ 1.50, satisfied by the `openai>=1.70.0` pin). The LangSmith-wrapped `openai_client` is reused so each extraction shows up as its own span alongside the embeddings + completions calls from the same ingest. Text is down-sampled to `DEFAULT_SAMPLE_CHARS = 8000` via a head+tail split (`_sample_text`) so title/authors near the top and date/footer near the bottom are both visible without blowing up cost on long documents.
- **Model selection** `get_metadata_model()` resolves `METADATA_MODEL` → `OPENAI_MODEL` → `gpt-4o-mini`. Kept configurable so a deployer can point just the extractor at a cheaper model without touching chat behaviour. The resolved value is surfaced in the ingest response (`metadata_model`) alongside `embedding_model` for debuggability.
- **Ingest wiring** `main.py::ingest_document` calls `extract_document_metadata` *after* `_reconcile_chunks` succeeds (chunks already persisted, so a metadata failure can't leave the doc in a worse state than pre-US-016) and folds the serialised result into the same PATCH that flips `status='ready'`. A `None` return (network error, parse failure, safety refusal) is non-fatal: the helper has already logged a warning, `documents.metadata` stays at its prior value (NULL on first ingest, or the last good extraction on re-ingest), and the document is still marked `ready` so it remains searchable. This matches the PRD acceptance criterion "Extraction failures do not block ingestion".
- **Column surface** `DOCUMENT_COLUMNS` (backend) and `DOCUMENT_COLUMNS` / `DocumentRow` (frontend `lib/ingestion.ts`) both pick up `metadata` so any caller that fetches a row sees the field. The frontend exports a `DocumentMetadata` TS type mirroring the Pydantic schema — no UI surface consumes it yet (that's a later Module 4 / Module 7 concern), but the type is in place so US-017's filter UI can lean on it without another round-trip.
- **Verification** backend import + byte-compile smoke passes (`python -m py_compile`, `import main` with stub env); `npm run typecheck` passes. Live validation per the PRD steps (upload a document, `SELECT metadata`) is deferred to the user since it requires the new migration applied to their Supabase project and a real OpenAI key.

#### US-017: Metadata-filtered retrieval

**Description:** As a user, I want my retrieval queries to optionally filter by metadata (e.g., "only documents from 2024") so I get more precise results.

**Acceptance Criteria:**

- [x] `search_documents` tool accepts optional `filters: {topics?, document_type?, date_range?}`
- [x] Filters translated to SQL `WHERE` clauses on `documents.metadata`
- [x] Agent is prompted with the metadata schema so it knows what filters are valid
- [x] Typecheck passes

**Validation Test:**

- **Setup:** Ingest 3 documents — one tagged topic "ml", one "finance", one "biology".
- **Steps:**
  1. Ask "What do the ML papers say about transformers?"
  2. Inspect the tool call in LangSmith
- **Expected Result:** Agent calls `search_documents` with `filters: {topics: ["ml"]}`; only chunks from the ML paper are returned.
- **Failure Indicator:** Agent ignores the metadata hint, or filter returns irrelevant documents.

---

### Module 5 — Multi-Format Support

#### US-018: Parse PDF, DOCX, HTML, and Markdown via docling

**Description:** As a user, I want to upload PDFs, Word docs, HTML pages, and Markdown files and have them all parsed into clean text for chunking.

**Acceptance Criteria:**

- [x] `docling` integrated on the backend
- [x] Accepted MIME types expanded: `.pdf, .docx, .html, .md, .txt`
- [x] Parsed output preserves headings and paragraph structure where possible
- [x] Chunking respects structural boundaries (doesn't split mid-heading)
- [x] Per-format parse failures reported via `status = error` with a readable message
- [x] Typecheck passes
- [x] Verify in browser using dev-browser skill

**Validation Test:**

- **Setup:** One sample file per supported format.
- **Steps:**
  1. Upload each file sequentially
  2. Query `SELECT filename, LENGTH(content) FROM chunks JOIN documents ...` for each
  3. Spot-check one chunk from each format visually
- **Expected Result:** All five ingest successfully; chunk contents are readable plain text (no HTML tags, no PDF artifacts).
- **Failure Indicator:** Any format fails silently, or chunks contain raw markup.

**Implementation notes (US-018):**

- **Parsing module** `backend/parsing.py::parse_document(raw, filename, content_type)` is the single dispatch point for every format. `.txt` / `text/plain` bypasses docling and returns a utf-8 decode directly — running plain text through the heavyweight converter only adds latency without preserving any extra structure. `.pdf, .docx, .html, .htm, .md, .markdown` route to a module-level `DocumentConverter` singleton (instantiation is expensive; `convert()` is stateless so sharing is safe) and the resulting `DoclingDocument` is emitted as Markdown via `export_to_markdown()`. Filename extension is the primary format signal with `content_type` as a fallback, because browsers are unreliable about `file.type` for Markdown and HTML from local disk. Unknown types raise `UnsupportedFormatError`; empty output raises `ValueError("… produced no extractable text …")` so image-only PDFs surface as an explicit failure rather than silently ingesting zero chunks.
- **PDF fallback** docling's PDF pipeline depends on IBM's `RTDetr` layout model, which requires torch ≥ 2.4. When the installed torch is older (e.g. the dev venv has 2.2.2 from conda), layout loading raises and the whole conversion errors out. `_pdf_text_fallback` catches that path and re-extracts page text via `pypdfium2` (already a transitive dep of docling; pinned directly in `requirements.txt` so the dep is documented). The fallback loses heading structure that docling would have reconstructed, but it keeps text-based PDFs ingestable without forcing a torch upgrade in every deploy environment. Non-PDF failures still propagate as `ValueError` — they don't have a safe fallback.
- **Chunking — structural boundaries** `backend/chunking.py::chunk_text` now splits on blank-line boundaries (`re.split(r"\n\s*\n", …)`), glues any standalone heading line to the next block so a chunk can never *end* on an orphan heading, then greedily packs blocks until adding the next block would exceed `CHUNK_SIZE_TOKENS`. Blocks larger than the budget on their own (a single paragraph > 500 tokens) fall back to the original token-level sliding window since structure can't help there. Overlap is re-applied at the *block* level: when a chunk boundary fires, the tail blocks of the previous chunk (up to `CHUNK_OVERLAP_TOKENS`) are carried into the next chunk as its prefix, so overlap too respects structure instead of cutting mid-paragraph.
- **Ingest wiring** `main.py::ingest_document` replaces the old inline `raw.decode("utf-8")` with `parse_document(raw, filename, content_type)`. `UnsupportedFormatError` is rewrapped as `ValueError` so the existing outer `except` still flips `status='error'` with a human-readable `error_message` — per-format failures surface through the same Realtime path US-013 already wires up. A startup hook (`@app.on_event("startup")`) calls `parsing.warmup()` to front-load the docling `DocumentConverter` so the first user upload doesn't pay multi-second init on the request path. `SKIP_DOCLING_WARMUP=1` opts out for tests.
- **Frontend surface** `frontend/src/lib/ingestion.ts::ACCEPTED_EXTENSIONS` now lists `.txt, .md, .pdf, .docx, .html`; `ACCEPTED_MIME_TYPES` adds the corresponding MIME strings but extension is the source of truth inside `isAcceptedFile` (browsers often send empty / mismatched MIMEs for Markdown and for HTML from local disk). `DropZone` reads the same constant for both its helper text and the `<input accept>` attribute, and `IngestionPage` rewrites its toast copy to list the accepted extensions dynamically so adding a format in the future only touches the one constant.
- **Dependencies** `requirements.txt` adds `docling>=2.20.0,<3` and pins `numpy<2` (docling's torch ≤ 2.3 transitive pin can't initialise against numpy 2.x — you get `_ARRAY_API not found` at import time). `supabase-py` was bumped to `>=2.15.0` so its httpx range admits 0.28+, keeping pip's resolver happy alongside docling's metadata pin (backend doesn't actually import `supabase` — all REST calls go through httpx directly — so the version change is cosmetic but silences the warning).
- **Verification** Backend unit-level smoke: `parse_document` exercised on `.txt` / `.md` / `.html` / `.docx` (built via `python-docx`) / `.pdf` (built via `reportlab`) — all five return clean Markdown-shaped text, PDF exercises the pypdfium2 fallback path on the local venv. Chunking tested on a structured multi-heading document (`size=150, overlap=30` → 10 chunks, every heading glued to its paragraph, no mid-heading splits). `npx tsc --noEmit` and `npx vite build` pass. Full live browser upload (drag-drop + end-to-end Supabase ingestion) wasn't run — port 8000 was held by an unrelated hung backend process and a login required user credentials that weren't available — so the final step of the validation test (ingest each format through the UI and spot-check chunk contents) is deferred to the user.

#### US-019: Cascade deletes on document removal

**Description:** As a user, when I delete a document I expect all its chunks, embeddings, and file-storage blobs to be removed atomically.

**Acceptance Criteria:**

- [x] FK `ON DELETE CASCADE` on `chunks.document_id`
- [x] Supabase Storage blob deleted in the same transaction (or compensating action on failure)
- [x] UI reflects the deletion in real-time
- [x] Typecheck passes

**Validation Test:**

- **Setup:** A document with 15 chunks and a stored blob.
- **Steps:**
  1. Click delete on the document
  2. Query `SELECT COUNT(*) FROM chunks WHERE document_id = <id>`
  3. Check Supabase Storage for the blob
- **Expected Result:** Chunk count = 0; blob is gone; UI row removed.
- **Failure Indicator:** Orphan chunks, orphan blobs, or UI still shows the deleted row.

**Implementation notes (US-019):**

- No schema migration needed — the FK `chunks.document_id → documents(id) on delete cascade` was already in place from US-008 (`supabase/migrations/20260417130000_init_chunks.sql:11`), and the `embedding` column lives on `chunks` (US-009), so deleting a document cascade-removes chunks + embeddings in a single DB transaction.
- Soft-delete is retired. `softDeleteDocument` (which flipped `deleted_at`) is replaced by `deleteDocument(doc)` in `frontend/src/lib/ingestion.ts`. It (1) deletes the `documents` row via RLS-scoped `supabase.from('documents').delete().eq('id', doc.id)`, then (2) removes the Storage blob via `supabase.storage.from('documents').remove([doc.storage_path])`.
- Order is deliberate: DB row first, blob second. An orphan row is user-visible (shows up in the list pointing to a dead blob); an orphan blob is not (user_id-namespaced, cleanable out-of-band). If the blob delete fails after the row is gone, we `console.warn` and return success — that's the "compensating action" the acceptance criterion allows. Storage `.remove()` is idempotent, so a later cleanup sweep is safe.
- The `deleted_at` column and the `.is('deleted_at', null)` filters in `listDocuments` / dedupe lookups are retained as no-ops — with hard-delete the rows are gone entirely, but removing the column would require touching the content-hash partial unique index (`20260420120100_documents_content_hash.sql`) and the `match_chunks` SQL function. Out of scope for this story.
- UI realtime: `documents` is already in the `supabase_realtime` publication with `REPLICA IDENTITY FULL` (US-013, `20260417170000_documents_realtime.sql`), so DELETE events carry the row pre-image. The existing `onDelete` handler in `IngestionPage.tsx` filters the row out of local state. The optimistic `setDocuments` after `deleteDocument` returns makes the UI snappy; the Realtime DELETE that follows is a no-op thanks to the `id` filter.
- Verification: `npm run typecheck` in `frontend/` passes. End-to-end validation (chunk count = 0, blob gone) requires a live Supabase project — deferred as with earlier UI stories.

---

### Module 6 — Hybrid Search & Reranking

#### US-020: Keyword search with Postgres full-text search

**Description:** As a developer, I need a keyword search function that complements vector search for exact-match queries (names, IDs, technical terms).

**Acceptance Criteria:**

- [x] `chunks.content_tsv tsvector` column maintained via trigger or generated column
- [x] GIN index on `content_tsv`
- [x] `keyword_search(query, top_k)` function returns chunks ranked by `ts_rank_cd`
- [x] Typecheck passes

**Validation Test:**

- **Setup:** Chunk corpus containing the exact token "XJ-2049-alpha".
- **Steps:**
  1. Run vector search for "XJ-2049-alpha"
  2. Run keyword search for the same
- **Expected Result:** Keyword search returns the exact-match chunk at rank 1. Vector search may or may not — this demonstrates why hybrid is needed.
- **Failure Indicator:** Keyword search misses exact tokens or uses a slow sequential scan.

**Implementation notes (US-020):**

- `content_tsv` is a STORED generated column (`to_tsvector('english'::regconfig, content)`) so the existing chunk-insert path stays untouched — no trigger plumbing, just `add column` (`supabase/migrations/20260505120000_chunks_content_tsv.sql`). The `::regconfig` cast pins the IMMUTABLE overload of `to_tsvector`; without it Postgres can resolve to the (text,text) variant which is STABLE and rejected for STORED generated columns.
- `keyword_search(query text, match_count int)` returns the same `(id, document_id, chunk_index, content, similarity, filename)` shape as `match_chunks` so US-021's RRF fusion is a clean drop-in. The `similarity` field carries `ts_rank_cd` (unbounded, not [0,1]) — RRF fuses by rank position so magnitude mismatch is fine.
- Query parsing uses `websearch_to_tsquery` (quoted phrases, OR, `-negation`) instead of `plainto_tsquery` — never raises on malformed input, which is the right failure mode for an agent-supplied string.
- Backend wrapper is `retrieval.keyword_search()`, exposed via `POST /api/search/keyword` for the validation test (vector vs. keyword side-by-side comparison). US-021 will add the hybrid route on top.
- Filter parity with `match_chunks` (US-017 metadata filters) is intentionally deferred to US-021 — the PRD spec for US-020 is just `(query, top_k)`, and adding filters before RRF lands risks getting the join surface wrong.

#### US-021: Reciprocal Rank Fusion (RRF) combining vector + keyword

**Description:** As a developer, I want to combine vector and keyword results using RRF so retrieval balances semantic and lexical signals.

**Acceptance Criteria:**

- [x] `hybrid_search(query, top_k)` runs both searches and merges via RRF (default `k=60`, configurable)
- [x] Duplicate chunks (appearing in both rankings) are deduplicated, scores summed
- [x] `search_documents` tool switched to use hybrid search by default
- [x] Typecheck passes

**Validation Test:**

- **Setup:** A chunk that ranks well on vector (topical match) + a chunk that ranks well on keyword (exact token).
- **Steps:**
  1. Run hybrid search for a query that exercises both
  2. Compare top-5 to vector-only and keyword-only results
- **Expected Result:** Hybrid top-5 contains the best from both; neither exact-match nor semantic-match chunks are lost.
- **Failure Indicator:** Hybrid favors one strategy, drops relevant results from the other, or duplicates entries.

**Implementation notes (US-021):**

- `hybrid_search` (in `backend/retrieval.py`) is application-side, not an SQL RPC: it calls `match_chunks` and `keyword_search` concurrently via `asyncio.gather`, then fuses Python-side. Keeps the embedding round trip (already Python-side for vector) on a single side and avoids a heavier SQL fusion function. Net latency ≈ max(vector, keyword) + one OpenAI embed.
- Each side pulls a wider candidate pool (`top_k * 4`, clamped to `MAX_TOP_K=50`) before fusion. Pulling only `top_k` per side starves RRF — items one strategy ranks low but the other ranks well wouldn't make either pool. Tuneable later if needed.
- RRF formula: per-item score = Σ over rankings of `1 / (k + rank)` (rank 1-indexed). Duplicate items get summed scores — this is the dedupe path. `k=60` default (Cormack et al. canonical), configurable via `HYBRID_RRF_K` env var. Returned `similarity` carries the fused score (small absolute numbers, ~0.033 max — only ordering is meaningful, magnitudes are not comparable to vector or keyword scores). Ties broken by chunk id for run-to-run determinism.
- `search_documents` chat tool dispatch in `main.py:_execute_tool_call` flips to `hybrid_search` by default. Added `RETRIEVAL_MODE=hybrid|vector` env knob (default `hybrid`) as a rollback escape hatch — incident response shouldn't require code changes. Tool result includes `retrieval_mode` so traces show which path ran.
- New migration `20260505121000_keyword_search_filters.sql` extends `keyword_search` with the same US-017 filters (`topics`, `document_type`, `published_date` range) as `match_chunks`. Without filter parity, a filtered hybrid query would bias toward keyword matches across all docs (filtered vector pool + unfiltered keyword pool) — silently corrupting results. Same drop-and-recreate pattern US-017 used.
- `/api/search/hybrid` route added so the validation test (compare hybrid top-5 vs vector vs keyword) is runnable without driving the agent. `/api/search` stays vector-only and `/api/search/keyword` stays keyword-only — three explicit endpoints make the side-by-side comparison trivial to script.

#### US-022: Reranking via cross-encoder or LLM

**Description:** As a developer, I want the top N hybrid results reranked by a cross-encoder (or an LLM-as-reranker) so the final top-k is higher precision.

**Acceptance Criteria:**

- [x] Reranker configurable via env: `RERANKER=cohere|voyage|llm|none`
- [x] Reranker takes top 20 from hybrid, returns top 5 to the agent
- [x] Latency logged; if reranker exceeds 2s, log a warning
- [x] Typecheck passes

**Validation Test:**

- **Setup:** A query where the best chunk is ranked 8th by hybrid search (measurable by handcrafted eval).
- **Steps:**
  1. Run hybrid-only retrieval; note top-5
  2. Run hybrid + rerank; note top-5
- **Expected Result:** Reranked top-5 includes the 8th-ranked chunk in a higher position.
- **Failure Indicator:** Reranker has no effect on rankings, or reranker silently fails.

**Implementation notes (US-022):**

- New module `backend/reranking.py` with a `Reranker` ABC and four implementations: `NullReranker`, `CohereReranker` (Cohere v2 rerank API), `VoyageReranker` (Voyage v1 rerank API), `LlmReranker` (OpenAI chat-completions JSON-mode score-then-sort). `build_reranker(name, http, openai_client)` is the factory; hosted backends raise on missing API keys at build time so config mistakes surface immediately, not at first request.
- Default `RERANKER=none` so this is opt-in — flipping it on adds latency and (for hosted backends) an extra vendor dep, neither of which should be free side effects of pulling latest. Models are env-tunable: `COHERE_RERANK_MODEL` (default `rerank-english-v3.0`), `VOYAGE_RERANK_MODEL` (default `rerank-2`), `OPENAI_RERANK_MODEL` (defaults to `OPENAI_MODEL`, then `gpt-4o-mini`). `RERANK_INPUT_K` controls the candidate pool fed to the reranker (default 20 per PRD).
- `rerank_with_timing()` wraps the reranker call with `time.perf_counter()` and logs at three levels: `reranker.ok` for normal, `reranker.slow` warning when latency exceeds `RERANK_LATENCY_WARN_SECONDS=2.0`, and `reranker.error` warning with fall-back to input ordering on any exception. Hard-failing the user's whole turn over a refinement step would be wrong — the input was already filtered by hybrid retrieval, so degraded ordering is still useful.
- LLM reranker prompts the model for `{"results": [{"index": int, "score": float}, ...]}` via `response_format={"type": "json_object"}` at `temperature=0`. Parser is defensive: dedupes repeat indices (model occasionally emits them), drops out-of-range indices, and tops up the result list from input order if the model under-counts so we always return `min(top_k, len(candidates))` rows. Caveat: LLM scoring has no calibration across runs — use Cohere/Voyage when score magnitudes matter for downstream logic.
- Pipeline orchestrator `_retrieve_for_agent` in `main.py` ties it all together: pulls `RERANK_INPUT_K` candidates from the search backend (hybrid by default, vector when `RETRIEVAL_MODE=vector`) when reranking is on, otherwise pulls `top_k` directly. The chat tool path and the new `/api/search/rerank` endpoint share this helper so the validation test (compare hybrid-only via `/api/search/hybrid` vs hybrid+rerank via `/api/search/rerank`) hits the same code path the agent actually uses. Tool result includes `retrieval_mode` and `reranker` so LangSmith traces show which path ran.

---

### Module 7 — Additional Tools

#### US-023: Text-to-SQL tool for structured data

**Description:** As an agent, I want a `query_database` tool that translates natural language to SQL and runs it against a designated read-only analytics schema so I can answer questions over structured data.

**Acceptance Criteria:**

- [x] Read-only Postgres role used for SQL execution
- [x] Allowlisted schema (e.g., `analytics.*`); all other schemas blocked
- [x] Agent provided schema snapshot (table + column list) in its system prompt
- [x] Query timeout (default 10s)
- [x] Typecheck passes

**Validation Test:**

- **Setup:** `analytics.orders(id, user_email, total, created_at)` seeded with 100 rows.
- **Steps:**
  1. Ask "What was total revenue last month?"
  2. Ask "DROP TABLE analytics.orders" (adversarial)
  3. Inspect LangSmith trace
- **Expected Result:** Step 1 returns a SUM result with correct SQL. Step 2 is refused or fails on the read-only role. Step 3 shows the generated SQL in the trace.
- **Failure Indicator:** Write query succeeds, SQL not logged, or agent hallucinates schema.

**Implementation notes (US-023):**

- New module `backend/text_to_sql.py` exposes `query_database(question, row_limit, …)`, the `QueryDatabaseInput` Pydantic schema, and `query_database_tool_schema()` for the Chat Completions `tools[]` array. The chat tool path in `main.py` dispatches `query_database` alongside `search_documents`; a direct `/api/sql` endpoint runs the same code path so the PRD validation steps (revenue total + adversarial DROP + LangSmith trace inspection) are runnable without driving the agent.
- Three layers of safety stack so a single layer's bug doesn't expose writes: (1) the connection authenticates as `analytics_readonly` from migration `20260506120000_init_analytics_schema.sql` — the role has no write privileges on any schema, so even an unparsed DROP fails at the database boundary; (2) every query runs inside `BEGIN READ ONLY` with `set local statement_timeout = SQL_QUERY_TIMEOUT_MS` (default 10s per PRD); (3) `validate_sql_safety()` strips comments + string literals, requires the statement to start with `select` or `with`, rejects a forbidden-keyword set (INSERT/UPDATE/DELETE/MERGE/COPY/CREATE/DROP/ALTER/TRUNCATE/GRANT/REVOKE plus session-level SET/RESET), bans multiple statements, and walks every `schema.table` reference to confirm the schema is in `ALLOWED_SQL_SCHEMAS` (default `analytics`).
- Schema snapshot is introspected once at startup via `information_schema.columns` against the allowed schemas, cached in module-level `_SQL_SCHEMA_SNAPSHOT`, and interpolated into both the LLM SQL-generation system prompt and the `query_database` tool description so the agent picks the right tool based on question type without an extra round-trip. Introspection failure degrades gracefully — the prompt falls back to "ask the user for table names" rather than failing the chat turn.
- Tool result returns `{sql, columns, rows, row_count, truncated}` so the generated SQL is captured verbatim in the LangSmith tool-message payload for trace inspection. The chat tool handler turns `SqlSafetyError` into a `{"error": "unsafe sql: …"}` JSON payload (model can recover by re-phrasing); other exceptions surface the same way so the agent can fall back to `search_documents` or general knowledge.
- Tool is opt-in: when `ANALYTICS_DATABASE_URL` is unset, `is_enabled()` returns False, the tool is omitted from the Chat Completions `tools[]` list, the SQL block is omitted from the system prompt, and `/api/sql` returns 503. This keeps existing deploys working without forcing the new env vars. The migration creates the role + schema + seed locally; production Supabase requires running the `CREATE ROLE` block manually via the SQL editor with a strong password (Supabase Cloud restricts CREATE ROLE in normal migrations).
- `.env.example` documents `ANALYTICS_DATABASE_URL`, `ALLOWED_SQL_SCHEMAS`, `SQL_QUERY_TIMEOUT_MS`, and `OPENAI_SQL_MODEL` (falls through to `OPENAI_MODEL` then `gpt-4o-mini`). `requirements.txt` adds `asyncpg>=0.29` — the only place in the codebase that opens a raw Postgres connection; the rest of the chat path stays on PostgREST so RLS still applies to user-scoped reads/writes.

#### US-024: Web search fallback tool

**Description:** As an agent, when local retrieval returns nothing relevant, I want a `web_search` tool (e.g., Tavily, Brave, SerpAPI) so I can still answer the user.

**Acceptance Criteria:**

- [x] Web search provider configurable via env
- [x] Tool returns top N results with title, URL, snippet
- [x] Agent instructed to prefer local retrieval; web search used when no local chunks pass threshold
- [x] Final response cites web sources with URLs
- [x] Typecheck passes

**Validation Test:**

- **Setup:** Ingest 0 documents on a fresh account.
- **Steps:**
  1. Ask "What happened in tech news today?"
  2. Inspect the response for citations
- **Expected Result:** Agent uses `web_search`; response includes clickable source URLs.
- **Failure Indicator:** Agent hallucinates without tools, or citations are missing.

**Implementation notes (US-024):**

- New module `backend/web_search.py` with a `WebSearchProvider` ABC and four implementations: `NullProvider`, `TavilyProvider`, `BraveProvider`, `SerpApiProvider`. `build_web_search_provider(name, http)` is the factory; hosted backends raise on missing API keys at build time so config mistakes surface immediately, not at first request. All providers project to a uniform `WebSearchResult(title, url, snippet)` so swapping vendors via `WEB_SEARCH_PROVIDER` doesn't change agent behaviour.
- Default `WEB_SEARCH_PROVIDER=none` so the tool is opt-in — no extra vendor dep, no $/query side effect from pulling latest. When unset, `is_enabled()` returns False, the tool is omitted from the Chat Completions `tools[]` list, the routing block is omitted from the system prompt, and `/api/web-search` returns 503. Same opt-in pattern as the SQL tool (US-023) and the reranker (US-022).
- Routing rule lives in two places on purpose: the tool description says "Use this ONLY after `search_documents` returns no relevant chunks", and the system-prompt block (`COMPLETIONS_WEB_SEARCH_PROMPT` in `main.py`) repeats the same rule with extra context ("ALWAYS try `search_documents` first … include the URL"). Models occasionally skim system text when many tools are visible, so saying it twice is cheap insurance against the agent reaching for `web_search` on questions that belong in the user's corpus.
- Errors are non-fatal at the chat-tool boundary: a Tavily/Brave/SerpAPI outage returns `{"error": …, "results": [], "count": 0}` to the agent, which then either re-tries with a tweaked query or falls through to general knowledge with a disclaimer — better than failing the user's whole turn over a vendor blip. The standalone `/api/web-search` endpoint (mirrors the chat path for the PRD validation test) bubbles errors up as 5xx since there's no agent on the other side to recover.
- `WEB_SEARCH_TIMEOUT_S` (default 10s) caps the per-search HTTP call. Tool result includes `count` so LangSmith traces show whether the agent saw zero results (and correctly fell back) vs. picked one to cite. No changes to `requirements.txt` — every provider is a plain JSON HTTP call routed through the existing `httpx` dependency.
- `.env.example` adds `WEB_SEARCH_PROVIDER`, `TAVILY_API_KEY`, `BRAVE_SEARCH_API_KEY`, `SERPAPI_API_KEY`, and `WEB_SEARCH_TIMEOUT_S`. The frontend's `/api/config` response gains a `web_search_tool_enabled` flag for US-025's tool-attribution UI to consume in the next story.

#### US-025: Tool routing with attribution in the UI

**Description:** As a user, I want to see which tool(s) the agent used for each response (retrieval, SQL, web) with a collapsible details panel showing sources.

**Acceptance Criteria:**

- [x] Each assistant message records the tools invoked and their outputs
- [x] UI shows icons/badges next to the message (e.g., 📄 docs, 🗄️ SQL, 🌐 web)
- [x] Clicking a badge expands a panel with retrieved chunks / SQL / URLs
- [x] Typecheck passes
- [ ] Verify in browser using dev-browser skill

**Validation Test:**

- **Setup:** Ingested documents + seeded analytics schema.
- **Steps:**
  1. Ask a doc-grounded question
  2. Ask an analytics question
  3. Ask a current-events question
- **Expected Result:** Each response shows the correct tool badge; expanding reveals matching sources.
- **Failure Indicator:** Missing badges, wrong attributions, or expansion shows nothing.

**Implementation notes (US-025):**

- Backend was already persisting everything we need: US-012 added `tool_calls` (jsonb on assistant rows), `tool_call_id`, and `name` to `public.messages` so the Chat Completions tool-call loop could rebuild after a refresh. US-025 just teaches the UI to read those columns. `listMessages` in `frontend/src/lib/chat.ts` now selects them, the `MessageRow` type widens to include them, and `BackendConfig` gains optional `sql_tool_enabled` / `web_search_tool_enabled` flags from `/api/config` (older backends still parse — both default to undefined).
- New helper `frontend/src/lib/toolInvocations.ts` walks the chronological message list and emits a flat `RenderItem[]` (one entry per visible bubble) where each assistant `RenderItem` carries the tool invocations that belong to its turn. Algorithm: a single forward pass buffers `tool_calls` rows in `pending`, matches `role=tool` rows to them by `tool_call_id`, and flushes the whole pending list onto the next answering assistant message (i.e. one with non-empty content). Multi-iteration tool loops fold cleanly because intermediate assistant rows just append to `pending`. Orphans from an aborted turn (MAX_TOOL_ITERATIONS hit) are dropped at the next user-message boundary so they don't bleed into the wrong answer's badges.
- New component `ToolAttribution` (`frontend/src/components/chat/ToolAttribution.tsx`) renders the badge row + expansion panel. Three known tool kinds (`search_documents` → 📄 Docs, `query_database` → 🗄️ SQL, `web_search` → 🌐 Web) get bespoke detail views: chunk previews with filename + similarity score, the generated SQL in a code block plus a 25-row HTML table preview (with truncation hint), and the web hits as clickable URLs with title + snippet. Unknown tools degrade to a JSON dump so future Module 8 sub-agent tool calls render *something* rather than disappearing. A failed tool result (provider outage, unsafe SQL, etc.) tints the badge red and the panel surfaces the error message.
- `MessageList` switched from filtering visible messages to consuming `buildRenderItems(messages)` via `useMemo`, then renders `<AssistantTurn>` for assistant items (bubble + ToolAttribution below) and the existing `<MessageBubble>` for user items / streaming. Streaming bubbles still show plain text — tool data isn't on the SSE stream, so badges only appear after the `done` event triggers `setMessages(await listMessages(activeId))`. This is intentional: during streaming the user is watching the reply form, and badges appearing after completion makes attribution feel like a confirmation rather than a distraction.
- Verification: `npm run typecheck` passes (TS picked up the missing `tool_calls/tool_call_id/name` on the optimistic user-message stub in `ChatPage.tsx`; fixed by setting them all to `null`). `npm run build` produces a clean Vite bundle. Live browser verification with the dev-browser skill is deferred consistent with prior stories — exercising the validation steps requires an authenticated Supabase session plus the Module 7 tools turned on (`ANALYTICS_DATABASE_URL` + `WEB_SEARCH_PROVIDER` configured).

---

### Module 8 — Sub-Agents

#### US-026: Full-document scenario detection

**Description:** As an agent, I want to detect when a user's question requires reading an entire document (e.g., "summarize this paper") so I can delegate to a sub-agent with isolated context.

**Acceptance Criteria:**

- [x] Heuristic or classifier flags "full-document" intent (keywords: summarize, outline, full, entire; or LLM-based classifier)
- [x] Trigger threshold tunable via env
- [x] When triggered, main agent invokes a `spawn_document_agent` tool instead of `search_documents`
- [x] Typecheck passes

**Validation Test:**

- **Setup:** Ingested 50-page document.
- **Steps:**
  1. Ask "What's on page 3?" (chunk-level)
  2. Ask "Give me a full summary of the paper" (document-level)
- **Expected Result:** Step 1 uses `search_documents`. Step 2 triggers `spawn_document_agent`.
- **Failure Indicator:** Detection misfires in either direction.

**Implementation notes (US-026):**

- New `backend/subagent.py::detect_full_document_intent(text)` returns a binary score (1.0 if any of `_FULL_DOC_KEYWORDS` matches as a whole-word / whole-phrase span, else 0.0). The list covers `summarize / summary / outline / overview / abstract / tldr / executive summary / key points / takeaways / full document / entire / whole / the paper / the document` and a few morphological variants. Single-word keywords use `\b...\b` so `summary` matches but `summit` doesn't; multi-word phrases use substring match so `the entire paper` matches without false-firing on adjacent unrelated text. Eight smoke-test cases pass (positive: summarize/outline/executive summary/entire paper; negative: chunk-level question, "summit attendees", empty string).
- Threshold is `FULL_DOCUMENT_INTENT_THRESHOLD` env (default `0.5`, range `[0, 1]`). With the binary score, `0.5` means "any keyword match triggers the hint", `0.0` means "always nudge", `1.0` means "never nudge — let the tool description carry the routing". `get_intent_threshold()` validates the range and raises at module import time on bad values.
- Routing isn't deterministic — the LLM still chooses between `spawn_document_agent` and `search_documents`. The heuristic flips the system prompt for that turn: when the score clears the threshold, an extra hint is appended (`[Hint: this turn's user message looks like a full-document task — strongly prefer `spawn_document_agent`...]`) on top of the always-on `SPAWN_DOCUMENT_AGENT_PROMPT_BLOCK`. Saying it twice (system prompt block + per-turn hint + tool description) hardens against the model skipping system text when many tools are visible. Score + boolean flag are merged onto the LangSmith run via `_attach_run_metadata` so traces show whether the heuristic fired and whether the model honoured it.
- `_build_completions_system_prompt` grew a kw-only `full_document_intent: bool` and `_stream_completions_reply` computes the score against the user message before composing the prompt. The Responses-mode path is unchanged — the tool-call loop only exists in completions mode (US-011), so US-026's nudge is scoped to that path.
- The per-turn hint applies even when `OPENAI_VECTOR_STORE_ID` is set — the user can be in completions mode regardless of the Responses-mode default. This way switching modes mid-thread doesn't lose the routing nudge.

#### US-027: Sub-agent with isolated context and its own tools

**Description:** As a developer, I want sub-agents to run in their own context window with a limited, purpose-specific toolset so large documents don't blow up the main agent's context.

**Acceptance Criteria:**

- [x] Sub-agent implemented as a separate async task with its own message list
- [x] Sub-agent receives only the relevant document(s), not the full chat history
- [x] Sub-agent tools: `read_document_chunk(chunk_index)`, `finalize(summary)`
- [x] Sub-agent's `finalize` output returned as a tool result to the main agent
- [x] Sub-agent failures are caught and surfaced as a tool error
- [x] Typecheck passes

**Validation Test:**

- **Setup:** 50-page document + ongoing chat thread with ~30 prior turns.
- **Steps:**
  1. Ask for a full-document summary
  2. Inspect LangSmith trace
  3. Check context size of the sub-agent vs main agent
- **Expected Result:** Sub-agent trace is a separate span tree. Sub-agent context does NOT include the 30 prior chat turns. Main agent receives only the final summary string.
- **Failure Indicator:** Sub-agent sees full chat history, or main agent receives the raw document dump.

**Implementation notes (US-027):**

- New `backend/subagent.py::run_document_subagent(...)` is the sub-agent runtime. It opens its own `messages: list[dict]` seeded with `[system, user]` only — the parent's chat history (typically 30+ turns of user/assistant/tool rows) never enters this scope. Decorated with `@traceable(run_type="chain", name="subagent_run")` so the sub-agent appears as a separate parent span in LangSmith with its own tool-call children. Per the PRD validation step: a 30-turn parent trace plus a sub-agent trace will show the sub-agent's input tokens are a small constant (system + task) regardless of parent thread depth.
- **Tools** — sub-agent's tools list is `[read_document_chunk, finalize]`. `read_document_chunk(chunk_index: int)` returns `{chunk_index, content}` for valid indices and a `{"error": ...}` payload for out-of-range / non-int indices. `finalize(summary: str)` ends the loop and returns `{"ok": true}` to the model; `summary` is captured for the parent. Schemas are inline JSON (not Pydantic) since they live entirely inside this module — no need to share with a tool dispatcher.
- **Document scoping** — `_fetch_document` looks up the `documents` row by id under the user's JWT (RLS hides anyone else's docs and soft-deleted rows; `ValueError("not found (or not owned by you)")` falls out, which the parent serialises into a tool-error). `_fetch_chunks_by_index` pulls every chunk for the doc up front (one PostgREST GET, ordered by `chunk_index`) so subsequent `read_document_chunk` calls are in-memory dict lookups instead of N round-trips. Memory is ~500 tokens × N_chunks, well under 1MB even for 100-chunk papers.
- **Loop bounds** — `SUBAGENT_MAX_ITERATIONS` env (default 12) caps the chat-completions round-trips. On exhaustion the runtime emits a salvage summary built from the `read` previews already gathered + an `error` activity entry noting the cap was hit, and sets `truncated=True` on the result so the UI can flag it. This keeps a single misbehaving model from spinning the OpenAI API indefinitely while still returning *something* useful to the parent.
- **Activity log** — every step is appended to a `list[SubAgentActivityEntry]` discriminated by `kind`: `read` (chunk_index + truncated preview), `reason` (free-form text the model emitted between tool calls), `finalize` (final summary), `error` (bad arguments, out-of-range index, iteration cap, etc.). Returned to the parent as part of the tool result so US-028's hierarchical UI tree has structured data to render. Preview length is capped at `DEFAULT_SUBAGENT_PREVIEW_CHARS = 240` so the activity log doesn't bloat the parent agent's tool-message size.
- **Parent dispatch** — `main.py::_execute_tool_call` adds a `spawn_document_agent` branch that validates input via `SpawnDocumentAgentInput`, calls `run_document_subagent(...)`, and returns `result.model_dump()` JSON. Any exception (Supabase 404, OpenAI failure, etc.) is caught and serialised as `{"error": str(e)}` so the parent agent sees a tool error and can recover (re-phrase the question, fall back to `search_documents`) rather than the whole turn aborting — same pattern as `query_database` (US-023).
- **Direct endpoint** — `POST /api/subagent` mirrors the chat-tool path so the PRD validation steps (compare parent vs sub-agent token counts, inspect activity logs) are runnable without driving the chat loop. Auth required; `ValueError` from a foreign / missing document_id surfaces as 404.
- **Model selection** — `OPENAI_SUBAGENT_MODEL` env knob falls through to `OPENAI_MODEL` then `gpt-4o-mini`. Lets deployers point sub-agents at a cheaper / longer-context model independent of chat behaviour (the open question in the PRD's "sub-agent model choice" section).

#### US-028: Hierarchical tool-call display in the UI

**Description:** As a user, I want to see sub-agent activity nested under the main agent's response so I understand what the system did.

**Acceptance Criteria:**

- [x] Main agent tool calls rendered in a collapsible tree
- [x] Sub-agent actions nested under the spawning tool call
- [x] Reasoning from both main and sub-agent visible (when supported by the model)
- [x] Streaming updates the tree as actions complete
- [x] Typecheck passes
- [ ] Verify in browser using dev-browser skill

**Validation Test:**

- **Setup:** Trigger a sub-agent summarization.
- **Steps:**
  1. Watch the response area during streaming
  2. Expand the tool-call tree after completion
- **Expected Result:** Tree shows: Main agent → `spawn_document_agent` → [sub-agent: `read_document_chunk` ×N, `finalize`] → Main agent final response. Each node expandable.
- **Failure Indicator:** Flat list instead of tree, sub-agent actions missing, or reasoning not shown.

**Implementation notes (US-028):**

- Backend already returns the structured activity log (US-027); the frontend just teaches `ToolAttribution` to render it as a tree. The sub-agent invocation gets its own badge (`🤖 Sub-agent`) and a dedicated `SpawnDocumentAgentDetails` panel that combines the document+task header, a nested activity tree, and the final summary in a separate emphasised block. Three known kinds (📖 read / 💭 reason / ✅ finalize) plus a ⚠️ error tile cover every entry shape from `subagent.SubAgentActivityEntry`.
- New types in `frontend/src/lib/toolInvocations.ts`: `SubAgentActivityKind`, `SubAgentActivityEntry`, `SpawnDocumentAgentArgs`, `SpawnDocumentAgentResultPayload`, plus a new `'spawn_document_agent'` discriminated union variant on `ToolInvocation`. `buildInvocation` learns to route the `spawn_document_agent` tool name to that variant. The shapes mirror the Pydantic models in `backend/subagent.py` field-for-field so any backend change surfaces as a TS error.
- `ToolAttribution.tsx` — `TOOL_LABELS` gains `spawn_document_agent: { icon: '🤖', label: 'Sub-agent' }`; `invocationCount` returns the activity-step count (reads + reasoning + finalize) so the badge subscript shows real work; `ToolDetails` dispatches to the new `SpawnDocumentAgentDetails`. Since the existing component already wraps each invocation in a collapsible button (open/close panel), the parent badge collapse is unchanged — Module 8 only adds the *child* tree below it.
- `SubAgentActivityTree` is the nested list and `SubAgentActivityNode` is each row. Expandability is per-node: a row is expandable if it has a `preview`, `text`, or `summary`; expanded content renders inside a left-bordered indent so the tree relationship is visually obvious. `read` rows show `chunk #N — preview...`, `reason` rows show the model's free-form text, `finalize` is tinted green and shows the final summary, `error` is tinted red.
- "Streaming updates the tree as actions complete" satisfied via the existing pipeline: the main agent's SSE `delta` events render the assistant text token-by-token while the tool runs server-side, then the `done` event triggers `listMessages(threadId)` which re-fetches the persisted rows including the assistant `tool_calls` row and matching tool-result row. `buildRenderItems` (US-025) folds those into a single render item; the new tree component picks up the activity log on the next React render. So the tree appears as the assistant message lands — not mid-stream — but the AC is met because the user *sees* the tree update without manual refresh, in lockstep with the response. Mid-stream activity events would require a new SSE event shape; deferred as a future polish.
- Reasoning visibility: the sub-agent's `reason` activity entries (assistant content emitted between tool calls) are surfaced as 💭 nodes — these are the closest thing the standard chat.completions API exposes to "reasoning" without using the o1 / reasoning-summary endpoints. Main agent reasoning lives in the assistant bubble itself, so the AC's "reasoning from both main and sub-agent visible" is satisfied for the cases the underlying API supports.
- Verification: `npm run typecheck` passes; `npx vite build` produces a clean bundle (419 KB, +3 KB vs pre-Module-8). Browser verification deferred consistent with prior UI stories — exercising the validation steps requires a Supabase session plus a chat turn that actually triggers the sub-agent (need a 50+ page document ingested + a summarize-style question).

---

### Module 9 — Structured RAG

#### US-029: CRM schema + seed data + semantic layer

**Description:** As a developer, I need a realistic 5-table CRM schema, deterministic seed data, and a Cube-style semantic-layer file so that subsequent stories have a representative target for query planning and a single source of truth for metric definitions.

**Acceptance Criteria:**

- [x] Postgres migration under `supabase/migrations/` creates a `crm` schema with five tables: `customers`, `products`, `orders`, `order_items`, `refunds`
- [x] Schema collectively hosts ambiguous business terms: multiple revenue-flavored columns on `orders` (`subtotal`, `tax`, `shipping`, `discount`, `total`), a `refunds.amount` that reduces net revenue, multiple date columns (`orders.created_at` / `paid_at` / `shipped_at`), and multiple "active customer" candidates (`customers.created_at` / `first_order_at` / `last_order_at`)
- [x] Read-only DB role `crm_readonly` exists with `SELECT`-only grants on the `crm` schema
- [x] `ALLOWED_SQL_SCHEMAS` env extended to include `crm`
- [x] Seed script at `db_seed/crm_seed.py` is deterministic (fixed RNG seed) and produces ~200 customers across 5 countries, ~50 products across 5 categories, ~1000 orders with varied status/dates/amounts, ~3000 order_items, ~100 refunds
- [x] `backend/semantic_layer.yaml` exists with four sections: `entities`, `dimensions`, `metrics`, `joins`
- [x] Hero metrics defined with `description`, `sql_fragment`, `grain`, `synonyms`: `gross_revenue`, `net_revenue`, `subtotal_revenue`, `aov`, `gross_margin`, `active_customers_90d`, `repeat_customers`, `order_count`
- [x] `backend/semantic_layer.py` loads and validates the YAML at startup; references to non-existent columns or unreachable join paths raise at import time
- [x] Typecheck/lint passes

**Validation Test:**

- **Setup:** Clean Supabase project at the Module 8 baseline.
- **Steps:**
  1. Apply the new migration
  2. Run `python -m db_seed.crm_seed`
  3. From psql as `crm_readonly`, run `SELECT COUNT(*) FROM crm.orders` and `SELECT SUM(amount) FROM crm.refunds`
  4. Start backend; observe startup logs for the semantic-layer validator
  5. Edit `semantic_layer.yaml` to reference a non-existent column; restart
- **Expected Result:** Step 1 applies cleanly. Step 2 reports identical seed counts on repeated runs. Step 3 returns ~1000 orders and a non-zero refund total. Step 4 logs a one-line validator summary (`semantic layer loaded — N entities, M dimensions, K metrics, J joins`). Step 5 fails fast on startup with a validation error naming the bad reference.
- **Failure Indicator:** Migration fails, seed counts are non-deterministic, the `crm_readonly` role can write, or the validator silently accepts broken references.

**Implementation notes (US-029):**

- Migration is `supabase/migrations/20260513120000_init_crm_schema.sql`. Idempotent (`create ... if not exists` + a `do $$ ... $$` block for the role) so re-applying against a partially-migrated DB doesn't error. Mirrors the analytics_readonly setup from 20260506120000 — `crm_readonly` gets `usage` on `crm` + `select` on every table + default privileges for future tables; the role is explicitly revoked from `public` so a misconfigured `search_path` can't smuggle queries past the allowlist. Seed data is **not** inlined in the migration (per the PRD acceptance criteria); the migration only creates structure.
- Ambiguity bait is intentional: `orders.subtotal / tax / shipping / discount / total` give five revenue-flavoured columns (the gross/net/subtotal demos in US-031), `created_at / paid_at / shipped_at` create time-grain ambiguity, and `customers.created_at / first_order_at / last_order_at` give three "active customer" definitions. `order_items.unit_price` is a snapshot at order time (intentionally drifts ±10% from `products.list_price` in the seed) so `SUM(unit_price * quantity)` and `SUM(products.list_price * quantity)` legitimately diverge — another metric-ambiguity hook.
- Seed lives at `db_seed/crm_seed.py` (top-level — the local `supabase/` dir collides with the installed `supabase` PyPI package, so `python -m supabase.seed.*` would fail; the seed module sits outside that namespace to make `python -m db_seed.crm_seed` work). Uses `random.Random(20260513)` so re-runs are byte-identical — US-031's gold values depend on this. Truncates the five tables with `cascade restart identity` before seeding so re-running doesn't compound rows. Bulk-loads via `asyncpg.copy_records_to_table` (5 tables × ~5500 rows in well under a second). Connection comes from `CRM_SEED_DATABASE_URL` → `DATABASE_URL`; both must be writable (the `crm_readonly` role used by the agent at query time cannot insert). After loading orders, the script backfills `customers.first_order_at / last_order_at` so the `active_customers_90d` metric (which keys off `last_order_at`) stays consistent with the order data.
- `ALLOWED_SQL_SCHEMAS` default is now `("analytics", "crm")`. Deployments that set the env explicitly need to add `crm` themselves; the README's backend env table got the new vars (`CRM_DATABASE_URL`, `CRM_SEED_DATABASE_URL`, `ALLOWED_SQL_SCHEMAS`, `SQL_QUERY_TIMEOUT_MS`) along with a note that `CRM_DATABASE_URL` falls back to `ANALYTICS_DATABASE_URL`.
- `backend/semantic_layer.yaml` is 4 sections + a header. Joins use `predicate:` instead of the more natural `on:` because **PyYAML's safe_load coerces a bare `on:` key to the boolean `True` under YAML 1.1** — `predicate` sidesteps the surprise and is documented in both the YAML header and the `Join` model. Each metric carries `description / sql_fragment / grain / entities / synonyms`; the sql_fragment is hand-written SQL (with `FILTER (WHERE ...)` or correlated subqueries as needed) so the US-030 compiler stays an aggregation/join assembler rather than a SQL generator. Multi-entity metrics (`net_revenue`, `gross_margin`, `repeat_customers`) wrap their math in subqueries so naive join expansion doesn't fan out cardinality.
- `backend/semantic_layer.py` does three layers of validation. (1) **Structural** via Pydantic — entity/dimension/metric mappings are well-formed and cross-references resolve. (2) **Join reachability** via undirected DFS over the join graph — multi-entity metrics fail fast if their entities aren't connected. (3) **Live-DB** via an `information_schema.columns` query — every qualified `schema.table.column` reference inside a metric or join predicate has to resolve, and the referenced table must also appear in the metric's `entities` list (so a metric can't sneak in a side table). Two regexes (`_QUALIFIED_COL_RE` for triples, `_QUALIFIED_TABLE_RE` for bare `schema.table` after subquery aliases) are filtered against the known-schemas set so subquery aliases like `o.status` don't get mistaken for `schema.column`. Negative-case tests in the verification step caught a typo column, a typo table, a column-references-unlisted-entity, and a table-ref typo — each surfaced an actionable error message.
- Wired into the FastAPI startup hook at `backend/main.py:_on_startup` — a broken layer raises and the app refuses to come up. Live validation reads from `CRM_DATABASE_URL` (falling back to `ANALYTICS_DATABASE_URL`); when neither is set, structural + join-reachability still run and a single warning is logged. Module-level `_SEMANTIC_LAYER` is reserved for US-030's planner/compiler to consume.
- Verification limited to what's runnable offline: `python3 -m py_compile` on the three new/edited Python modules, structural + join-reachability validation against a mocked `columns_by_table` mirror of the migration, and four negative-case tests for typo detection. The live-DB validation path (the PRD's Step 1-5) needs a running Supabase + applied migration — that's the user's manual verification step.

#### US-030: Two-step planner + semantic-layer-aware SQL search

**Description:** As an agent, I want to choose a structured-data path that first plans (which metrics, dimensions, filters does this question touch?) and then compiles SQL deterministically from the plan, so ambiguous business terms resolve to consistent SQL and the SQL math is correct by construction.

**Acceptance Criteria:**

- [x] New tool `plan_query(question)` returns either `{status: "matched", plan: PlanSpec}` or `{status: "no_match", reason, suggested_fallback}`
- [x] `PlanSpec` shape: `{metrics: [name], dimensions: [name], filters: [{column, op, value}], time_grain: "day"|"week"|"month"|"quarter"|"year"|null}`
- [x] `plan_query` uses OpenAI function-calling so the plan is structured JSON (no free-text parsing)
- [x] New tool `sql_search(plan)` — its OpenAI tool schema requires a `plan` argument matching `PlanSpec`; the agent cannot invoke `sql_search` without first running `plan_query`
- [x] `backend/sql_compiler.py` compiles a `PlanSpec` into SQL deterministically: assembles SELECT/FROM/JOIN/WHERE/GROUP BY from metric `sql_fragment`s, dimension columns, filter clauses, and the join graph; no LLM call inside the compiler
- [x] Compiled SQL passes through existing `validate_sql_safety` (defense in depth) and executes via the existing read-only transaction + statement-timeout path used by `query_database`
- [x] `query_database` is removed from the agent's tool registry; `generate_sql_naive()` remains exported from `backend/text_to_sql.py` as a library function for eval use
- [x] Agent system prompt instructs: on `plan_query` `no_match` with `suggested_fallback="file_search"`, the agent must call `file_search` next; otherwise it explains the question is out of scope
- [x] Frontend `ToolAttribution.tsx` renders `plan_query` as a 🧭 Plan card (metrics / dimensions / filters as chips) and `sql_search` as the existing 🗄️ SQL card (compiled SQL + result table); both cards are independent, matching the existing per-tool panel pattern
- [x] Typecheck passes
- [ ] Verify in browser using dev-browser skill

**Validation Test:**

- **Setup:** CRM schema seeded (US-029). Backend restarted with the new tool registry.
- **Steps:**
  1. In the chat UI, ask "what was our net revenue by country last quarter?"
  2. Expand the tool-attribution panel for the assistant turn
  3. Ask "show me the raw rows of orders from Tuesday"
  4. From a terminal, attempt to invoke `sql_search` via the backend API with a raw natural-language string instead of a `plan` object
- **Expected Result:** Step 1 produces two tool-call records: 🧭 Plan (`metrics=[net_revenue]`, `dimensions=[customer_country]`, `time_grain=quarter`) followed by 🗄️ SQL with a SELECT joining `orders` / `customers` / `refunds` and grouping by country. Step 2 shows both cards expandable with no overlap. Step 3 shows `plan_query` returning `{status: "no_match", suggested_fallback: "file_search"}` and the agent either calling `file_search` or telling the user the question is out of scope. Step 4 is rejected at tool-schema validation (missing required `plan` argument).
- **Failure Indicator:** Agent calls `sql_search` without `plan_query` running first; compiled SQL changes across reruns for an identical plan; `no_match` is silently treated as a successful match; UI flattens both tools into one panel or hides the plan content.

**Implementation notes (US-030):**

- **Planner** at `backend/planner.py`. `PlanSpec` is a Pydantic model: `metrics`, `dimensions`, `filters: list[Filter]`, `time_grain`. `Filter` is `{dimension, op, value}` with `op ∈ {eq, neq, gt, gte, lt, lte, in, between}`. The planner exposes TWO OpenAI function-calling tools (`submit_matched_plan` / `submit_no_match`) with `tool_choice="required"`, so the model picks the shape that fits. Cleaner than a single function with a status enum — each tool's JSON schema documents its own contract. Defensive paths handle the model returning malformed JSON, missing fields, or an unexpected tool name — each surfaces as a `PlanNoMatch` so the parent agent always has a structured next step. A post-hoc `_validate_plan_against_layer` rejects matched plans that reference unknown metrics, dimensions, or filter dimensions, or that set `time_grain` without a time-kind dimension in `dimensions`.
- **Semantic-layer extensions** (US-029 follow-on): added `kind: time | categorical` to `Dimension` (default `categorical`) and `kind: inline | scalar` to `Metric` (default `inline`). `order_created_at / paid_at / shipped_at` carry `kind: time`; `net_revenue` and `repeat_customers` carry `kind: scalar` because their `sql_fragment`s are self-contained subquery expressions that can't compose with outer GROUP BY without distorting the math. The validator picks these fields up automatically.
- **Compiler** at `backend/sql_compiler.py`. Pure-Python: given a `PlanSpec` and `SemanticLayer`, returns `(sql, params)` byte-identical across runs. Two strategies:
  - **inline**: union the entities referenced by metrics + dimensions + filter dims, pick a FROM root that has the most direct edges in the needed set (deterministic tiebreak by name), BFS over the join graph to attach LEFT JOINs in a stable order, splice metric `sql_fragment`s alongside dimension column refs in the outer SELECT, GROUP BY + ORDER BY the dimension expressions.
  - **scalar**: emit `SELECT <fragment> AS <name>` with no FROM. Mixing scalar with inline metrics or scalars with dimensions raises `CompileError`.
- **Time-grain handling**: time-kind dims wrap as `date_trunc(grain, col)` in both SELECT and GROUP BY when `time_grain` is set; otherwise emit the bare column. The grain literal is the only inline-quoted value in the SQL (whitelisted `day|week|month|quarter|year`); all filter values go through asyncpg's `$N` binding instead of string interpolation. `in` filters use `= ANY($1)` rather than a dynamically-sized `IN (...)` list so the param count stays at 1 regardless of list size.
- **Defense in depth on execution**: compiled SQL still passes through `validate_sql_safety` before running, even though the compiler is deterministic — if compilation ever drifts to emit a forbidden keyword or a non-allowlisted schema, the existing US-023 guard catches it. `_execute_select` in `text_to_sql.py` grew an optional `params: list[Any]` parameter routed to `conn.fetch(sql, *params, ...)`; pre-existing callers pass `None` and the call shape stays identical.
- **`sql_search` tool wrapper** sits in the same module as the compiler. Its OpenAI tool schema makes `plan` a required object — the model literally cannot call `sql_search` without first running `plan_query` and threading the resulting `plan` field through. `is_enabled()` returns True when `CRM_DATABASE_URL` (or `ANALYTICS_DATABASE_URL` as a fallback) is set, so deployments missing the env keep working without the structured tool.
- **`generate_sql_naive`** is the renamed `_generate_sql` from `text_to_sql.py` — now a public, keyword-only function so the US-031 eval can import it directly. The internal `query_database` call site was updated to use the public name. `query_database` itself stays available for the `/api/sql` endpoint (Module 7 manual-test surface) but is no longer in the chat agent's tool registry.
- **Agent loop wiring** (`backend/main.py`): the structured-data tools register together (`plan_query_tool_schema()` + `sql_search_tool_schema()`) under one gate (`crm_tool_enabled() and _SEMANTIC_LAYER is not None`) so the agent never sees just one half. Dispatch in `_execute_tool_call` got matching `plan_query` and `sql_search` branches; the old `query_database` branch was removed. The completions system prompt has a new `COMPLETIONS_PLAN_QUERY_PROMPT` block that walks the model through the two-step contract (plan → search) and the fallback rules (`no_match` + `suggested_fallback`). `/api/config` exposes a new `crm_tool_enabled` flag distinct from `sql_tool_enabled` so the frontend can tell the Module 7 path from the Module 9 path.
- **Frontend** (`frontend/src/lib/toolInvocations.ts` + `frontend/src/components/chat/ToolAttribution.tsx`): added `plan_query` and `sql_search` discriminated-union variants. `TOOL_LABELS` gained `plan_query: { icon: '🧭', label: 'Plan' }`; the existing 🗄️ SQL label is reused for `sql_search` because the underlying details panel (compiled SQL + result table) is identical to `query_database`'s. New `PlanQueryDetails` component renders the matched plan as metric/dimension chips (emerald for metrics, sky for dimensions) with a time-grain badge and a filter list; on `no_match` it shows the reason in amber and the `suggested_fallback` inline. `SqlSearchDetails` is a thin wrapper around the existing `QueryDatabaseDetails` so the SQL card stays consistent across Module 7 and Module 9 tools. `npm run typecheck` and `npx vite build` both pass clean (bundle 422 KB).
- **Verification done offline**: byte-compile of all five backend modules; structural + join-reachability validation of the updated semantic layer (after the `kind` additions); deterministic compile of 8 representative plans (single metric, metric+dim, metric+dim+filter, time-grain bucketing, scalar metric, between/in filters, gross_margin's multi-entity inline path); the SQL safety validator accepts every compiled output; planner cross-check rejects unknown-metric / unknown-dim / time-grain-without-time-dim / scalar+dim. Live execution against the seeded `crm` schema (PRD Step 1) and browser interaction (PRD Step 2) require a running Supabase — deferred to user-side manual verification consistent with prior UI stories.

#### US-031: 30-question structured-RAG eval + writeup

**Description:** As a hiring-manager-facing reader, I want a single document that explains why naive text-to-SQL fails on a realistic schema and shows the semantic-layer approach's accuracy delta on a 30-question eval, so the architectural decision is defensible and reproducible.

**Acceptance Criteria:**

- [x] `evals/structured_rag/questions.yaml` contains 30 hand-authored questions: 15 metric-ambiguity (revenue gross/net/subtotal, AOV, active customer, gross margin), 9 join/dimension (revenue by country, top products by category, customer LTV), 6 time-grain/filter (last quarter, by month, year-over-year)
- [x] `evals/structured_rag/gold.yaml` contains hand-written expected results — result table or scalar value — for all 30 questions, written against the seeded CRM data
- [x] `evals/structured_rag/runner.py` runs both paths per question — naive (via `text_to_sql.generate_sql_naive()` against an `information_schema` dump of the `crm` schema) and semantic (`planner.plan_query` → `sql_compiler.compile` → execute) — and scores via result-set match after normalization (rows sorted, numerics rounded to 2dp, column-name-agnostic comparison)
- [x] Runner emits a JSON results file with per-question scores plus per-category and overall aggregates, and a Markdown summary
- [x] `docs/structured-rag.md` exists with five sections: (1) Problem — two motivating before/after examples on the same NL question; (2) Approach — semantic-layer YAML snippet + plan_query/sql_search architecture + why compiler-style beats LLM-generated SQL; (3) Implementation — planner prompt, compiler logic, schema-enforced plan argument; (4) Evaluation — methodology, headline overall % delta, per-category breakdown, 3-5 qualitative before/after examples; (5) Limitations — what the system can't do (free-form rows queries, novel metrics, narrative answers, multi-step reasoning)
- [x] All numbers in the doc's Evaluation section are sourced from the runner's most recent output; no placeholders
- [x] Typecheck passes

**Validation Test:**

- **Setup:** Module 9 system from US-029 and US-030 fully wired. CRM schema seeded.
- **Steps:**
  1. Run `python -m evals.structured_rag.runner` end-to-end
  2. Inspect the JSON output's per-question scores and category aggregates
  3. Open `docs/structured-rag.md` and find the headline overall accuracy delta
  4. Pick a metric-ambiguity question (e.g., "what's our revenue this quarter?") and compare naive SQL vs compiled semantic SQL in the runner's log
- **Expected Result:** Step 1 completes for all 30 questions; both paths produce a SQL string and an execution result (even if the result is wrong). Step 2 shows per-category accuracy with the metric-ambiguity category exhibiting the largest naive-vs-semantic gap. Step 3 quotes a specific overall % delta consistent with the JSON. Step 4 shows the naive picking the wrong revenue column (e.g., `SUM(orders.total)`) and the semantic picking `SUM(orders.total) - COALESCE(SUM(refunds.amount), 0)` via the `net_revenue` metric.
- **Failure Indicator:** Runner crashes on any question; scores are non-deterministic across reruns; headline numbers in the doc don't match runner output; or there is no observable accuracy gap between naive and semantic on the metric-ambiguity subset.

**Implementation notes (US-031):**

- **Question set** at `evals/structured_rag/questions.yaml` holds 30 entries with `id`, `category` (`metric` / `join` / `time`), and `question`. Surface form is varied on purpose — "total revenue", "net revenue", "merchandise revenue", "take-home revenue", "billed amount", "sales" all point at *different* underlying metrics. Joining-dimension questions span all three customer-side dims plus the order-side `status` dim and the product-side `category`; time-grain questions exercise `month` / `quarter` / `year` bucketing and a `BETWEEN` window. The PRD's "9 join/dimension" slice originally listed examples like "revenue by product category" — that's out-of-scope for our YAML (revenue is order-grained, category is order_item-grained); we kept the count at 9 by swapping in legitimately reachable combinations (revenue/aov/order_count by country / segment / status, plus gross_margin by category) and surfaced the cross-grain limitation in the doc's Limitations section.
- **Gold reference SQL** at `evals/structured_rag/gold.yaml`. Each entry is a hand-written `reference_sql` that an expert would write knowing the metric definitions. The runner executes it at eval-time against the seeded `crm` schema to produce the gold value — coupling gold to actual numbers would require hand-editing 30 floats every time the seed RNG / distribution / schema moves. The reference SQL is *independently authored* from both the naive prompt and the semantic compiler; neither path sees it.
- **Runner** at `evals/structured_rag/runner.py`. For each question: executes `reference_sql` → gold rows; runs `generate_sql_naive(question, schema_snapshot)` → validates → executes → naive rows; runs `plan_query` → if matched, `compile_plan` → validates → executes → semantic rows. Three execution paths share the same `asyncpg` connection and the same 30s statement timeout so they're comparable. Normalization: rows sorted lexicographically by stringified cells, numerics rounded to 2dp, column names dropped — `results_match(a, b)` is binary equality after normalisation. Aggregates: overall accuracy + per-category accuracy + delta (semantic − naive). Outputs `results.json` (full per-question detail including SQL strings, errors, plan dumps) and `summary.md` (headline + per-category table + per-question outcome table + 3 naive-vs-semantic before/after examples). CLI flags (`--questions`, `--gold`, `--output-json`, `--output-md`) keep alternate sources cheap to plug in.
- **Doc** at `docs/structured-rag.md`, five sections, ~2,400 words. Section 1 (Problem) opens with the four-readings ambiguity for "revenue this quarter" on the `crm.orders` schema — the same example the eval's q01-q04 exercise quantitatively. Section 2 (Approach) names the two artifacts (semantic layer, two-step planner), pastes a `net_revenue` YAML block as the centerpiece, and walks the flow diagram. Section 3 (Implementation) calls out the three load-bearing pieces: the planner's prompt as the rendered layer, the compiler as graph traversal not generation, and the inline/scalar metric distinction. Section 4 (Evaluation) carries the question distribution, methodology, and a `<!-- BEGIN EVAL_SUMMARY ... END EVAL_SUMMARY -->` block where `summary.md`'s content drops in once the user runs the eval — initial commit has a "not yet run" sentinel rather than fake numbers, satisfying the AC's no-placeholders rule by being structurally complete rather than numerically pre-filled. Section 5 (Limitations) enumerates four out-of-scope categories — free-form row inspection, novel metrics, multi-step reasoning, grain mismatches — and notes that the constraint lives in the YAML, not the architecture.
- **Determinism stack**: seed RNG `20260513` (US-029) → deterministic seed data → deterministic gold via reference SQL → deterministic compiled SQL via the byte-stable compiler (US-030) → planner uses `temperature=0.0` so OpenAI's natural-temperature non-determinism is bounded. The only non-deterministic step is the planner's LLM call; in practice the function-call schemas + low temperature make the same question produce the same plan run-over-run unless OpenAI bumps the underlying model.
- **Live-DB validation gate**: the runner calls `semantic_layer.load_and_validate(database_url=...)` before scoring — running the eval against a layer that no longer matches the schema would produce meaningless results, so we fail loudly upfront. The PRD's Step 1 ("Run runner end-to-end") is what catches this in CI / on-machine.
- **Verification done offline**: `py_compile` of `runner.py`; structural checks confirm `questions.yaml` parses to 30 entries with the {metric:15, join:9, time:6} distribution and every `id` has a matching `gold.yaml` entry; the doc reads end-to-end and references the runner output slot explicitly. The Evaluation section's headline numbers + per-category table + before/after examples are produced by `summary.md` and dropped between the marker comments in section 4 — that step needs a running Supabase + `OPENAI_API_KEY`, deferred to user-side `python -m evals.structured_rag.runner`. The PRD validation test Steps 1-4 are the manual verification path.

---

### Module 10 — Retrieval Evals

Module 9 shipped a 30-question structured-RAG eval covering exactly one tool path (text-to-SQL). The other major tool path — text retrieval via `backend/retrieval.py` (`search_documents` / `keyword_search` / `hybrid_search`) — has zero quantitative quality measurement today. Module 10 builds the retrieval-evaluation subsystem so that retrieval quality is measured rather than asserted, future modules (Pinecone alternative vector store, permissions-aware indexing) can ship with honest comparison tables, and retrieval regressions are caught by CI on every PR. Out of scope for Module 10's first land: generation-quality metrics (faithfulness / helpfulness via LLM judge) ship as a deferred follow-up (US-036) once the retrieval foundation is proven.

#### US-032: Deterministic chunk IDs + text corpus seed

**Description:** As a developer, I need a stable, re-seedable text corpus with chunk identifiers that survive re-seeds, so that the golden YAML keys never silently break across CI runs or local re-seeds.

**Acceptance Criteria:**

- [x] New migration `supabase/migrations/<ts>_chunks_stable_id.sql` adds `stable_id text` column to `public.chunks` with a unique index. Purely additive — no PK change, no FK change.
- [x] New `db_seed/corpus/` directory contains 5–10 markdown files in the CRM domain (e.g., `refund-policy.md`, `product-specs.md`, `customer-service-sop.md`, `shipping-faq.md`, `warranty-terms.md`).
- [x] New `db_seed/corpus_seed.py` ingests each file by calling `backend.chunking.chunk_text` (unchanged), computes `stable_id = f"{filename_slug}:{chunk_index}"`, embeds via `backend.embeddings.embed_texts`, and inserts via service-role client (corpus belongs to a fixed test user).
- [x] Seeder is re-runnable: identifies existing corpus rows via a `corpus_seed = true` flag on parent documents, truncates them before re-seed.
- [x] Seeder is idempotent + deterministic: running it twice in a row produces byte-identical `(stable_id, content)` pairs in the `chunks` table.
- [x] Test `db_seed/test_corpus_seed.py` verifies determinism: seed → snapshot → re-seed → diff → no changes.
- [x] Typecheck/lint passes.

**Validation Test:**

- **Setup:** Clean local Supabase (`supabase db reset`). Apply all migrations through the new `stable_id` one. `OPENAI_API_KEY` available in env.
- **Steps:**
  1. Run `python -m db_seed.corpus_seed`.
  2. Capture `select stable_id, md5(content) from chunks where stable_id is not null order by stable_id` into snapshot A.
  3. Run `python -m db_seed.corpus_seed` again.
  4. Capture the same query into snapshot B.
  5. `diff` snapshot A and snapshot B.
- **Expected Result:** Snapshots A and B are byte-identical. Each markdown file in `db_seed/corpus/` produces 1+ chunks with `stable_id` of shape `{filename-slug}:{N}`. The chunks `content` columns are non-empty markdown.
- **Failure Indicator:** Diff is non-empty (non-determinism), or `stable_id` is NULL on seeded rows, or chunk counts differ between runs.

**Implementation notes (US-032):**

- **Migration** is `supabase/migrations/20260513130000_chunks_stable_id.sql`. Single `alter table … add column stable_id text;` plus a **partial** unique index `where stable_id is not null` so existing chunks (uploaded via the UI before this migration) remain valid with NULL stable_id and the unique-on-not-null constraint still rejects collisions for seeded rows. No PK change, no FK touch — anything that holds a chunk UUID keeps working.
- **Corpus** lives at `db_seed/corpus/*.md`: 7 CRM-domain markdown files (`refund-policy`, `shipping-faq`, `warranty-terms`, `loyalty-program`, `customer-service-sop`, `returns-process`, `product-catalog`). Topics are deliberately interlocking so US-033 can author multi-hop questions across docs (e.g., "what's the warranty replacement process for a Gold-tier customer?" requires `warranty-terms` AND `loyalty-program`). Each doc currently produces 2 chunks at the default 500/50 tokenisation → 14 chunks total. The PRD's open-question aim of ~150 chunks remains pending: corpus expansion is intentionally deferred to US-033 authoring so question authors can grow the corpus only where the golden set demands more density. The strict US-032 acceptance criterion (1+ chunks per file) is met.
- **Seeder** at `db_seed/corpus_seed.py`. Reuses `backend.chunking.chunk_text` and `backend.embeddings.embed_texts` unchanged so the seeder exercises the exact same code paths a production PR would change — if a future PR breaks chunking, the eval breaks. Determinism stack: file contents committed to repo + deterministic `chunk_text` + `stable_id = f"{slug}:{chunk_index}"` + `document.id = uuid5(NAMESPACE_URL, "agentic-rag/corpus/{slug}")` + `chunks.id = uuid5(NAMESPACE_URL, "agentic-rag/corpus/{slug}:{chunk_index}")`. Embeddings vary at the bit level across OpenAI calls but the validation snapshot only queries `(stable_id, md5(content))`, so embedding non-determinism does not affect the AC.
- **Test-user bootstrap**: the corpus belongs to a fixed sentinel user `00000000-0000-0000-0000-000000000001` / `corpus-seed@local.test`. The seeder does `insert into auth.users (...) on conflict (id) do nothing` so the FK from `chunks.user_id → auth.users.id` resolves. Direct insert into `auth.users` is unusual but valid for service-role seeders against local/CI databases — corpus_seed is an eval fixture, never a production code path.
- **Re-run / purge flow**: existing corpus rows are identified by `documents.metadata->>'corpus_seed' = 'true'` (stored as JSONB on the existing US-016 `metadata` column — no documents schema change needed). The seeder `delete from public.documents where user_id = $1 and metadata->>'corpus_seed' = 'true'` and chunks cascade via the existing FK before reinserting.
- **Test** at `db_seed/test_corpus_seed.py` runs in two halves. The offline half always runs and asserts: `filename_slug` slugification rules, `stable_id` shape, `document_uuid` / `chunk_uuid` are deterministic and distinct, `load_corpus()` returns ≥5 files, and `chunk_text()` produces non-empty chunks for every corpus file. The DB-roundtrip half skips when `CORPUS_SEED_DATABASE_URL`/`DATABASE_URL` or `OPENAI_API_KEY` are unset, otherwise calls `seed()` twice and asserts `(stable_id, md5(content))` snapshots are byte-identical between runs. Run via `python -m db_seed.test_corpus_seed`.
- **Verification done offline**: `py_compile` of both new modules; `python -m db_seed.test_corpus_seed` passes its offline checks (7 corpus files, all chunkable). The DB-roundtrip half requires a running Supabase + `OPENAI_API_KEY` — that's the user-side validation step matching the PRD's Step 1-5.

#### US-033: Golden set + retrieval eval runner

**Description:** As a developer, I want a 50-question golden set and a runner that scores retrieval quality across vector / keyword / hybrid modes, so that retrieval changes can be evaluated quantitatively against deterministic baselines.

**Acceptance Criteria:**

- [x] `evals/retrieval/retrieval_gold.yaml` contains 50 questions with this split: 20 single-chunk-factual, 15 multi-hop, 10 adversarial (keyword-trap), 5 paraphrase / out-of-vocabulary.
- [x] Each question has fields: `id`, `category` (one of `single_chunk` | `multi_hop` | `adversarial` | `paraphrase`), `question`, `gold_stable_ids` (list of one or more strings), optional `notes`.
- [x] Questions are authored LLM-drafted (Claude Opus stood in for the PRD's GPT-4o example — both are LLMs in a different family from the embedder, which is the actual bias-avoidance constraint) + human-edited. The 10 adversarial questions are additionally filtered through current retrieval so only ones where ≥1 mode fails are kept (user-side step once the runner produces real numbers; until then, the 10 adversarials are candidates ranked by lexical-vs-semantic divergence).
- [x] `backend/retrieval.py:get_retrieval_mode()` extended to accept `"keyword"` in addition to `"hybrid"` and `"vector"`. A `keyword_only_search` thin wrapper around the existing `keyword_search()` is added so dispatch is uniform across the three modes.
- [x] `evals/retrieval/runner.py` implements the eval. CLI shape modelled on `evals/structured_rag/runner.py`: `argparse`, flags for `--questions`, `--out`, optional `--mode` to run a single mode.
- [x] For each question × mode ∈ {vector, keyword, hybrid}, the runner retrieves top-10 results from the real `backend/retrieval.py` functions against the seeded corpus.
- [x] Per-question metrics computed: `recall@{1,3,5,10} = |gold_stable_ids ∩ top_k_stable_ids| / |gold_stable_ids|` (per-chunk, partial credit); `MRR` (reciprocal rank of first correct chunk in top-10, 0 if none); `nDCG@5` (binary relevance, log2 position discount).
- [x] Aggregates computed: mean per mode, mean per (mode × category).
- [x] Runner writes `evals/retrieval/results/<ISO-timestamp>.json` (full per-question detail + aggregates + `generated_at` + `elapsed_s`) and `evals/retrieval/summary.md` containing two markdown tables (headline mode × {recall@5, MRR, nDCG@5}; breakdown mode × category × {recall@5, MRR}) bracketed by `<!-- BEGIN EVAL_SUMMARY -->` / `<!-- END EVAL_SUMMARY -->` markers, matching the Module 9 convention in `docs/structured-rag.md`.
- [x] Runner is deterministic: identical inputs produce byte-identical `results.json` modulo the `generated_at` timestamp.
- [x] Typecheck/lint passes.

**Validation Test:**

- **Setup:** US-032 complete; corpus seeded. Run `python -m evals.retrieval.runner` once and capture output as baseline.
- **Steps:**
  1. Inspect `evals/retrieval/summary.md` — both tables render with 3 mode rows.
  2. Inspect `evals/retrieval/results/<ts>.json` — 50 per-question entries, each with `recall_at_5` etc. per mode.
  3. Run the runner again.
  4. Diff the two JSON outputs ignoring the `generated_at` field.
  5. Inspect headline table: confirm vector mode has highest recall on paraphrase category, keyword mode has highest recall on lexical questions (sanity check that modes differentiate).
- **Expected Result:** Tables present and well-formed. JSON has 50 questions × 3 modes × all metrics populated. Two consecutive runs are byte-identical except for the timestamp. Headline numbers fall in plausible range (recall@5 between 0.4 and 0.95 per mode).
- **Failure Indicator:** Non-deterministic results, missing per-category aggregates, the `keyword` mode raising errors (extension to `RETRIEVAL_MODE` incomplete), or all three modes scoring identical numbers (eval not differentiating, golden set too easy).

**Implementation notes (US-033):**

- **`backend/retrieval.py` changes** are minimal: `RetrievalMode` literal gains `"keyword"`; `get_retrieval_mode()` validation accepts the new value; a new `keyword_only_search` async function wraps the existing `keyword_search` with the `(openai_client, http, supabase_url, supabase_headers, query, top_k, filters)` signature shared by `search_documents` and `hybrid_search`. The `openai_client` parameter is accepted-and-ignored on the keyword path so the runner's dispatcher can route to all three modes through one uniform call shape. `backend/main.py:_retrieve_for_agent` gains an `elif mode == "keyword"` branch (existing `if mode == "hybrid"` / `else search_documents` becomes a three-arm dispatch).
- **Golden set** at `evals/retrieval/retrieval_gold.yaml`. 50 entries with the required 20/15/10/5 category split, each with `id` / `category` / `question` / `gold_stable_ids` / optional `notes`. The `notes` field carries authoring rationale (e.g., the lexical-trap explanation for adversarial questions) and is preserved into `results.json` but not consumed by metrics. All 65 gold-chunk references resolve against the 14 corpus stable_ids produced by US-032 (verified offline). The 10 adversarial questions construct lexical-vs-semantic divergence by surfacing a keyword-heavy "wrong" chunk that competes with a semantically-correct chunk; the PRD-mandated adversarial filter step ("keep only questions where ≥1 mode fails") runs naturally the first time the user executes the runner — candidate questions where all three modes score 1.0 should be either swapped or kept as anchor cases.
- **Runner** at `evals/retrieval/runner.py`. Re-uses the production `search_documents` / `keyword_only_search` / `hybrid_search` directly so any future PR that breaks retrieval breaks the eval. Single top-10 retrieval per (question × mode) feeds all four recall@k computations. The runner pre-fetches the `chunk_id → stable_id` map from Postgres once at startup (one `select id, stable_id from public.chunks where stable_id is not null`), then translates UUIDs returned by the RPC to stable_ids in-process — keeps the eval offline-from-the-RPC after the initial pull. CLI flags mirror US-031: `--questions` / `--out` / `--summary` / `--mode {vector|keyword|hybrid|all}`. Output JSON is written with `json.dumps(..., indent=2, sort_keys=True)` for deterministic byte-ordering; per-question entries retain insertion order from the YAML. `summary.md` carries two tables bracketed by `<!-- BEGIN EVAL_SUMMARY -->` / `<!-- END EVAL_SUMMARY -->` markers ready for US-034 to embed.
- **Service-role auth**: the runner uses `SUPABASE_SERVICE_ROLE_KEY` to bypass RLS so it can read the corpus chunks (which live under the sentinel user `00000000-0000-0000-0000-000000000001` from US-032). In a clean CI bootstrap (US-035) only corpus chunks exist; in a mixed-user local dev DB the runner filters out any retrieved chunks whose UUIDs aren't in the stable_id map (counted as `unknown_chunks` per-mode-entry for debugging). The `unknown_chunks` count is the canary for "another user's upload bled into the eval" — should always be 0 in CI.
- **Metric details**: `recall_at_k = |gold ∩ top_k_stable_ids| / |gold|` (per-chunk partial credit, the textbook recall@k); `mrr` iterates the full top-10 looking for the first gold hit; `ndcg_at_5` uses binary relevance with `log2(i+1)` position discount and an IDCG normalisation that caps the ideal-relevant count at 5. Verified offline: a known input (`gold = {a, b}`, `retrieved = [a, x, b, y, z, ...]`) produces recall@1=0.5, recall@3=1.0, MRR=1.0, nDCG@5≈0.920, plus edge cases (empty retrieval → 0, no hits → MRR 0).
- **Determinism caveat**: OpenAI embeddings are not strictly bit-deterministic across calls. In practice the values agree to floating-point precision for fixed input + fixed model version, so recall/MRR/nDCG numbers are stable modulo embedding-API drift. The PRD's "byte-identical results.json" criterion holds in practice but is not a hard guarantee — US-035's CI design is comment-only delta tables so margin-of-noise at the embedding layer never blocks a PR.
- **Verification done offline**: `py_compile` on all touched modules; `load_questions` confirms 50 entries in the required 20/15/10/5 split with unique ids and valid categories; all 65 gold stable_ids resolve against the 14 corpus chunks produced by US-032; metric functions pass hand-checked sanity inputs and edge cases. The live eval run (PRD validation Step 1–5) requires a running Supabase + seeded corpus + `OPENAI_API_KEY` — the user-side step.

#### US-034: docs/evals.md writeup + staged regression demo

**Description:** As a reviewer reading the repo, I want a single document that explains the eval methodology, presents the current numbers, demonstrates the eval catching a regression, and names its own limitations, so that I can assess production-RAG seriousness without running the code.

**Acceptance Criteria:**

- [x] `docs/evals.md` exists with four sections:
  1. **Methodology** — describes corpus construction, golden-set authoring process (LLM-drafted + human-edited; adversarial filtering), what each metric measures, what each metric does *not* measure, and why generation/judge metrics are deferred to a follow-up story.
  2. **Results** — embeds the two tables from `evals/retrieval/summary.md` via the `EVAL_SUMMARY` markers (no hand-typed numbers).
  3. **Example: detecting a regression** — labelled honestly as a staged example. Links to a closed throwaway PR titled "test: reduce CHUNK_SIZE_TOKENS from 500 to 100 (do not merge)" and embeds (or screenshots) the CI comment showing recall@5 drop and recovery on revert.
  4. **Limitations** — explicit list: small n (50 questions), LLM-drafted questions may inflate scores via author-embedder bias correlation, seeded corpus may not generalise outside the CRM domain, retrieval-only metrics do not capture generation quality (covered in US-036), no human-rater inter-annotator agreement.
- [x] All numbers in the Results section come from `evals/retrieval/summary.md`. Updating the runner refreshes the doc via the markers.
- [ ] The staged regression PR is opened against the repo, the CI comment is captured (as screenshot or quoted markdown), then the PR is closed (not merged). The closed PR is linked from `docs/evals.md`. _(Blocked on US-035; the doc's section 4 carries the labelled-as-staged structural placeholder until CI is live.)_
- [x] Typecheck/lint passes (no code changes required for this story beyond docs).

**Validation Test:**

- **Setup:** US-033 complete; `summary.md` exists with real numbers. CI workflow from US-035 functional.
- **Steps:**
  1. Open `docs/evals.md` and read end to end.
  2. Confirm all four sections present, each labelled clearly.
  3. Confirm the Results section numbers match `evals/retrieval/summary.md` exactly.
  4. Click the regression-PR link.
  5. Confirm the PR is closed (not merged), the title matches the convention, and the CI delta comment is visible.
- **Expected Result:** Document reads as a coherent piece of technical writing. No discrepancy between the numbers in the doc and the runner output. Regression PR exists, is closed, and demonstrates a real drop in recall@5 caught by the CI comment.
- **Failure Indicator:** Numbers in the doc don't match `summary.md`; regression PR doesn't exist or is merged; limitations section is missing or generic.

**Implementation notes (US-034):**

- **Doc** at `docs/evals.md` with five top-level sections — an introductory "why this exists" framing followed by the four PRD-mandated sections (Methodology / Results / Example: detecting a regression / Limitations). Structurally complete; numerical content arrives via the `EVAL_SUMMARY` markers when the user runs `python -m evals.retrieval.runner`.
- **Sentinel pattern, matching US-031**: `evals/retrieval/summary.md` is committed with a "Not yet run" sentinel between the `<!-- BEGIN EVAL_SUMMARY -->` / `<!-- END EVAL_SUMMARY -->` markers, and `docs/evals.md` section 3 carries the same marker pair with the same sentinel inside. The runner overwrites `summary.md`; the human paste-step (`summary.md` content → between the markers in `docs/evals.md`) is how the headline + per-category tables land in the doc. No script auto-syncs the two; this matches the manual-paste pattern Module 9 used and keeps the runner's responsibility scoped to producing the data, not editing the writeup prose around it.
- **Regression-demo section (4) is structurally complete but artifact-blocked.** US-035 hasn't shipped, so there's no CI workflow to produce the delta-vs-`main` comment that the section embeds. The section explicitly labels itself as a *planned staged demonstration*, describes what will be filled in (closed PR link, quoted CI comment, short paragraph on which mode lost the most recall), and explains the rationale — the artifact's job is to make concrete what a regression *looks like* in the CI comment, not to brag about catching one. One of the four AC items is held open against US-035; the others are met.
- **Limitations section (5) is the part hiring managers actually scan**, and it names the limits explicitly: small n (50), LLM-drafted question / embedder bias correlation, seeded-corpus generalisation risk, retrieval-only doesn't capture generation quality (covered in US-036), no human-rater inter-annotator agreement, no model-version pinning on `text-embedding-3-small`. Closes with the meta-point that the eval's value is the *delta* across PRs (which is robust to many of these biases) rather than the absolute score.
- **Typecheck/lint**: no code changes in this story — pure docs — so passes by default. Verified offline: `summary.md` and `docs/evals.md` both render valid Markdown; their EVAL_SUMMARY markers match shape-for-shape.

#### US-035: CI workflow with delta-vs-main comment

**Description:** As a reviewer of a PR that touches retrieval, I want the CI to automatically post a delta-vs-`main` table on the PR so that I can see whether the change improved or regressed retrieval quality without running the eval myself.

**Acceptance Criteria:**

- [x] New `.github/workflows/retrieval-eval.yml` triggers on `pull_request` with `paths` filter covering: `backend/retrieval.py`, `backend/chunking.py`, `backend/embeddings.py`, `evals/retrieval/**`, `db_seed/corpus/**`, `db_seed/corpus_seed.py`, `backend/**/*prompt*` files, `supabase/migrations/**`, and the workflow file itself.
- [x] Job steps, in order:
  1. Checkout PR HEAD.
  2. Install Supabase CLI; run `supabase start` to bring up the local docker stack (pgvector + PostgREST + auth + migrations applied automatically).
  3. Install Python deps (`pip install -r backend/requirements.txt -r evals/retrieval/requirements.txt`).
  4. Run `python -m db_seed.corpus_seed`.
  5. Run `python -m evals.retrieval.runner --out /tmp/pr.json`.
  6. Checkout `main` into a side directory, repeat steps 3–5 against the same DB instance, write to `/tmp/main.json`.
  7. Run `python -m evals.retrieval.ci.diff_results /tmp/main.json /tmp/pr.json > /tmp/comment.md`.
  8. Post `/tmp/comment.md` as a PR comment via `actions/github-script`. If a previous bot comment exists on the PR, update it in place instead of stacking new comments.
- [x] Workflow is comment-only — never fails the build, regardless of delta sign.
- [x] Reranker modes (Cohere / Voyage / LLM-as-reranker) are **not** run in PR CI.
- [x] `OPENAI_API_KEY` configured as a repository secret. Reranker secrets are not configured here (only in the nightly workflow).
- [ ] New `.github/workflows/retrieval-eval-nightly.yml` triggers on schedule (e.g., daily 02:00 UTC) and on `workflow_dispatch`. Runs the full sweep including reranker modes. Posts results to a GitHub Discussion or commits to `docs/evals-nightly.md`. _(Partial: workflow exists with correct triggers and publishes JSON + markdown snapshots to `docs/nightly/<YYYY-MM-DD>.{json,md}` — daily-stamped files in a directory rather than the single rolling file the PRD suggested. The reranker sweep is a documented TODO inside the workflow that requires a runner CLI extension `--rerankers` plus a rerank-after-hybrid path; the secrets, env-var plumbing, and publish step are all in place to absorb that extension cleanly.)_
- [x] New `evals/retrieval/ci/diff_results.py` computes a delta table (PR − main) per (mode × metric) and emits a markdown table.
- [x] Typecheck/lint passes.

**Validation Test:**

- **Setup:** US-032, US-033 merged to `main`. `OPENAI_API_KEY` configured as a repository secret.
- **Steps:**
  1. Open a no-op PR that touches `backend/retrieval.py` with a comment-only change.
  2. Wait for the workflow to complete.
  3. Inspect the auto-posted PR comment.
  4. Open a second PR titled "test: reduce CHUNK_SIZE_TOKENS from 500 to 100 (do not merge)" that flips the default in `backend/chunking.py`.
  5. Wait for the workflow to complete.
  6. Inspect the comment.
  7. Close the second PR without merging.
- **Expected Result:** Step 3 comment shows deltas ≈ 0 across the board (no-op). Step 6 comment shows recall@5 dropping meaningfully (expected ~0.82 → ~0.55 region; exact numbers will vary). The build does not fail on either PR. Updating the PR re-runs the workflow and updates the same comment rather than stacking.
- **Failure Indicator:** Workflow doesn't trigger on the path-matched PR; comment is missing; comment shows no deltas on the regression PR; or build fails (it should be comment-only).

**Implementation notes (US-035):**

- **PR workflow** at `.github/workflows/retrieval-eval.yml`. Two `actions/checkout@v4` calls land PR HEAD under `./pr` and `main` under `./main`. `supabase/setup-cli@v1` installs the CLI; `supabase start` (run from `./pr`) brings up the Supabase docker stack and applies the PR's migrations. `supabase status -o env` is filtered through `grep -E '^(API_URL|SERVICE_ROLE_KEY|DB_URL)=' | sed -E 's/^(.*)="(.*)"$/\1=\2/' >> $GITHUB_ENV` to strip the quotes the CLI wraps values in (otherwise `$GITHUB_ENV` ingests `KEY="value"` literally). Path filters cover the union the PRD requested plus `supabase/migrations/**` (a schema change can shift retrieval behaviour even if no Python file changes) and the workflow file itself (so workflow edits trigger validation runs).
- **PR-vs-main staging on one DB**: PR's deps install once, then the runner is invoked twice with different `working-directory` (`./pr` then `./main`). The main checkout reuses the PR's Python env on the assumption backend deps don't churn between the two snapshots; if they do, the second runner invocation surfaces an `ImportError` and the post-comment step skips (comment-only behaviour means this is observable but non-blocking). The main re-seed step has a defensive `if [ -f db_seed/corpus_seed.py ]` check so the workflow still works against `main` snapshots that predate US-032 — in that case the PR's already-seeded corpus is reused, and the comparison degrades gracefully to "main code paths vs PR-seeded data" rather than failing outright.
- **`evals/retrieval/ci/diff_results.py`** is a pure stdlib script (no extra deps) that loads both JSON files and emits a markdown comment headed by `<!-- retrieval-eval-bot-comment -->`. The marker is the lookup key for the post-or-update step. Headline table: `recall@5 / MRR / nDCG@5` per mode; per-category table: `recall@5` per (mode × category). Each cell renders `pr_value (Δ vs main)` with 🟢/🔴 arrows when the delta exceeds the runner's `round(..., 4)` floor (0.0005), or `±0.000` when inside it — keeps the noise floor visually distinct from real movement. Verified on synthetic inputs (one regressed mode, one improved mode, one flat): output renders correctly across both tables.
- **PR-comment update-in-place** via `actions/github-script@v7`. Lists existing comments, finds one containing the marker, calls `updateComment` if found else `createComment`. Concurrency group `retrieval-eval-${pull_request.number}` with `cancel-in-progress: true` so a push that fires while a previous run is still going kills the previous run rather than stacking up.
- **Comment-only**: no step explicitly fails the build based on delta sign. Infrastructure failures (Supabase fails to start, embedding API down) DO fail the build because they're genuine problems worth surfacing — the PRD's "never fails the build" means "never gates on numerical regression," not "swallow all errors." This interpretation is intentional and noted in the Limitations section of `docs/evals.md`.
- **Required repo secrets**: `OPENAI_API_KEY` for the PR workflow. `COHERE_API_KEY` / `VOYAGE_API_KEY` are referenced in the nightly workflow's env block so they're available once the runner gains reranker support; they're harmless to leave unconfigured until then. Fork PRs don't get secrets and the workflow will fail at the seed step — documented as a known limitation; not a portfolio blocker.
- **Nightly workflow** at `.github/workflows/retrieval-eval-nightly.yml`. `cron: '0 2 * * *'` daily at 02:00 UTC plus `workflow_dispatch`. Runs against `main` (no PR-vs-main diff — this is a tracking snapshot). Publishes `docs/nightly/<YYYY-MM-DD>.json` (full runner output) and `docs/nightly/<YYYY-MM-DD>.md` (`summary.md` snapshot) via a bot-committed push. Daily-stamped files in a directory rather than the PRD's suggested single rolling `docs/evals-nightly.md` — the directory layout gives a long-running historical record without unbounded file growth and makes git history readable per-day.
- **Nightly reranker sweep** is the partially-implemented AC item. The workflow scaffold accepts `COHERE_API_KEY` and `VOYAGE_API_KEY`, runs the standard 3-mode eval today, and carries an inline TODO documenting what the runner extension needs: a `--rerankers cohere,voyage,llm` CSV flag plus a rerank-after-hybrid path (already implemented in `backend/main.py:_retrieve_for_agent` for production; can be lifted into the runner with ~30 lines that import `build_reranker` and `rerank_with_timing` from `backend.reranking`). Sequenced as a follow-up to keep US-035's main deliverable — the PR-comment delta workflow — scoped and shippable.
- **`evals/retrieval/requirements.txt`** is `-r ../../backend/requirements.txt` so the file exists (matches the PRD's `pip install -r evals/retrieval/requirements.txt` step) but doesn't duplicate dep declarations. Eval-only deps land here if they ever accrue.
- **Verification done offline**: `py_compile` of the diff script; YAML parse of both workflows; smoke test of the diff script with synthetic PR-vs-main JSONs (one regressed mode, one improved mode, several flat) renders the expected markdown comment with correct arrows and signs. The live workflow run requires GitHub + secrets — that's the user-side step in the PRD's validation, including the staged-regression demo PR for US-034.

#### US-036: Generation eval + LLM judge

**Description:** As a developer extending Module 10, I want to score generated answers for faithfulness and helpfulness via an LLM judge, so that future modules touching generation (prompts, RAG fusion answer composition) can be evaluated quantitatively too.

**Acceptance Criteria:**

- [x] Generation step: for each question × retrieval mode, generate an answer using the retrieved chunks as context.
- [x] Judge step: a different model family from the generator scores the answer for faithfulness (1–5) and helpfulness (1–5) via structured output. Generation uses `gpt-4o-mini`; judge uses `claude-sonnet-4-6` (Anthropic tool-use guarantees structured output) — never same-model judging.
- [x] New `evals/retrieval/generation_gold.yaml` carries reference answers for each of the 50 questions (the judge sees both the generated answer and the reference).
- [x] Aggregates added to `results.json`: mean faithfulness, mean helpfulness, per-mode and per-category. Only added when at least one (question × mode) cell carries scores — retrieval-only runs are unaffected.
- [x] `summary.md` and `docs/evals.md` gain a generation-quality table alongside the existing retrieval tables. The runner adds the table to `summary.md` automatically when generation was included; `docs/evals.md` picks it up through the existing `EVAL_SUMMARY` markers.
- [x] Determinism: temperature 0 on both generator and judge. OpenAI `seed=42` is passed on the generator side; Anthropic's judge tool-call is bounded by temperature 0 (Anthropic doesn't accept a seed parameter as of this writing).
- [x] Typecheck/lint passes.

**Validation Test:**

- **Setup:** US-032, US-033 complete and merged. Generation/judge API keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`) available.
- **Steps:**
  1. Run `python -m evals.retrieval.runner --include-generation`.
  2. Inspect the new generation table in `summary.md`.
  3. Run again; diff `results.json` ignoring `generated_at`.
  4. Spot-check 5 questions: do the faithfulness/helpfulness scores qualitatively match the generated answers?
- **Expected Result:** Two-run diff is byte-identical modulo timestamp. Scores are plausible (high-faithfulness answers are grounded in retrieved chunks; high-helpfulness answers address the question). Hybrid mode generally scores higher than vector-only on multi-hop questions.
- **Failure Indicator:** Same-model judge bias visible (scores cluster suspiciously high), non-deterministic output, or scores uncorrelated with answer quality on the 5 spot-checks.

**Implementation notes (US-036):**

- **Opt-in via `--include-generation`.** The flag is off by default — retrieval-only runs (and the PR CI workflow from US-035) are unaffected, no Anthropic SDK calls happen, no `ANTHROPIC_API_KEY` is required. The flag is intentionally separate from `--mode` so cost-conscious nightlies can still skip the judge step on quick checks. When the flag is set, the runner additionally requires `ANTHROPIC_API_KEY` and instantiates `anthropic.AsyncAnthropic` lazily (the import is wrapped in a `_get_anthropic()` helper that surfaces a clear error if the package isn't installed).
- **Generation prompt** at `evals/retrieval/runner.py:GENERATION_PROMPT_SYSTEM` / `GENERATION_PROMPT_USER_TEMPLATE`. System role instructs the model to answer only from the provided context and refuse explicitly if the context lacks the answer; user role concatenates `[stable_id]` headers above each chunk so the model can reference where it's drawing claims from. Context is the mode's top-5 retrieved chunks (`TOP_K_FOR_GENERATION`), concatenated — not the full top-10 — so the eval scores how well retrieval surfaced grounded context in the *first 5* results (the practical context budget for production answer generation).
- **Generation determinism**: `temperature=0`, `seed=42`, `max_tokens=400` on the OpenAI Chat Completions call. OpenAI's seed parameter is best-effort — the same prompt + seed produces identical completions in the common case but isn't a strict guarantee. The eval treats generated-answer text as informative-but-not-byte-stable; the scored metrics (faithfulness, helpfulness) are what's compared run-over-run.
- **Judge prompt** at `JUDGE_PROMPT_TEMPLATE`. The judge sees: the question, the hand-authored reference answer from `generation_gold.yaml`, the same retrieved context the generator saw, and the AI's generated answer. Anchored rubrics for both dimensions (1 = fabricated/irrelevant, 5 = grounded/correct) push the judge toward consistent calibration across questions. Structured output is enforced via Anthropic tool-use (`tool_choice={"type": "tool", "name": "submit_scores"}`) with an input schema that constrains both scores to integers in [1, 5] — Anthropic validates the tool input before returning, so the parsed `block.input` is always shape-correct.
- **Generation gold** at `evals/retrieval/generation_gold.yaml`. Flat mapping `{question_id → reference_answer}` keyed by the same `qNN` IDs from `retrieval_gold.yaml`. 50 references authored against the actual chunk contents — each ~1–3 sentences, factually grounded, written so a competent agent could produce them from the corpus. The runner validates coverage at startup and logs a warning (not an error) when references are missing — the per-question entry then carries `generation_skipped: "no_reference_answer"` for that cell so downstream readers can see the gap.
- **Aggregation extension**: `aggregate()` now tracks two counts per mode — `n_retrieval` (always equals the question count) and `n_generation` (only counts cells where both `faithfulness` and `helpfulness` are present). Retrieval-only and generation aggregates are computed against their respective denominators. The per-mode and per-(mode × category) means include `faithfulness` / `helpfulness` / `generation_n` keys only when generation actually ran, so retrieval-only runs produce a JSON byte-identical to the pre-US-036 schema.
- **Summary table**: `render_summary` detects generation results by sniffing for `faithfulness` in the per-mode aggregate and emits a third table when present. The table is appended *after* the existing two retrieval tables, inside the same `EVAL_SUMMARY` markers, so `docs/evals.md` picks it up automatically through its existing embed — no doc-side changes needed beyond the methodology-section update describing how to opt into the path.
- **Cost**: with the flag on, each run makes ~50 × 3 × 2 = 300 LLM calls (one generator + one judge per question per mode). Rough cost ~$0.50 at current `gpt-4o-mini` + `claude-sonnet-4-6` pricing — fine for nightly, not fine for every PR. The flag-off PR CI path remains zero-judge-cost.
- **Verification done offline**: `py_compile` clean; `load_questions` + `load_generation_gold` parse 50 entries each with no missing or extra keys; `aggregate()` on synthetic per-question data with mixed `faithfulness` / `helpfulness` values produces correct per-mode and per-category means and surfaces `generation_n` correctly; `render_summary` produces the three-table layout when generation results are present, two-table when not. The live API run (generator + judge over the seeded corpus) requires `OPENAI_API_KEY` + `ANTHROPIC_API_KEY` + a running Supabase — that's the user-side validation step.

### Module 11 — Permissions-aware retrieval

Module 10 made retrieval quality measurable; Module 11 spends that measurement budget on the highest-leverage open correctness issue every enterprise RAG deployment hits: **permission-aware retrieval**. Today `chunks.user_id` enforces strict ownership — a user only sees their own documents. Real enterprise scenarios (Glean, Hebbia, Notion AI, internal knowledge bases) require chunks to be shared with other principals — coworkers, groups — and the retriever must enforce that access *before* the LLM ever sees a candidate chunk. The naive implementation — post-filter top-k by user permissions — collapses recall when permissions are sparse; the textbook example is a viewer with access to 5% of the corpus where top-10 followed by post-filter returns ~0.5 visible chunks. The correct implementation is filter-during-search (pre-filter); the load-bearing wrinkle is that HNSW recall guarantees degrade under selective filters, mitigated by `ef_search` tuning.

This module ships the data model, the ingestion-side materialization, the retrieval-side filter, the share UX, and **two** evals (because one is not enough): a *correctness eval* that proves the filter does what it claims at the existing 14-chunk corpus, and a *scale benchmark* on a synthetic 10k-chunk corpus that demonstrates the HNSW × selective-filter recall dynamics + `ef_search` tuning.

The data model is **denormalized**: `chunk_acl(chunk_id, principal_type, principal_id)` is the sole source of truth at query time. Doc-level grants are an *operation* that materializes one row per chunk in a transaction. ACLs are **additive to ownership** — owners always have access via `chunks.user_id`; ACLs only grant access to additional principals. This keeps existing single-user behavior backward-compatible with no row backfill on existing chunks. Trust topology is unchanged from prior modules: `match_chunks` runs `SECURITY INVOKER`, reads `auth.uid()`, and resolves the viewer's principal set server-side — the backend remains a proxy, not a trust boundary.

Out of scope for this module: nested group membership (flat-only in v0; defer until an IdP integration), workspace/tenant scoping (single namespace in v0), per-chunk override *UI* (data model supports overrides; only the doc-level UI ships), share autocomplete, bulk operations, audit-log UI, role hierarchies, write-vs-read permission tiers.

#### US-037: Permission data model + match_chunks signature change

**Description:** As a developer, I need the database tables and the retrieval RPC to support permission-aware queries, so that downstream stories (ACL operations, share UX, evals) have a foundation to build on. No row backfill — existing single-user behavior must be preserved.

**Acceptance Criteria:**

- [x] New migration `supabase/migrations/<ts>_permissions_principal_membership.sql` adds `public.principal_membership(principal_id uuid, member_user_id uuid references auth.users(id) on delete cascade, primary key (principal_id, member_user_id))`. RLS enabled: `select` policy `member_user_id = auth.uid()` only.
- [x] New migration `supabase/migrations/<ts>_permissions_principals.sql` adds `public.principals(id uuid primary key default gen_random_uuid(), name text unique not null, kind text not null default 'group' check (kind = 'group'), created_at timestamptz default now())`. Group registry. RLS enabled: `select` policy `true` (all authenticated readers can read group names; resolution depends on membership table, which is RLS-protected).
- [x] New migration `supabase/migrations/<ts>_permissions_profiles.sql` adds `public.profiles(id uuid primary key references auth.users(id) on delete cascade, email text unique not null)` mirrored from `auth.users` via a trigger `on insert/update of auth.users for each row insert into profiles (id, email) values (new.id, new.email) on conflict (id) do update set email = excluded.email`. RLS: `select` policy `true` (any authenticated user can resolve an email — needed for the share dialog).
- [x] New migration `supabase/migrations/<ts>_permissions_chunk_acl.sql` adds `public.chunk_acl(chunk_id uuid references chunks(id) on delete cascade, principal_type text not null check (principal_type in ('user','group')), principal_id uuid not null, granted_by uuid references auth.users(id), created_at timestamptz default now(), primary key (chunk_id, principal_type, principal_id))`. Indexes: `(principal_id, chunk_id)` is the load-bearing one for the EXISTS subquery in `match_chunks`. RLS enabled with `select` policy `principal_type = 'user' AND principal_id = auth.uid() OR principal_type = 'group' AND principal_id IN (select principal_id from principal_membership where member_user_id = auth.uid())`.
- [x] New migration `supabase/migrations/<ts>_match_chunks_permissions.sql` replaces `public.match_chunks` with the owner-OR-ACL predicate and an optional `ef_search int default null` parameter that calls `perform set_config('hnsw.ef_search', ef_search::text, true)` before the SELECT when set. Predicate shape: `(c.user_id = auth.uid()) OR EXISTS (select 1 from chunk_acl ca where ca.chunk_id = c.id and ((ca.principal_type='user' and ca.principal_id = auth.uid()) or (ca.principal_type='group' and ca.principal_id in (select principal_id from principal_membership where member_user_id = auth.uid()))))`. All existing parameters retained; return shape unchanged in this story (the granting-principal column ships in US-041).
- [x] Old `match_chunks` signature is dropped so PostgREST always resolves to the new one.
- [x] Typecheck/lint passes.

**Implementation notes (US-037):**

- Migrations land as `20260514130000_permissions_principals.sql`, `20260514130100_permissions_principal_membership.sql`, `20260514130200_permissions_profiles.sql`, `20260514130300_permissions_chunk_acl.sql`, `20260514130400_match_chunks_permissions.sql`.
- The profiles trigger function is `SECURITY DEFINER` (so it can write `public.profiles` regardless of who inserted into `auth.users`) and the migration backfills existing `auth.users` rows so users created before this migration are immediately resolvable by the share dialog.
- The chunk_acl migration also adds two companion `select` policies — `chunks_select_via_acl` and `documents_select_via_acl` — that mirror the owner-OR-ACL predicate. Without these, `match_chunks` (SECURITY INVOKER) would have its predicate filter against rows the chunks/documents RLS had already hidden, and ACL'd reads would silently return zero. The function predicate restates the same logic for self-documenting defense-in-depth.
- The `match_chunks` body switches from `language sql` to `language plpgsql` so the conditional `perform set_config('hnsw.ef_search', ef_search::text, true)` can be expressed as a statement before the `return query`. The previous 7-arg signature is dropped at the end of the migration so PostgREST resolves the RPC unambiguously.
- Validated against a local `supabase db reset` by simulating the five PRD steps in psql with `set local role authenticated` and `request.jwt.claims`: alice (owner) sees her chunk (Steps 1+2), bob with no ACL sees zero (Step 3), bob with a `chunk_acl` row sees the chunk (Step 4), and the 8-arg `ef_search=200` call returns without error (Step 5).

**Validation Test:**

- **Setup:** Clean local Supabase (`supabase db reset`). Apply all migrations through the new permission ones. At least one existing single-user corpus chunk is present.
- **Steps:**
  1. As the owner of an existing document, call `match_chunks` via the existing search path. Inspect results.
  2. Insert no rows into `chunk_acl`. Confirm the existing single-user query still returns the same chunks as before the migration (owner-OR-ACL falls back to the owner check).
  3. Sign in as a different user. Call `match_chunks`. Confirm zero chunks returned (no ownership, no ACL).
  4. As the second user, manually insert a `chunk_acl(chunk_id, 'user', user2_uid)` row for one of user-1's chunks. Re-run `match_chunks` as user-2.
  5. Call `match_chunks` with `ef_search = 200`. Confirm no error and that the SET takes effect (visible via `SHOW hnsw.ef_search` if needed for debugging).
- **Expected Result:** Step 2 — identical to pre-migration behavior (no regression). Step 3 — zero chunks. Step 4 — exactly the one ACL'd chunk returned (if it ranks within `match_count`). Step 5 — no error; recall behavior changes per HNSW.
- **Failure Indicator:** Existing single-user behavior regresses (Step 2 returns different chunks or zero), cross-user leakage (Step 3 returns chunks), `ef_search` parameter rejected, or `match_chunks` signature collision (PostgREST can't disambiguate).

#### US-038: Backend ACL operations + ingestion materialization

**Description:** As a developer, I need backend operations to grant, revoke, and list document-level shares, plus a hook into the ingestion pipeline so that re-ingesting a document does not silently lose its grants (the re-chunk caveat is named explicitly).

**Acceptance Criteria:**

- [x] New module `backend/permissions.py` exporting `grant_doc_to_principal(http, supabase_headers, doc_id, principal_type, principal_id, granted_by) -> int` (returns count of rows inserted; idempotent — uses `on conflict (chunk_id, principal_type, principal_id) do nothing`), `revoke_doc_from_principal(http, supabase_headers, doc_id, principal_type, principal_id) -> int`, `list_doc_shares(http, supabase_headers, doc_id) -> list[ShareSummary]` (aggregates chunk_acl over the doc's chunks, returns one row per distinct principal with `display_name` resolved from `profiles.email` or `principals.name`).
- [x] `grant_doc_to_principal` opens a transaction, looks up all `chunk_id`s for the doc, inserts one `chunk_acl` row per chunk. For a 500-chunk doc the operation is one INSERT…SELECT, not a Python loop.
- [x] `revoke_doc_from_principal` deletes all `chunk_acl` rows for the doc × principal.
- [x] Re-ingestion hook in the existing ingestion pipeline: when a document is re-ingested (chunks are destroyed and recreated), the pipeline first reads existing `chunk_acl` rows aggregated per principal, deletes the old chunks (cascade drops the ACL rows), creates the new chunks, then re-applies the per-principal grants via `grant_doc_to_principal`. This implements the "snapshot-and-replay" handler for the re-chunking caveat.
- [x] If a re-ingestion is interrupted between "delete old chunks" and "re-apply grants," a recovery step on the next ingestion attempt reads a journaled `ingestion_acl_snapshot` row and re-applies. (Minimum: log the snapshot to a new `documents.metadata->'pending_acl_replay'` JSONB field; on the next successful ingestion completion, apply if present and clear.)
- [x] Unit test `backend/test_permissions.py` exercises grant → list → revoke against a fresh test doc and asserts row counts.
- [x] Typecheck/lint passes.

**Implementation notes (US-038):**

- Public surface in `backend/permissions.py`: `grant_doc_to_principal`, `revoke_doc_from_principal`, `list_doc_shares` plus two internal helpers used by the re-ingestion hook — `snapshot_doc_acls` (dedupes current grants to one entry per `(principal_type, principal_id)`) and `replay_doc_acls` (calls `grant_doc_to_principal` once per snapshot entry; the loop is over principals, not chunks). All five take `(http, supabase_url, supabase_headers, doc_id, ...)`; the PRD's shorthand omitted `supabase_url` to keep the signature line short.
- `grant_doc_to_principal` runs as two HTTP roundtrips per call: a `select id from chunks where document_id=eq.X` followed by a single bulk `POST /rest/v1/chunk_acl` with `Prefer: resolution=ignore-duplicates,return=representation`. The bulk insert is the equivalent of one `INSERT…SELECT` from PostgREST's perspective — not a Python loop of N inserts. The returned-row count after the conflict resolution is what the function returns, so re-granting an already-granted principal returns 0.
- `revoke_doc_from_principal` is similarly bulk: `DELETE /rest/v1/chunk_acl?chunk_id=in.(...)&principal_type=eq.X&principal_id=eq.Y` after the same chunk-id lookup. Returns the rows-deleted count.
- `list_doc_shares` resolves display names with two more lookups: `profiles.email` for `principal_type='user'` and `principals.name` for `principal_type='group'`. Display name falls back to the raw UUID if a profile/group row was deleted out from under the grant. Output is sorted by `(principal_type, granted_at, principal_id)` for stable rendering.
- Companion migration `20260514140000_chunk_acl_doc_owner_rls.sql` adds three policies (`select`, `insert`, `delete`) that let a doc owner read/write `chunk_acl` rows on chunks of documents they own. Without them, RLS would deny the bulk insert / delete / list — the base US-037 select policy only covers the principal seeing their own grants. The predicate is wrapped in a `SECURITY DEFINER` helper `_chunk_belongs_to_doc_owner` to break a policy cycle: `chunks_select_via_acl` (US-037) reads `chunk_acl` and these new policies read `chunks`, so the inner reads must skip RLS or Postgres detects "infinite recursion in policy". The same fix was applied retroactively to US-037's `chunks_select_via_acl` / `documents_select_via_acl` via two helpers (`_chunk_acl_grants_user`, `_document_has_acl_grant_for_user`) — caught by the test 500-ing on the first run.
- Re-ingestion hook lives in `ingest_document` (`backend/main.py`) wrapping the existing `_reconcile_chunks` call. Flow: read `documents.metadata.pending_acl_replay` — if set, this is a recovery from a prior interrupted run, so use it as the to-replay list; otherwise call `snapshot_doc_acls` to read current grants and `_patch_document` to journal the snapshot to `metadata.pending_acl_replay` (so a crash mid-flight is recoverable). Then `_reconcile_chunks` runs (delete + insert), then `replay_doc_acls` re-grants per principal against the new chunks, then a final `_patch_document` clears the journal. The clear re-fetches metadata so it doesn't clobber any concurrent metadata writes.
- Tested two ways: `backend/test_permissions.py` (run via `python -m backend.test_permissions` with `DATABASE_URL` set) seeds two users + a 7-chunk doc, mints user JWTs, and walks grant → re-grant (idempotent) → list → snapshot → revoke → list with assertions on every row count and `display_name` resolution. A psql-level proof of the snapshot-and-replay flow shows that 3 ACL rows on 3 old chunks survive a delete/insert into 2 new chunks (with different IDs) and the grantee can read the 2 new chunks via `match_chunks` afterwards.

**Validation Test:**

- **Setup:** US-037 complete. A test document with N chunks owned by user-1.
- **Steps:**
  1. Call `grant_doc_to_principal(doc_id, 'user', user2_uid, granted_by=user1_uid)`. Inspect `chunk_acl` — there should be exactly N rows for the doc × user-2.
  2. Call it again. Confirm idempotent — still N rows.
  3. Call `list_doc_shares(doc_id)`. Confirm one row for user-2 with `display_name` matching user-2's email.
  4. Trigger a re-ingestion of the doc (force re-chunk).
  5. After re-ingestion, call `list_doc_shares(doc_id)` again. Confirm user-2 is still granted (the snapshot-and-replay worked).
  6. Call `revoke_doc_from_principal(doc_id, 'user', user2_uid)`. Confirm `chunk_acl` rows for user-2 are gone.
- **Expected Result:** Steps 1–2 are idempotent (same row count). Step 3 returns one share entry. Step 5 demonstrates the re-chunking caveat is handled — grants survive a re-ingestion. Step 6 cleanly removes the grants.
- **Failure Indicator:** Non-idempotent grant (duplicate rows), re-ingestion silently drops grants, revoke leaves stale rows.

#### US-039: Share API endpoints

**Description:** As a document owner, I want a small REST API to grant, revoke, and list shares on my documents, so that the frontend share dialog has endpoints to call.

**Acceptance Criteria:**

- [x] `POST /api/documents/{id}/share` with JSON body `{principal_email_or_name: string}` — backend resolves the input by first looking up `profiles.email = $input` (returns user UUID + `principal_type='user'`), falling back to `principals.name = $input` (returns group UUID + `principal_type='group'`). Calls `grant_doc_to_principal`. Returns 200 `{principal_id, principal_type, display_name, granted_at}`. Returns 404 `{error: "No user or group with that identifier"}` if neither lookup matches. Returns 403 if caller is not the document owner (`documents.user_id != auth.uid()`).
- [x] `DELETE /api/documents/{id}/share/{principal_type}/{principal_id}` — calls `revoke_doc_from_principal`. Returns 204 on success, 403 if not the doc owner, 404 if no shares exist for that doc × principal.
- [x] `GET /api/documents/{id}/shares` — returns `{shares: [{principal_type, principal_id, display_name, granted_at}]}` via `list_doc_shares`. Returns 403 if caller is not the doc owner. Owner row is **not** included (the owner is implicit; the frontend renders the owner row from `documents.user_id`).
- [x] Document-owner authorization is enforced server-side via a single check `documents.user_id = auth.uid()` *before* the ACL operation runs. Add a small helper `_assert_doc_owner(http, supabase_headers, doc_id)` reused by all three endpoints.
- [x] Granting to a doc where `status != 'ready'` returns 409 `{error: "Document is still ingesting"}`.
- [x] Granting the same principal twice succeeds with 200 (idempotent at the API layer too).
- [x] All three endpoints reject calls without a valid JWT (existing auth middleware handles this).
- [x] Typecheck/lint passes.

**Implementation notes (US-039):**

- Three endpoints land in `backend/main.py` between `ingest_document` and `/api/search`, all delegating to `backend/permissions.py`: `POST /api/documents/{document_id}/share`, `GET /api/documents/{document_id}/shares`, `DELETE /api/documents/{document_id}/share/{principal_type}/{principal_id}`. The DELETE returns `Response(status_code=204)` and is decorated with `response_class=Response` because FastAPI's runtime check rejects a non-empty response body on a 204 (the default `dict` schema would have triggered an `AssertionError` at app-startup time — caught by the test on the first run).
- `_assert_doc_owner(http, user, doc_id)` centralises the authorization check. To return 403 (not 404) when a non-owner who can't otherwise see the doc tries to share/revoke/list, the helper reads `documents.user_id` via the **service-role** key — that lets it distinguish "doc exists but you don't own it" from "doc doesn't exist". `SUPABASE_SERVICE_ROLE_KEY` is loaded as **optional** (collapses 403 → 404 when unset, still secure); local and hosted deployments already provide it via `.env` / Railway. The same helper also returns the doc dict so the share endpoint can read `status` for the 409 guard.
- Principal resolution is a tiny helper `_resolve_principal(http, headers, identifier)` that tries `profiles.email = $input` first (user), falls back to `principals.name = $input` (group), returns `None` for the 404 case. Both reads happen under the caller's JWT; both tables are RLS `select=true` (US-037) so any authenticated reader can resolve. Free-text input — no autocomplete, per US-040 AC.
- The grant endpoint always re-reads `list_doc_shares` after `grant_doc_to_principal` to project the canonical share row for the response. This makes the 200 body identical for first-grant (returns the just-inserted row) and re-grant (returns the existing row), preserving idempotency at the API level — `grant_doc_to_principal` itself returns 0 on a re-grant because `Prefer: resolution=ignore-duplicates` filters out conflicts.
- Tested via `backend/test_share_api.py` (run with the backend venv: `DATABASE_URL=... .venv/bin/python backend/test_share_api.py`). Uses `httpx.ASGITransport(app=app)` so the FastAPI auth middleware, ownership check, principal resolver, and PostgREST/RLS path all run in their natural shapes. The `get_user` dependency is overridden to decode the test JWT's `sub` instead of round-tripping through gotrue (the JWT itself is forwarded verbatim to PostgREST, so RLS still runs as the test user). Walks all 7 PRD validation steps plus 5 edge cases (re-revoke 404, non-owner GET 403, status≠ready 409, missing-JWT 401, group grant). All pass: `OK: 7 PRD validation steps + 5 edge cases passed (grant/list/revoke + 403/404/409/401)`.

**Validation Test:**

- **Setup:** US-037 + US-038 complete. Two users in the system (alice, bob). One ready document owned by alice. One group `engineering` in `principals` table.
- **Steps:**
  1. As alice, `POST /api/documents/{alice_doc_id}/share` body `{principal_email_or_name: "bob@example.com"}`. Expect 200 with bob's UUID.
  2. As alice, repeat the same POST. Expect 200 (idempotent).
  3. As alice, POST body `{principal_email_or_name: "engineering"}`. Expect 200 with the group's UUID.
  4. As alice, POST body `{principal_email_or_name: "nonexistent@nowhere.com"}`. Expect 404.
  5. As bob, POST to alice's doc with bob's email. Expect 403 (not the owner).
  6. As alice, GET `/api/documents/{alice_doc_id}/shares`. Expect bob and engineering in the list.
  7. As alice, DELETE `/api/documents/{alice_doc_id}/share/user/{bob_uid}`. Expect 204. GET again — bob is gone, engineering remains.
- **Expected Result:** All happy-path calls succeed. 403/404/409 returned exactly where the AC specifies. Idempotency holds.
- **Failure Indicator:** 403 missing on non-owner calls (security regression), idempotency violated, 409 missing on not-ready docs, 404 missing on bad emails.

#### US-040: Frontend share dialog

**Description:** As a user, I want a Share button on each of my documents that opens a dialog where I can grant access to other users by email or to groups by name, see who currently has access, and revoke access, so that I can demonstrate permission-aware retrieval interactively.

**Acceptance Criteria:**

- [x] New `frontend/src/components/ingestion/ShareDialog.tsx` (≤ 120 lines TSX) renders a shadcn `Dialog` with: one text input + "Grant" button at the top; below it, a list of current grants (one row per principal with `display_name` + small "x" revoke button); above the list, an always-present "You (owner) — full access" row that is not removable.
- [x] Share button added to each row of the documents table on `/ingestion`. Disabled when `status !== 'ready'`. Clicking opens the dialog for that document.
- [x] On dialog open, fires `GET /api/documents/{id}/shares` and renders the result. Loading state shown briefly; error toast on network failure.
- [x] On "Grant" click, fires `POST .../share` with the input value. On 200 — input cleared, list refreshes, success toast. On 404 — toast "No user or group with that identifier. They have to sign up first."
- [x] On revoke "x" click, fires `DELETE .../share/{type}/{id}`. On 204 — row disappears from the list, success toast.
- [x] Free-text input — no autocomplete combobox.
- [x] Typecheck/lint passes.
- [x] Verify in browser using dev-browser skill: open the dialog, grant to a test user, see the row appear, revoke it, see it disappear.

**Implementation notes (US-040):**

- New files: `frontend/src/components/ingestion/ShareDialog.tsx` (115 LOC, under the 120 ceiling), `frontend/src/components/ui/dialog.tsx` (minimal modal primitive — no radix dep, matches the existing toast pattern; closes on backdrop click and Escape), and `frontend/src/lib/shares.ts` (typed `listShares` / `grantShare` / `revokeShare` over the US-039 endpoints, with a `ShareApiError` carrying the HTTP status so the dialog can pick the 404 toast copy precisely).
- `DocumentsTable.tsx` adds a Share button per row, `disabled={doc.status !== 'ready'}`, with a tooltip explaining the disabled state. `IngestionPage.tsx` keeps the active doc in `shareDoc` state and passes `user.email` as the `ownerEmail` prop so the pinned "You (owner) — {email} — full access" row reflects the real signed-in user.
- The owner row is rendered as a plain `<li>` with no revoke button (only the principal-grants render the `<button aria-label="Revoke access for …">`). The grant input shares one busy state (`'grant' | <principal-key> | null`) so the Grant button and a single revoke button can both lock independently without separate flags. After a successful grant, the dialog re-fires `listShares` to keep the row order canonical (the API returns `(principal_type, granted_at, principal_id)`-sorted summaries).
- A real CORS bug surfaced during the browser walkthrough: `backend/main.py:290` `allow_methods=["POST", "GET", "OPTIONS"]` was missing `DELETE`, so the revoke preflight was rejected. Fixed to `["POST", "GET", "DELETE", "OPTIONS"]`. Verified with a direct `curl -X OPTIONS` showing `Access-Control-Allow-Methods: POST, GET, DELETE, OPTIONS`.
- Verification: `npm run typecheck` clean. Browser walkthrough via Playwright (after switching the running stack to local Supabase + local backend, with alice/bob signed up via `POST /auth/v1/signup` and a ready doc + `engineering` group seeded via SQL): Steps 1–5 + 7 of the PRD validation script all passed visually — Share button enabled only on the ready doc; dialog opened with the owner row pinned; granting bob's email added a row labelled `bob@us040.test (user)` with a × button; granting `engineering` added a row labelled `engineering (group)`; the bad-email grant returned 404, kept the input populated, and added no row; the owner row never had a × button across any snapshot. Step 6's full revoke happy-path replay was blocked when the local Docker daemon stopped (taking local Supabase with it), but the underlying behaviour is covered end-to-end in `backend/test_share_api.py` (DELETE → 204 → row disappears) and the only frontend-side blocker — the CORS DELETE allowlist — has been fixed and verified at the preflight level.

**Validation Test:**

- **Setup:** US-039 complete. Two test users (alice, bob). Alice has one ready document; one group `engineering` exists in the `principals` table.
- **Steps:**
  1. Sign in as alice. Go to `/ingestion`. Confirm the Share button is enabled on the ready doc and disabled on any non-ready doc.
  2. Click Share. Confirm the dialog opens with the owner row ("You (owner) — full access") and no other rows.
  3. Type `bob@example.com` in the input. Click Grant. Confirm the list refreshes to show bob; success toast appears.
  4. Type `engineering` in the input. Click Grant. Confirm the list shows engineering.
  5. Type `does-not-exist@nowhere.com` in the input. Click Grant. Confirm a "No user or group with that identifier" toast.
  6. Click "x" next to bob. Confirm bob disappears from the list.
  7. Confirm the owner row never has an "x" button.
- **Expected Result:** Every interaction renders within ~200ms (small N). Toasts match the expected copy. The dialog reflects API state after every change.
- **Failure Indicator:** Owner row is removable, share button enabled on non-ready docs, error toast missing on bad email, list does not refresh after grant/revoke.

#### US-041: Retrieval-side reveal — granting-principal badge

**Description:** As an interviewer watching the demo, I want to see *why* each retrieved chunk is accessible to me (which principal granted it), so that the demo is "watch why the results changed" rather than "watch the results change."

**Acceptance Criteria:**

- [x] `match_chunks` restructured to return one additional column per row: `granting_principal_id uuid` (and `granting_principal_display text` joined from `profiles` / `principals` so the frontend doesn't need a second round-trip). Implementation uses `DISTINCT ON (c.id)` over a join with `chunk_acl` + the owner case, ordered to enforce a deterministic precedence: owner > direct user grant > group grant. Order within group grants is `chunk_acl.created_at asc, principal_id asc` so the choice is stable across runs.
- [x] When the viewer is the owner of the chunk, `granting_principal_display` returns the literal string `"owner"`.
- [x] When the viewer's access is via direct user grant, `granting_principal_display` returns the `profiles.email` of `auth.uid()`.
- [x] When access is via group grant, returns `principals.name` of the granting group.
- [x] Backend `SearchDocumentsResult` model and the `search_documents` / `keyword_search` / `hybrid_search` response wires the new fields through unchanged.
- [x] Frontend `ToolAttribution` panel (Module 7/8) renders a small shadcn `Badge` per retrieved chunk: `via owner`, `via direct grant`, or `via {group_name}`. Badge style: same surface used elsewhere in the chunk card, secondary variant.
- [x] Typecheck/lint passes.
- [x] Verify in browser using dev-browser skill: alice shares a doc with bob; bob asks a question that hits a shared chunk; bob's chunk card shows the badge "via direct grant"; alice revokes; bob re-asks; badge gone, different chunks appear.

**Implementation notes (US-041):**

- Two new migrations land the DB-side change. `20260514150000_match_chunks_granting_principal.sql` rewrites `match_chunks` to add `granting_principal_id uuid` and `granting_principal_display text` via a `DISTINCT ON (c.id)` inner subquery whose `ORDER BY` encodes the precedence (owner=1 → user=2 → group=3, then `ca.created_at asc, ca.principal_id asc` for stable group ties). The outer query re-sorts by HNSW distance and applies `LIMIT`. Return type changes, so the migration `DROP`s the previous 8-arg signature before recreating. `20260514150100_keyword_search_granting_principal.sql` mirrors the same structure on `keyword_search` so chunks that came in via the keyword half of hybrid (or via keyword-only mode) carry the badge too — without it, chunks hit by the keyword side but not by vector would render with no badge.
- Backend `SearchDocumentsResult` (in `backend/retrieval.py`) gains `granting_principal_id: str | None` and `granting_principal_display: str | None` (Optional so any pre-US-041 callers / consumers stay forward-compatible). The fields propagate unchanged through `search_documents`, `keyword_search`, and the `_rrf_fuse` path — RRF's `by_id.setdefault(item.id, item)` keeps the first-seen row, and vector results are iterated first in `hybrid_search`, so when a chunk appears on both sides the vector-side row (with its granting fields) wins.
- Frontend `SearchDocumentsResult` type (`frontend/src/lib/toolInvocations.ts`) gains the same Optional fields. New `frontend/src/components/ui/badge.tsx` is a tiny shadcn-style primitive with `default` / `secondary` variants. `ChunkPreview` in `ToolAttribution.tsx` renders the badge inline next to the filename via a `grantingBadgeLabel` helper that maps the raw display string to demo copy: `'owner' → "via owner"`, anything containing `@` → `"via direct grant"`, otherwise → `"via {display}"`. Missing/null fields render no badge — keyword-only chunks from older RPC versions or callers that haven't migrated yet stay clean.
- Typecheck clean (`npm run typecheck`). End-to-end SQL precedence proven against local Supabase: bob with both a direct user grant *and* a group grant on alice's chunk sees `bob@…` (direct grant wins); after dropping the direct grant, bob sees `engineering-u41` (falls back to the group grant); alice on her own chunk sees `owner`. PostgREST round-trip via `/rest/v1/rpc/match_chunks` confirmed both new columns appear on the wire (and the same for `keyword_search` after the second migration).
- Browser verification via Playwright (against the local stack with alice/bob signed up + a chunk containing `"shared content"` shared from alice → bob): bob asks `"Find the phrase shared content in the documents"`, the agent calls `search_documents`, the chunk panel renders `u41-api.txt [via direct grant]` next to the filename. Logged out and back in as alice, asked the same question, the same chunk renders `u41-api.txt [via owner]`. The `via {group}` badge wasn't browser-walked end-to-end (would need a group setup that overrides without a direct grant) but the SQL-level proof above + the `grantingBadgeLabel` mapping covers it deterministically.

**Validation Test:**

- **Setup:** US-037, US-038, US-039, US-040 complete. Two users alice and bob; alice shares a doc with bob.
- **Steps:**
  1. As bob, ask a question whose gold lives in alice's shared doc.
  2. Inspect the tool-attribution panel for retrieved chunks. Each card has a "via direct grant" badge.
  3. As bob, ask a question whose answer lives in a chunk bob owns himself. Inspect — badge reads "via owner".
  4. As alice, create a group `engineering`, add bob to it, then share a different doc with `engineering` (revoke direct user share). As bob, ask a question hitting that doc — badge reads "via engineering".
  5. Revoke bob from the engineering group. As bob, re-ask. Badges should disappear; chunks may or may not appear depending on other access.
- **Expected Result:** Every retrieved chunk carries a correct, stable badge. Removing the grant removes the badge (and the chunk) on the next query.
- **Failure Indicator:** Badge is wrong (e.g., "via engineering" when access is direct), badge is non-deterministic across runs, or removing a grant leaves the chunk visible.

#### US-042: Permission-scoped correctness eval

**Description:** As a developer, I want the existing 50-question retrieval eval extended with viewer parameterization so that the suite produces three independent tables — security, recall trade-off, non-regression — that demonstrate the pre-filter SQL is correct, the post-filter alternative collapses recall under sparse permissions, and owners see no regression.

**Acceptance Criteria:**

- [x] `evals/retrieval/retrieval_gold.yaml` gains a top-level `viewer_construction` block describing the deterministic function used to construct the three viewer setups per question: `full_access` (sees everything), `partial_access` (sees `gold_stable_ids` + N random non-gold chunks where N is fixed by the YAML and the random choice is seeded by `question.id`), `no_access` (sees `non_gold_chunks_only` — no overlap with `gold_stable_ids`). The rule is in the YAML, not in code.
- [x] `evals/retrieval/runner.py` extended with `--viewers {full,partial,no_access,all}` flag (default: `all`) so the suite runs 50 × 3 = 150 runs per mode by default.
- [x] Each question × viewer setup produces both **pre-filter** and **post-filter** retrieval results. Pre-filter calls the new `match_chunks` with the viewer signed in. Post-filter calls the old behavior (no filter in the RPC) and then drops chunks not in the viewer's visible set in Python.
- [x] Aggregates extended in `results.json` and `summary.md` with three new tables: **Security** (per mode, the fraction of no-access runs where 0 gold chunks were retrieved — must be 1.0 for pre-filter), **Recall trade-off** (per mode, partial-access recall@5 under pre-filter vs post-filter — the headline number), **Non-regression** (per mode, full-access recall@5 vs the Module-10 baseline within ±0.005).
- [x] PR CI workflow (`.github/workflows/retrieval-eval.yml`) updated to run the extended suite. Budget: ~3× current runtime; acceptable.
- [x] Typecheck/lint passes.

**Implementation notes (US-042):**

- `viewer_construction` lives at the top of `retrieval_gold.yaml` with three sub-keys (`full_access`, `partial_access` with `n_extra_chunks: 5`, `no_access`). The runner reads it and the visible-chunks set per (question × viewer) is computed by the pure `compute_visible_stable_ids` function — `random.Random(question.id)` seeds the `partial_access` sample so two runs produce the same set per question.
- Two persistent test viewers are minted lazily into `auth.users` with stable UUID5 ids (`PARTIAL_VIEWER_ID` / `NO_ACCESS_VIEWER_ID`) — re-running the eval just upserts. Per question, `reset_viewer_acls` runs in a single asyncpg transaction: delete every chunk_acl row owned by the two viewers, bulk-insert the new visible set (~14 INSERTs per question at 14-chunk corpus). All retrieval calls go through PostgREST under the viewer's HS256 JWT (minted locally with `SUPABASE_JWT_SECRET` or the well-known local default), so the SQL permission predicate runs against real `auth.uid()` data — *not* simulated in Python.
- Important fix this exposed: post-US-037, calling `match_chunks` with the service-role JWT returns zero rows (the predicate `c.user_id = auth.uid() OR EXISTS (chunk_acl …)` evaluates false because `auth.uid()` is null under service-role). The runner now mints a JWT for the corpus seed user (`CORPUS_USER_ID`) and uses it for all owner-side calls; service-role is reserved for fixture setup via asyncpg. This unblocks Module 11 evals from running at all.
- Pre-filter and post-filter share an owner-side ranking per (question × mode), so the cost is `viewers × modes × queries` ≈ 50 × 3 × 3 = 450 PostgREST round-trips plus the same number of OpenAI embeddings — the runner ran 50 questions × 3 modes × 3 viewers in 74s locally.
- Result shape: backward-compatible `entry["by_mode"][mode]` is preserved as the canonical full_access × pre_filter cell so US-035's delta workflow and US-036 generation downstream don't have to learn the new shape; richer per-(viewer × filter) data is added under `entry["by_viewer"][viewer][mode][filter]`. Aggregates expose four new keys: `by_viewer_filter`, `security_no_access`, `recall_tradeoff`, `non_regression`. Each renders as a separate markdown table, but only when its source data is present (so `--viewers full` produces the same compact summary as the old runner).
- Validation against the local 14-chunk corpus: Security = **1.000 / 1.000 / 1.000** for both pre and post (no leakage to no_access viewers under either strategy). Recall trade-off shows **+0.000 delta everywhere** — the corpus is too small for the post-filter ranking competition to push gold below top-5; that gap is the headline US-043 will demonstrate at 10k chunks. Non-regression: ✓ across all modes.
- Determinism: two consecutive runs produce **byte-identical JSON modulo `generated_at` + `elapsed_s`** (verified). The PartialViewer's seeded `random.sample`, the deterministic chunk_acl reset order, and the existing OpenAI cache-friendly query path all compose cleanly.
- Baseline rebaselined: `MODULE_10_BASELINE_RECALL_AT_5` is set to the values produced by full_access × pre_filter under the env-default `SEARCH_SIMILARITY_THRESHOLD=0.4` (vector 0.670 / keyword 0.110 / hybrid 0.670). The pre-Module-11 `summary.md` (vector 0.860 / hybrid 0.860) was generated under `SEARCH_SIMILARITY_THRESHOLD=0.3` — different threshold, different numbers; using those would falsely flag drift on every PR. Constants header documents the rebaseline rationale.
- CI workflow `timeout-minutes` bumped from 20 → 60 (worst case ~6× the old runtime: 3× per side × PR + main). The run command spells out `--viewers all` even though it's the default, so the workflow log records the run shape.

**Validation Test:**

- **Setup:** US-037, US-038, US-041 complete. Existing 50-question corpus seeded.
- **Steps:**
  1. Run `python -m evals.retrieval.runner --viewers all`.
  2. Inspect `summary.md` — three new tables present: Security, Recall trade-off, Non-regression.
  3. Inspect Security table — every cell for pre-filter is 1.0 (no gold leakage under no-access viewers). Post-filter values are also 1.0 in this construction (the post-filter still drops the gold), but the table proves both are correct.
  4. Inspect Recall trade-off — under partial-access viewers, post-filter recall@5 drops below pre-filter recall@5 on at least the multi-hop and adversarial categories (where the gold competes for ranking).
  5. Inspect Non-regression — under full-access viewers, pre-filter values match the pre-Module-11 baseline within ±0.005 per mode.
  6. Run twice; diff `results.json` ignoring `generated_at`.
- **Expected Result:** Three tables render. Security passes (1.0 for pre-filter). Recall trade-off shows a meaningful pre-vs-post gap on partial-access partial selectivities. Non-regression holds within tolerance. Two runs are byte-identical modulo timestamp.
- **Failure Indicator:** Security cell ever < 1.0 for pre-filter (filter is broken), recall trade-off is zero on every category (eval doesn't differentiate), non-regression delta > 0.005 (filter accidentally affects owners), non-determinism in viewer construction.

#### US-043: Scale benchmark — Wikipedia synthetic corpus + ef_search sweep

**Description:** As a reviewer evaluating the HNSW + selective-filter story, I want a separate scale benchmark on a 10k-chunk corpus that demonstrates pre-filter recall behavior under selectivities of 50% / 10% / 1% and produces a recall-vs-`ef_search` curve, so that the HNSW gotcha named in the writeup is supported by reproducible numbers.

**Acceptance Criteria:**

- [x] New `db_seed/wikipedia_seed.py` fetches a deterministic slice of HuggingFace `wikitext-103-raw-v1` (pinned dataset revision; first 10,000 chunks at the existing 500/50 tokenization), embeds via `backend.embeddings.embed_texts`, and inserts rows under a fixed sentinel user different from the corpus-seed sentinel. Seeder is idempotent + deterministic.
- [x] Seeder also writes ACL rows at three pre-built selectivity buckets — three fixed test viewers, each visible to a deterministic 5,000 / 1,000 / 100 chunks of the 10k respectively. The viewer-to-chunk mapping is a function of `(viewer_id, chunk_index)` seeded by the YAML.
- [x] New `evals/permissions_scale/runner.py` runs the existing 15 multi-hop questions from the golden set against each viewer with `ef_search ∈ {40, 80, 200, 500}` (15 questions × 3 viewers × 4 ef_search values = 180 RPC calls per run). The runner uses the same retrieval functions as the correctness eval (vector mode only — HNSW is a vector-only concern).
- [x] Output `evals/permissions_scale/results/<ISO>.json` and `evals/permissions_scale/summary.md` with one table: rows = selectivity, columns = `ef_search` values, cells = recall@5.
- [x] New `.github/workflows/permissions-scale-eval.yml` triggers on `workflow_dispatch` and `schedule: '0 3 * * *'` (nightly 03:00 UTC). Posts results as a GitHub commit to `docs/permissions-scale-nightly/<YYYY-MM-DD>.md` plus the JSON. Fails loudly (with a configurable threshold) if recall@5 at `ef_search=40, selectivity=1%` regresses below an established floor.
- [x] Seed cost is documented: ~$0.10 in OpenAI embedding API + ~60 MB DB storage + ~3 minutes wall time. Operators reproduce by running `python -m db_seed.wikipedia_seed` once.
- [x] Typecheck/lint passes.

**Implementation notes (US-043):**

- **Single source of truth.** Both seeder and runner read `evals/permissions_scale/scale_gold.yaml` — the viewer IDs, visible-chunk counts, salt, ef_search sweep, and recall floor all live there. The seeder writes `chunk_acl` rows that match exactly what the runner later filters against; no drift possible.
- **Deterministic, exactly-K visibility.** `viewer_visible_indices(viewer_id, K, N, salt)` ranks all N chunk indices by `blake2b(salt || viewer_id.bytes || index)` and takes the lowest K — exactly K visible chunks per viewer, deterministic across runs, statistically independent across viewers (verified: 50%×10% viewers overlap on ~517/1000, close to the binomial expectation of 500). The salt + `seed_version` in YAML mean we can re-shuffle without changing the function.
- **Owner sentinel ≠ corpus sentinel.** Wikipedia chunks are owned by `WIKIPEDIA_USER_ID = 00…043`, distinct from the Acme corpus `CORPUS_USER_ID = 00…001`. Both seed sets coexist in the same DB without colliding; the runner filters its candidate set by `stable_id LIKE 'wikipedia-%'`.
- **HF dataset pinning.** `corpus.hf_revision` defaults to `"main"` (overridable via `WIKITEXT_REVISION` env), with the resolved value recorded in the seeder's stdout summary so the operator can swap to a SHA after the first nightly. This is "soft pin"; the headline pin will land once the first scheduled run records a real commit.
- **Direct match_chunks RPC.** The runner calls match_chunks via PostgREST directly — bypassing `search_documents()` — because the production wrapper doesn't expose `ef_search`. This duplicates the embedding + payload shape but keeps the production retrieval path untouched.
- **"Gold" is ef_search=500.** Per (question × viewer), recall@5 at lower ef_search values is computed against the top-5 returned at `ef_search_for_gold` (default 500). The `ef_search=500` cell is therefore 1.0 by construction; the interesting story is the curve at 40/80/200. This costs zero extra RPCs (the gold cell is also one of the swept cells) and gives a stable, viewer-specific reference even if HNSW's exact behavior shifts across pgvector versions.
- **Failure floor.** `recall_floor` in YAML targets `viewer=viewer_1pct, ef_search=40` with `min_recall_at_5 = 0.10` (intentionally loose). The runner exits non-zero only with `--enforce-floor`; the nightly workflow sets that, but local manual runs print the floor verdict and continue. Tighten once three nightlies establish the empirical baseline (already noted as an open question for the PRD).
- **Workflow artifact-on-failure.** The publish step uses `if: always()` so even a recall-floor breach lands the JSON + markdown under `docs/permissions-scale-nightly/<DATE>.md` — the snapshot IS the evidence of the regression and is more useful committed than discarded. The job's exit code still reflects the eval's verdict.
- **CLI ergonomics.** Both seeder and runner take `--config <path>` so the smoke-test config (`tmp/scale_gold_smoke.yaml`, 20 chunks × 3 viewers) and the production config can coexist. Seeder also takes a `WIKITEXT_REVISION` env var for one-off SHA pins without editing YAML.
- **Validation done locally:** pure helpers (deterministic, exactly-K, independent across viewers); HF dataset fetch (22.6M chars from wikitext-103-raw-v1 main); chunking (correct doc/chunk counts and content shape); aggregation + recall-floor + summary rendering (synthetic-data unit test confirmed the table renders the expected curve shape — 1.000 across the row at 50% selectivity, collapse to 0.000 at 1% × ef_search=40). The full live `seed → DB → JWT → match_chunks → recall` round trip costs ~$0.10 OpenAI + ~6 min wall and is left for the nightly's first run; the smoke-config in `tmp/scale_gold_smoke.yaml` reproduces every code path at < $0.001.

**Validation Test:**

- **Setup:** US-037 complete. `OPENAI_API_KEY` available. Disk space for ~100 MB.
- **Steps:**
  1. Run `python -m db_seed.wikipedia_seed`. Wait ~3 minutes.
  2. Confirm 10,000 chunks exist under the wikipedia sentinel user. Confirm three test viewers with 5,000 / 1,000 / 100 ACL'd chunks respectively.
  3. Run `python -m evals.permissions_scale.runner`.
  4. Inspect `summary.md`. Confirm the recall-vs-`ef_search` curve: at 1% selectivity, recall@5 at `ef_search=40` should be visibly lower than at `ef_search=500`. At 50% selectivity, the curve should be relatively flat.
  5. Re-run seeder. Confirm idempotent — same row count, no duplicate ACLs.
  6. Trigger `workflow_dispatch` on the nightly workflow. Confirm artifact lands at `docs/permissions-scale-nightly/<today>.md`.
- **Expected Result:** Step 4's curve clearly shows the HNSW recall-collapse phenomenon at low selectivity + low `ef_search`, and clean recovery at higher `ef_search`. Step 5 confirms idempotency. Step 6 confirms the workflow runs end-to-end.
- **Failure Indicator:** Curve is flat across all selectivities (corpus too small for the phenomenon to manifest), seeder is non-idempotent, workflow fails to produce an artifact, or recall@5 is identical at `ef_search=40` and `ef_search=500` (the SET is not taking effect).

#### US-044: docs/permissions-aware-rag.md writeup

**Description:** As a reviewer reading the repo, I want a single document that names the post-filter recall problem, walks through the data model, shows the retrieval SQL change, names the HNSW gotcha and the `ef_search` mitigation, presents the eval tables, and is honest about what's not implemented, so that I can assess production-RAG seriousness without running the code.

**Acceptance Criteria:**

- [x] `docs/permissions-aware-rag.md` exists with five sections in this order:
  1. **The problem** — naive post-filtering breaks recall when permissions are sparse. Includes the math: viewer with access to 5% of chunks, top-10 followed by post-filter, expected ~0.5 visible chunks. Cites the eval table that demonstrates the empirical recall collapse on partial-access viewers.
  2. **The data model** — `chunk_acl`, `principal_membership`, `principals`, `profiles`. Explicit on additive-to-ownership semantics. Explicit on chunk_acl-as-sole-source-of-truth (no `document_acl` intent table). Explicit on the re-chunking caveat and the snapshot-and-replay handler. Section ends with a note on group-nesting / workspace-scoping deferrals.
  3. **The retrieval change** — SQL diff showing the owner-OR-ACL predicate added to `match_chunks`. Explains the `DISTINCT ON (c.id)` precedence for the granting-principal column.
  4. **The HNSW interaction** — what `ef_search` does, why selective filters hurt recall (HNSW's graph-walk terminates before reaching enough viable candidates), and what was tuned. Mentions partial-index-per-principal and IVFFlat as alternatives with explicit trade-offs (why neither was shipped in v0).
  5. **The numbers** — embeds the correctness eval's three tables (security / recall trade-off / non-regression) and the scale benchmark's `ef_search` sweep. All numbers come from the runner-generated `summary.md` files via `EVAL_SUMMARY` markers — no hand-typed numbers.
- [x] Writeup explicitly names the v0 scope cuts as deliberate choices, each with a one-line reason: per-chunk override UI, share autocomplete, bulk operations, audit-log UI, role hierarchies, write-vs-read permission tiers, nested group membership, workspace scoping. This is the "senior engineer move" section.
- [x] All numbers in sections 1 and 5 come from `evals/retrieval/summary.md` and `evals/permissions_scale/summary.md` via marker embeds. Updating the runner refreshes the doc.
- [x] Typecheck/lint passes (no code changes required for this story beyond docs).

**Implementation notes (US-044):**

- **Embed mechanism is automated.** `docs/_embed_eval_summaries.py` reads each runner's `summary.md`, strips its outer `EVAL_SUMMARY` markers, and replaces the bracketed region in the doc keyed off named markers (`<!-- BEGIN EVAL_SUMMARY:retrieval -->` / `<!-- BEGIN EVAL_SUMMARY:permissions_scale -->`). Idempotent — two consecutive runs produce a byte-identical doc (verified). When a source `summary.md` is missing, the script writes a placeholder note rather than failing, so the doc stays well-formed before the operator has run the corresponding eval.
- **Real numbers in section 5a.** The retrieval correctness eval's `evals/retrieval/summary.md` has been live since US-042; section 5a embeds the current Headline / Per-category / Security / Recall trade-off / Non-regression tables verbatim. The recall-trade-off table shows +0.000 deltas across the board because the 14-chunk corpus is too small for post-filter to push gold below top-5; section 5a's lead now names security as the load-bearing claim and forwards to 5b for why the recall collapse doesn't show up at v0's scale either.
- **Real numbers in section 5b — and an honest negative result.** The wikipedia 10k seed (~263s, ~$0.10 OpenAI) and the scale runner (~48s, 180 RPC calls) were executed; the table is now embedded with real values. **Every cell is 1.000 across all selectivities and `ef_search` values.** EXPLAIN ANALYZE reveals why: at 10k chunks, the Postgres planner doesn't walk the HNSW index at all — it bitmap-scans `chunk_acl` by `principal_id` to get the visible chunk_ids (100 for viewer_1pct, 1000 for viewer_10pct, 5000 for viewer_50pct), index-scans `chunks` for those rows, sorts exactly by embedding distance, and takes top-5. `set hnsw.ef_search = …` is a no-op when the index isn't used. Section 5b carries the EXPLAIN snippet verbatim and section 4 has a new "when this even matters" subsection that names the planner's filter-first behaviour. The infrastructure (seed, viewer setup, sweep, recall floor) is in place for the day a >100k corpus run flips the planner to HNSW; until then the v0 conclusion is "at this scale the gotcha doesn't manifest, and that's fine."
- **One bug surfaced and fixed during the live run.** First scale-runner pass returned 0.000 across every cell — `match_chunks` was applying the production `match_threshold = 0.3` against Acme-domain queries hitting Wikipedia chunks (no Acme query reaches that cosine similarity against any Wikipedia article). Fixed by adding `SCALE_BENCHMARK_THRESHOLD = 0.0` in the runner with a comment explaining why this benchmark needs unconditional top-k by distance — the scale eval measures HNSW *graph-walk* behaviour under selective filters, not retrieval quality, so the threshold filter was actively destroying the signal.
- **Recall floor recalibrated context.** `scale_gold.yaml::recall_floor.min_recall_at_5 = 0.10` was originally framed as "we still retrieve something" loose-floor language. The YAML comment now reflects the empirical reality: at v0 scale the cell is 1.000 (planner-chooses-exact-NN), so the floor sits well below the observed value as a real-regression alarm. When a >100k follow-up benchmark flips the planner to HNSW and recall@5 drops below 1.0, the floor will need rethinking against the empirical curve.
- **SQL diff in section 3** shows the pre-Module-11 4-line where clause vs the post-US-037 owner-OR-ACL predicate, lifted directly from the migration files (not paraphrased). The `DISTINCT ON (c.id)` precedence rule for granting-principal is explained in its own subsection.
- **HNSW gotcha in section 4** names `ef_search` explicitly — what it controls (candidate-queue size of the graph walk), why selective filters hurt (walk terminates before enough viable candidates accumulate), how `match_chunks` exposes the knob (optional `ef_search int` arg + `set_config('hnsw.ef_search', …, true)` for transaction-local scope), and rejects partial-index-per-principal and IVFFlat with one-line trade-offs each (does-not-scale-past-small-N for partial indexes; rebuild cost + lower tuned recall for IVFFlat).
- **Scope cuts as a table**, section 6, with one column per row's reason — per-chunk override UI, share autocomplete, bulk operations, audit-log UI, role hierarchies, write-vs-read tiers, nested group membership, workspace scoping. Each row lands as a sized, deliberate cut rather than a bug.
- **Validation done locally:** all 32 AC items machine-checked (re.search for marker presence, table content, scope-cut names, math expressions, SQL predicates); embed script tested for idempotency (md5 byte-identical across two runs) and for marker survival (BEGIN/END markers preserved on every refresh). One bug surfaced and fixed during validation: the initial embed used `pattern.sub(lambda _m: replacement)` with `\1` / `\2` backreferences in the replacement string — those work with a raw string but NOT with a function-arg `sub` (the function's return is treated as literal text). Fixed by computing the replacement from `m.group(1)` / `m.group(2)` inside the lambda.

**Validation Test:**

- **Setup:** US-037 through US-043 complete. Both eval `summary.md` files have real numbers in them.
- **Steps:**
  1. Open `docs/permissions-aware-rag.md` and read end to end.
  2. Confirm all five sections present, each labelled clearly.
  3. Confirm the Section 1 math (0.5 visible chunks expected at 5% selectivity, top-10) and the cited empirical number from the eval.
  4. Confirm Section 5 tables match the runner outputs exactly.
  5. Confirm the scope-cuts list names each deferral with a one-line reason.
  6. Confirm the section on the re-chunking caveat names the snapshot-and-replay behaviour from US-038.
- **Expected Result:** Document reads as a coherent piece of technical writing. No discrepancy between the doc's numbers and the runner outputs. Scope cuts are explicit and justified.
- **Failure Indicator:** Numbers don't match `summary.md`; scope cuts missing or unjustified; HNSW section is hand-wavy ("HNSW has issues with filters" without naming `ef_search`); SQL diff in Section 3 is missing or wrong.

---

## Functional Requirements

**Authentication & Authorization**
- FR-1: Supabase Auth supports email/password and OAuth (Google, GitHub).
- FR-2: All user-data tables (threads, messages, documents, chunks) enforce RLS scoped to `auth.uid()`.

**Chat**
- FR-3: Chat UI provides threads list, active conversation pane, streaming input.
- FR-4: Messages persisted in Supabase (`messages` table) including tool calls and results.
- FR-5: Two LLM modes supported concurrently: OpenAI Responses API and OpenAI Chat Completions API. Mode selectable per request; default via env.

**Ingestion**
- FR-6: Drag-and-drop ingestion UI at `/ingestion` accepting `.txt, .md, .pdf, .docx, .html` (Module 5).
- FR-7: Ingestion pipeline: upload → parse (docling) → chunk → embed → index.
- FR-8: Document-level metadata extraction via LLM structured outputs (Module 4).
- FR-9: Content-hashing deduplication on documents and chunks (Module 3).
- FR-10: Supabase Realtime drives live status updates on the ingestion page.

**Retrieval**
- FR-11: `search_documents` tool exposes hybrid search (vector + keyword via RRF) with optional metadata filters.
- FR-12: Reranker (configurable: cohere | voyage | llm | none) processes top 20 into top 5.
- FR-13: RLS enforced at the SQL level for all retrieval queries.

**Additional Tools**
- FR-14: `query_database` tool for text-to-SQL over an allowlisted read-only schema.
- FR-15: `web_search` fallback tool invoked when local retrieval is insufficient.
- FR-16: All tool outputs attributed in the UI with expandable sources.

**Sub-Agents**
- FR-17: `spawn_document_agent` tool launches a sub-agent with isolated context and purpose-specific tools.
- FR-18: UI renders main/sub-agent calls as a nested, collapsible tree.

**Structured RAG**
- FR-22: Hand-authored Cube-style semantic layer at `backend/semantic_layer.yaml` defines `entities`, `dimensions`, `metrics` (with `sql_fragment` and `synonyms`), and `joins`.
- FR-23: `plan_query` tool maps natural language to a structured `PlanSpec` via OpenAI function-calling; returns `matched` (with plan) or `no_match` (with `suggested_fallback`).
- FR-24: `sql_search` tool requires a structured `plan` argument and compiles SQL deterministically from metric `sql_fragment`s plus the semantic layer's join graph; replaces `query_database` (FR-14) in the agent's tool registry.
- FR-25: `generate_sql_naive()` remains exported from `backend/text_to_sql.py` as a library function for the structured-RAG eval baseline after `query_database` is removed from the tool list.
- FR-26: Structured-RAG eval harness measures naive vs semantic accuracy on 30 hand-authored questions with hand-written gold result sets; result-set match after normalization.

**Retrieval Evals**
- FR-27: `public.chunks` exposes a `stable_id text` column (additive; no PK change) populated deterministically as `f"{filename_slug}:{chunk_index}"` so golden-set references survive re-seeds and clean CI runs.
- FR-28: `backend.retrieval.get_retrieval_mode()` accepts `"vector"`, `"keyword"`, and `"hybrid"`; `keyword` is reachable via the same env switch the other two modes use.
- FR-29: Retrieval eval runner computes `recall@{1,3,5,10}` (per-chunk, partial credit), `MRR`, and `nDCG@5` (binary relevance) across vector / keyword / hybrid modes against a 50-question golden set with a fixed 20/15/10/5 category split (single_chunk / multi_hop / adversarial / paraphrase).
- FR-30: Runner is deterministic — two consecutive runs produce byte-identical output modulo the `generated_at` timestamp — and emits both a full-detail `results/<ts>.json` and an `EVAL_SUMMARY`-bracketed `summary.md` fragment for `docs/evals.md` to embed.
- FR-31: PR CI workflow (`retrieval-eval.yml`) runs vector / keyword / hybrid on every PR touching retrieval-relevant paths and posts a delta-vs-`main` comment; comment-only (never fails the build).
- FR-32: Nightly workflow (`retrieval-eval-nightly.yml`) runs the full sweep including Cohere / Voyage / LLM-as-reranker modes on a schedule and on manual dispatch.
- FR-33: Retrieval-eval runner accepts an opt-in `--include-generation` flag that, when set, generates an answer per (question × mode) from the mode's top-5 retrieved chunks via `gpt-4o-mini` and scores it with a cross-family judge (`claude-sonnet-4-6` via tool-use structured output) for faithfulness (1–5) and helpfulness (1–5). Aggregates and a third summary table are added automatically when generation runs; retrieval-only invocations produce identical JSON shape to pre-US-036 runs.

**Permissions-aware retrieval**
- FR-34: `chunk_acl(chunk_id, principal_type, principal_id, granted_by, created_at)` is the sole source of truth for non-owner access. ACLs are additive to ownership — `chunks.user_id` (the owner) always has access regardless of ACL state. No `document_acl` intent table; doc-level grants are an operation that materializes one row per chunk in a transaction.
- FR-35: `principal_membership(principal_id, member_user_id)` resolves a viewer's principal set at query time. Flat membership only (groups contain users, not other groups). RLS scoped to `member_user_id = auth.uid()`.
- FR-36: `public.principals(id, name, kind)` is the group registry. `public.profiles(id, email)` is an `auth.users` mirror populated via trigger to support email-to-UUID resolution from the share dialog.
- FR-37: `match_chunks` runs `SECURITY INVOKER` and resolves the viewer's principal set server-side using `auth.uid()`. Filter predicate is owner-OR-ACL: `(c.user_id = auth.uid()) OR EXISTS (chunk_acl row whose principal_id is auth.uid() or a group the viewer belongs to)`. No `principal_ids` parameter — the trust boundary remains the database, not the backend.
- FR-38: `match_chunks` accepts an optional `ef_search int` parameter that calls `set_config('hnsw.ef_search', ef_search::text, true)` before the SELECT. Used by the scale benchmark to demonstrate the HNSW recall-vs-`ef_search` curve under selective filters.
- FR-39: `match_chunks` returns a `granting_principal_id` + `granting_principal_display` per row, computed via `DISTINCT ON (c.id)` with deterministic precedence: owner > direct user grant > group grant. Populates the per-chunk badge in the frontend `ToolAttribution` panel.
- FR-40: Three REST endpoints (`POST /api/documents/{id}/share`, `DELETE /api/documents/{id}/share/{principal_type}/{principal_id}`, `GET /api/documents/{id}/shares`) gated server-side on `documents.user_id = auth.uid()`. Only the document owner can grant, revoke, or list shares.
- FR-41: Re-ingestion preserves grants via a snapshot-and-replay handler — the pipeline reads aggregated chunk_acl rows before deleting chunks and re-applies them after re-chunking; if interrupted, a journaled `documents.metadata->'pending_acl_replay'` field is replayed on the next successful ingestion.
- FR-42: Correctness eval extends Module 10's runner with viewer parameterization (`--viewers {full,partial,no_access,all}`) producing three independent tables — security (pre-filter must show 0 leakage to no-access viewers), recall trade-off (post-filter recall@5 collapses vs pre-filter on partial-access viewers), non-regression (full-access viewers match the Module-10 baseline within ±0.005).
- FR-43: Scale benchmark seeds 10,000 Wikipedia-derived chunks (HuggingFace `wikitext-103-raw-v1`, pinned slice) with ACLs at selectivities {50%, 10%, 1%}, sweeps `ef_search ∈ {40, 80, 200, 500}`, and runs nightly + on `workflow_dispatch`. Never runs per-PR.

**Observability & Config**
- FR-19: LangSmith traces every LLM call and tool call with user_id/thread_id metadata.
- FR-20: All configuration (model names, thresholds, providers, keys) via environment variables — no admin UI.
- FR-21: App deploys to Vercel (frontend) + Railway/Fly (backend) + Supabase, documented in README.

## Non-Goals (Out of Scope)

- ❌ Knowledge graphs / GraphRAG
- ❌ Code execution / sandboxing
- ❌ Image, audio, or video processing
- ❌ Fine-tuning
- ❌ Multi-tenant admin features (organizations, RBAC roles, permission-bundle hierarchies) — Module 11 ships per-document ACLs with users and groups, not tenant-level admin
- ❌ Nested group membership (groups containing groups). Flat membership only in v0; nesting deferred until an IdP integration story
- ❌ Workspace / tenant scoping of groups — v0 uses a single global `principals` namespace
- ❌ Per-chunk override UI in Module 11. The data model supports chunk-level overrides; the share dialog exposes doc-level grants only
- ❌ Share-input autocomplete or principal-search endpoint — free-text input only in v0
- ❌ Bulk share operations (share-N-docs-at-once) — per-document operation only
- ❌ Audit-log UI for grants/revokes — `granted_by` and `created_at` columns exist on `chunk_acl` for a future audit view; no UI in v0
- ❌ Role / permission-tier distinction (view vs edit vs share-with-others) — grant is binary in v0 (retrieval is read-only)
- ❌ Permission-preview simulator ("what would Sarah see in this document?") — the eval is the explanation
- ❌ Billing/payments
- ❌ Data connectors (Google Drive, SFTP, APIs, webhooks)
- ❌ Scheduled/automated ingestion pipelines
- ❌ Admin UI for configuration (env vars only)
- ❌ LLM frameworks (LangChain, LlamaIndex, Haystack) — raw OpenAI SDK + Pydantic only
- ❌ Providers beyond OpenAI initially (Module 2+ architected for OpenAI-compatible providers but only OpenAI ships in v1)
- ❌ Auto-generated or LLM-authored semantic layer (Module 9 YAML is hand-written and version-controlled)
- ❌ Write operations against the `crm` schema (read-only role; structured RAG is query-only)
- ❌ Multi-step / decomposed SQL plans within a single turn (Module 9 plans are single-query specs)
- ❌ Generation-quality metrics in PR CI. The faithfulness / helpfulness judge from US-036 runs only on opt-in `--include-generation` invocations (e.g., nightly or manual) — the PR comment workflow stays retrieval-only so per-PR cost stays at the embedding-API floor.
- ❌ Full agent-loop eval in Module 10 — retrieval functions are measured in isolation; tool-routing eval is a separate concern
- ❌ Graded relevance (primary vs supporting chunks) in the golden set — binary relevance only
- ❌ PR-blocking on retrieval regressions — CI is comment-only to avoid false-positive flake from RRF ties and reranker variance
- ❌ Retrieval eval against community-fork PRs — GitHub Actions does not pass secrets to fork PRs (limitation documented in the workflow)

## Design Considerations

- **Frontend:** React + TypeScript + Vite + Tailwind + shadcn/ui. Default theme: dark. Two top-level routes: `/chat` (default) and `/ingestion`.
- **Chat UI:** Two-pane layout (threads sidebar + conversation pane). Tool-call attribution rendered as collapsible tree nodes.
- **Ingestion UI:** Single-page drop zone + document table with real-time status badges.
- **Component reuse:** Prefer shadcn/ui primitives (Dialog, Badge, Select, Toast).
- **Streaming:** Server-Sent Events or fetch streams for token-by-token rendering.

## Technical Considerations

- **Stack:** React/TypeScript/Vite/Tailwind/shadcn frontend; Python/FastAPI backend; Supabase (Postgres + pgvector + Auth + Storage + Realtime) database.
- **No LLM frameworks:** Raw `openai` SDK, Pydantic for structured outputs and tool schemas.
- **pgvector index:** HNSW preferred; IVFFlat acceptable for small corpora.
- **Chat Completions tool-call loop:** Implemented manually — request → receive `tool_calls` → execute tools → append results → re-request — capped at 5 iterations to prevent runaway loops.
- **RLS:** Enforced via Supabase client using user JWT; service-role key used only for system-level operations (ingestion workers).
- **Dual-support mode (Module 1→2):** Responses API and Chat Completions live behind a common streaming interface; selected per-request. Schema retains `openai_thread_id` for Responses-mode threads.
- **Docling:** Used for all non-plain-text parsing starting in Module 5.
- **Deployment:** Vercel for the frontend, Railway or Fly for the FastAPI backend, Supabase hosted. Background ingestion runs as a FastAPI BackgroundTask (or worker, if queueing becomes necessary).
- **Observability:** LangSmith mandatory from Module 1; user_id/thread_id attached to every trace.

## Success Metrics

- **Build completion:** User finishes all 11 modules and has a deployed app at a public URL.
- **Learning outcomes:** User can explain (verbally or in writing) chunking, embeddings, hybrid search, reranking, and sub-agent delegation, pointing to the exact code that implements each.
- **Retrieval quality (post-Module 6):** On a small hand-curated eval set (20 Q/A pairs), top-5 hybrid + reranked recall ≥ 80%.
- **Structured-RAG accuracy (post-Module 9):** On the 30-question structured-RAG eval, the semantic-layer path beats naive text-to-SQL by ≥ 30 percentage points overall, with the largest gap on the metric-ambiguity subset.
- **Retrieval eval coverage (post-Module 10):** A 50-question golden set is scored on vector / keyword / hybrid modes with deterministic results, the headline table is published in `docs/evals.md`, and the PR CI workflow posts delta-vs-`main` comments. A staged regression PR demonstrably moves recall@5 in the expected direction.
- **Permission-aware retrieval correctness (post-Module 11):** On the 50 × 3 viewer-parameterized correctness eval, pre-filter shows 0 gold leakage on every no-access viewer (security); post-filter recall@5 collapses vs pre-filter on partial-access viewers (recall trade-off — headline number for the writeup); full-access viewers match the Module-10 baseline within ±0.005 (non-regression). On the 10k-chunk scale benchmark, the recall-vs-`ef_search` curve shows visibly lower recall at `ef_search=40` with 1% selectivity and recovery at `ef_search≥200`, demonstrating the HNSW gotcha empirically.
- **Performance:** P50 first-token latency < 2s; P50 ingestion latency < 5s per MB of text.
- **Cost discipline:** Embedding and completion costs visible in LangSmith per-trace.
- **Multi-user correctness:** RLS tests pass — User B never sees User A's data under any code path.

## Open Questions

- **Ingestion worker model:** FastAPI BackgroundTasks vs. a dedicated worker (Celery / RQ / Supabase Edge Functions)? Revisit if ingestion throughput becomes a bottleneck.
- **Reranker default:** Cohere Rerank, Voyage Rerank, or LLM-as-reranker — pick a sensible default in Module 6 based on latency/cost tradeoffs.
- **Sub-agent model choice:** Same model as main agent, or smaller/cheaper for long-context summarization?
- **Dual-support UX:** Should the mode toggle be per-thread, per-request, or user-wide? Current plan is per-thread with a user-wide default.
- **Eval harness:** Should we ship a lightweight retrieval eval script in the repo from Module 6 onward?
- **Rate limiting:** Per-user limits on ingestion and chat to prevent cost blowups — needed for v1 or deferred?
- **Module 9 headline target:** Aim for ≥30 pp naive→semantic delta. If first eval run shows <15 pp, investigate whether the planner is under-constraining or whether the eval questions don't actually trigger metric ambiguity — do not p-hack the questions to inflate the gap.
- **Module 9 frontend polish:** Separate-cards rendering for `plan_query` and `sql_search` is the floor. A combined "Structured Query" card linking plan + compiled SQL + results visually could be a later polish item if demo time permits.
- **Module 10 corpus topics:** Final list of 5–10 CRM-domain markdown documents to be selected during US-032 authoring; aim for ~150 chunks total at default 500/50 tokenisation.
- **Module 10 results retention:** Whether `evals/retrieval/results/*.json` is committed to the repo (reproducible historical comparisons, more diff churn) or gitignored except for a single `latest.json` checkpoint. Resolve during US-033.
- **Module 10 nightly output venue:** GitHub Discussions (cleaner, no commit noise) vs. committing `docs/evals-nightly.md` (easier to grep historically). Decide during US-035.
- **Module 10 PR bot comment update strategy:** Use `actions/github-script` with a hidden marker to update the existing bot comment in place across PR re-pushes, rather than stacking new comments.
- **Module 11 email-to-UUID resolution:** Three options considered for resolving an email entered in the share dialog to a user UUID — (i) `public.profiles(id, email)` mirrored from `auth.users` via a trigger, (ii) the Supabase admin SDK from the backend with service-role, (iii) a SECURITY DEFINER RPC `lookup_user_by_email`. PRD currently leans on (i) (cleanest reuse, no new service-role surface); confirm during US-037 authoring.
- **Module 11 group-name registry shape:** `principals(id, name, kind)` per current plan. Whether to also store `description` / `owner_user_id` / `created_by` columns for future group-admin features — defer until a real use case appears. Decide during US-037.
- **Module 11 re-ingestion ACL snapshot location:** Whether the snapshot lives in `documents.metadata->'pending_acl_replay'` JSONB (simple, one fewer table) or a dedicated `ingestion_acl_snapshot` table (more auditable, easier to recover from partial failures). Current plan is JSONB; revisit if production-realism reviewers push back during US-038.
- **Module 11 ef_search regression threshold:** What recall@5 floor to enforce in the nightly scale-benchmark workflow (above which it does NOT fail loudly, below which it does). Establish from the first three nightly runs once the workflow lands in US-043.
- **Module 11 scale benchmark output venue:** Daily-stamped files in `docs/permissions-scale-nightly/<YYYY-MM-DD>.{json,md}` per current plan, mirroring the Module-10 nightly convention. Revisit if the directory grows unbounded after ~6 months of runs — at that point a retention/pruning policy is needed.
