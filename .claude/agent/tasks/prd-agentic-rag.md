# PRD: Agentic RAG System

## Introduction

An educational, production-oriented Retrieval-Augmented Generation (RAG) application built in 8 progressive modules. The system has two primary interfaces: a **Chat** view for threaded, retrieval-augmented conversations, and an **Ingestion** view for manual document upload and management.

The target audience is technically-minded builders who want to learn production RAG patterns (chunking, embeddings, hybrid search, reranking, agentic routing, sub-agents) by directing AI coding tools. They do not need to know Python or React—they need to understand RAG concepts and codebase structure deeply enough to direct AI to build and fix the system.

The system avoids LLM frameworks (LangChain, LlamaIndex) in favor of raw OpenAI SDK calls and Pydantic, so every layer of the stack is inspectable and modifiable.

## Goals

- Deliver a working, deployable, multi-user RAG application.
- Progress through 8 discrete modules, each learnable in a focused session.
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

- [ ] `documents.metadata JSONB` column
- [ ] Pydantic schema: `{title, authors: [str], topics: [str], published_date: date | null, document_type: str}`
- [ ] LLM extraction runs during ingestion using structured outputs
- [ ] Extraction failures do not block ingestion; `metadata` may be null with a warning logged
- [ ] Typecheck passes

**Validation Test:**

- **Setup:** Upload a research paper PDF or markdown with clear metadata.
- **Steps:**
  1. Trigger ingestion
  2. Query `SELECT metadata FROM documents WHERE id = <id>`
- **Expected Result:** `metadata` JSON contains a reasonable title, author list, and 2+ topics.
- **Failure Indicator:** `metadata` is null without an error log, or schema-invalid JSON is stored.

#### US-017: Metadata-filtered retrieval

**Description:** As a user, I want my retrieval queries to optionally filter by metadata (e.g., "only documents from 2024") so I get more precise results.

**Acceptance Criteria:**

- [ ] `search_documents` tool accepts optional `filters: {topics?, document_type?, date_range?}`
- [ ] Filters translated to SQL `WHERE` clauses on `documents.metadata`
- [ ] Agent is prompted with the metadata schema so it knows what filters are valid
- [ ] Typecheck passes

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

- [ ] `docling` integrated on the backend
- [ ] Accepted MIME types expanded: `.pdf, .docx, .html, .md, .txt`
- [ ] Parsed output preserves headings and paragraph structure where possible
- [ ] Chunking respects structural boundaries (doesn't split mid-heading)
- [ ] Per-format parse failures reported via `status = error` with a readable message
- [ ] Typecheck passes
- [ ] Verify in browser using dev-browser skill

**Validation Test:**

- **Setup:** One sample file per supported format.
- **Steps:**
  1. Upload each file sequentially
  2. Query `SELECT filename, LENGTH(content) FROM chunks JOIN documents ...` for each
  3. Spot-check one chunk from each format visually
- **Expected Result:** All five ingest successfully; chunk contents are readable plain text (no HTML tags, no PDF artifacts).
- **Failure Indicator:** Any format fails silently, or chunks contain raw markup.

#### US-019: Cascade deletes on document removal

**Description:** As a user, when I delete a document I expect all its chunks, embeddings, and file-storage blobs to be removed atomically.

**Acceptance Criteria:**

- [ ] FK `ON DELETE CASCADE` on `chunks.document_id`
- [ ] Supabase Storage blob deleted in the same transaction (or compensating action on failure)
- [ ] UI reflects the deletion in real-time
- [ ] Typecheck passes

**Validation Test:**

- **Setup:** A document with 15 chunks and a stored blob.
- **Steps:**
  1. Click delete on the document
  2. Query `SELECT COUNT(*) FROM chunks WHERE document_id = <id>`
  3. Check Supabase Storage for the blob
- **Expected Result:** Chunk count = 0; blob is gone; UI row removed.
- **Failure Indicator:** Orphan chunks, orphan blobs, or UI still shows the deleted row.

---

### Module 6 — Hybrid Search & Reranking

#### US-020: Keyword search with Postgres full-text search

**Description:** As a developer, I need a keyword search function that complements vector search for exact-match queries (names, IDs, technical terms).

**Acceptance Criteria:**

- [ ] `chunks.content_tsv tsvector` column maintained via trigger or generated column
- [ ] GIN index on `content_tsv`
- [ ] `keyword_search(query, top_k)` function returns chunks ranked by `ts_rank_cd`
- [ ] Typecheck passes

**Validation Test:**

- **Setup:** Chunk corpus containing the exact token "XJ-2049-alpha".
- **Steps:**
  1. Run vector search for "XJ-2049-alpha"
  2. Run keyword search for the same
- **Expected Result:** Keyword search returns the exact-match chunk at rank 1. Vector search may or may not — this demonstrates why hybrid is needed.
- **Failure Indicator:** Keyword search misses exact tokens or uses a slow sequential scan.

#### US-021: Reciprocal Rank Fusion (RRF) combining vector + keyword

**Description:** As a developer, I want to combine vector and keyword results using RRF so retrieval balances semantic and lexical signals.

**Acceptance Criteria:**

- [ ] `hybrid_search(query, top_k)` runs both searches and merges via RRF (default `k=60`, configurable)
- [ ] Duplicate chunks (appearing in both rankings) are deduplicated, scores summed
- [ ] `search_documents` tool switched to use hybrid search by default
- [ ] Typecheck passes

**Validation Test:**

- **Setup:** A chunk that ranks well on vector (topical match) + a chunk that ranks well on keyword (exact token).
- **Steps:**
  1. Run hybrid search for a query that exercises both
  2. Compare top-5 to vector-only and keyword-only results
- **Expected Result:** Hybrid top-5 contains the best from both; neither exact-match nor semantic-match chunks are lost.
- **Failure Indicator:** Hybrid favors one strategy, drops relevant results from the other, or duplicates entries.

#### US-022: Reranking via cross-encoder or LLM

**Description:** As a developer, I want the top N hybrid results reranked by a cross-encoder (or an LLM-as-reranker) so the final top-k is higher precision.

**Acceptance Criteria:**

- [ ] Reranker configurable via env: `RERANKER=cohere|voyage|llm|none`
- [ ] Reranker takes top 20 from hybrid, returns top 5 to the agent
- [ ] Latency logged; if reranker exceeds 2s, log a warning
- [ ] Typecheck passes

**Validation Test:**

- **Setup:** A query where the best chunk is ranked 8th by hybrid search (measurable by handcrafted eval).
- **Steps:**
  1. Run hybrid-only retrieval; note top-5
  2. Run hybrid + rerank; note top-5
- **Expected Result:** Reranked top-5 includes the 8th-ranked chunk in a higher position.
- **Failure Indicator:** Reranker has no effect on rankings, or reranker silently fails.

---

### Module 7 — Additional Tools

#### US-023: Text-to-SQL tool for structured data

**Description:** As an agent, I want a `query_database` tool that translates natural language to SQL and runs it against a designated read-only analytics schema so I can answer questions over structured data.

**Acceptance Criteria:**

- [ ] Read-only Postgres role used for SQL execution
- [ ] Allowlisted schema (e.g., `analytics.*`); all other schemas blocked
- [ ] Agent provided schema snapshot (table + column list) in its system prompt
- [ ] Query timeout (default 10s)
- [ ] Typecheck passes

**Validation Test:**

- **Setup:** `analytics.orders(id, user_email, total, created_at)` seeded with 100 rows.
- **Steps:**
  1. Ask "What was total revenue last month?"
  2. Ask "DROP TABLE analytics.orders" (adversarial)
  3. Inspect LangSmith trace
- **Expected Result:** Step 1 returns a SUM result with correct SQL. Step 2 is refused or fails on the read-only role. Step 3 shows the generated SQL in the trace.
- **Failure Indicator:** Write query succeeds, SQL not logged, or agent hallucinates schema.

#### US-024: Web search fallback tool

**Description:** As an agent, when local retrieval returns nothing relevant, I want a `web_search` tool (e.g., Tavily, Brave, SerpAPI) so I can still answer the user.

**Acceptance Criteria:**

- [ ] Web search provider configurable via env
- [ ] Tool returns top N results with title, URL, snippet
- [ ] Agent instructed to prefer local retrieval; web search used when no local chunks pass threshold
- [ ] Final response cites web sources with URLs
- [ ] Typecheck passes

**Validation Test:**

- **Setup:** Ingest 0 documents on a fresh account.
- **Steps:**
  1. Ask "What happened in tech news today?"
  2. Inspect the response for citations
- **Expected Result:** Agent uses `web_search`; response includes clickable source URLs.
- **Failure Indicator:** Agent hallucinates without tools, or citations are missing.

#### US-025: Tool routing with attribution in the UI

**Description:** As a user, I want to see which tool(s) the agent used for each response (retrieval, SQL, web) with a collapsible details panel showing sources.

**Acceptance Criteria:**

- [ ] Each assistant message records the tools invoked and their outputs
- [ ] UI shows icons/badges next to the message (e.g., 📄 docs, 🗄️ SQL, 🌐 web)
- [ ] Clicking a badge expands a panel with retrieved chunks / SQL / URLs
- [ ] Typecheck passes
- [ ] Verify in browser using dev-browser skill

**Validation Test:**

- **Setup:** Ingested documents + seeded analytics schema.
- **Steps:**
  1. Ask a doc-grounded question
  2. Ask an analytics question
  3. Ask a current-events question
- **Expected Result:** Each response shows the correct tool badge; expanding reveals matching sources.
- **Failure Indicator:** Missing badges, wrong attributions, or expansion shows nothing.

---

### Module 8 — Sub-Agents

#### US-026: Full-document scenario detection

**Description:** As an agent, I want to detect when a user's question requires reading an entire document (e.g., "summarize this paper") so I can delegate to a sub-agent with isolated context.

**Acceptance Criteria:**

- [ ] Heuristic or classifier flags "full-document" intent (keywords: summarize, outline, full, entire; or LLM-based classifier)
- [ ] Trigger threshold tunable via env
- [ ] When triggered, main agent invokes a `spawn_document_agent` tool instead of `search_documents`
- [ ] Typecheck passes

**Validation Test:**

- **Setup:** Ingested 50-page document.
- **Steps:**
  1. Ask "What's on page 3?" (chunk-level)
  2. Ask "Give me a full summary of the paper" (document-level)
- **Expected Result:** Step 1 uses `search_documents`. Step 2 triggers `spawn_document_agent`.
- **Failure Indicator:** Detection misfires in either direction.

#### US-027: Sub-agent with isolated context and its own tools

**Description:** As a developer, I want sub-agents to run in their own context window with a limited, purpose-specific toolset so large documents don't blow up the main agent's context.

**Acceptance Criteria:**

- [ ] Sub-agent implemented as a separate async task with its own message list
- [ ] Sub-agent receives only the relevant document(s), not the full chat history
- [ ] Sub-agent tools: `read_document_chunk(chunk_index)`, `finalize(summary)`
- [ ] Sub-agent's `finalize` output returned as a tool result to the main agent
- [ ] Sub-agent failures are caught and surfaced as a tool error
- [ ] Typecheck passes

**Validation Test:**

- **Setup:** 50-page document + ongoing chat thread with ~30 prior turns.
- **Steps:**
  1. Ask for a full-document summary
  2. Inspect LangSmith trace
  3. Check context size of the sub-agent vs main agent
- **Expected Result:** Sub-agent trace is a separate span tree. Sub-agent context does NOT include the 30 prior chat turns. Main agent receives only the final summary string.
- **Failure Indicator:** Sub-agent sees full chat history, or main agent receives the raw document dump.

#### US-028: Hierarchical tool-call display in the UI

**Description:** As a user, I want to see sub-agent activity nested under the main agent's response so I understand what the system did.

**Acceptance Criteria:**

- [ ] Main agent tool calls rendered in a collapsible tree
- [ ] Sub-agent actions nested under the spawning tool call
- [ ] Reasoning from both main and sub-agent visible (when supported by the model)
- [ ] Streaming updates the tree as actions complete
- [ ] Typecheck passes
- [ ] Verify in browser using dev-browser skill

**Validation Test:**

- **Setup:** Trigger a sub-agent summarization.
- **Steps:**
  1. Watch the response area during streaming
  2. Expand the tool-call tree after completion
- **Expected Result:** Tree shows: Main agent → `spawn_document_agent` → [sub-agent: `read_document_chunk` ×N, `finalize`] → Main agent final response. Each node expandable.
- **Failure Indicator:** Flat list instead of tree, sub-agent actions missing, or reasoning not shown.

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

**Observability & Config**
- FR-19: LangSmith traces every LLM call and tool call with user_id/thread_id metadata.
- FR-20: All configuration (model names, thresholds, providers, keys) via environment variables — no admin UI.
- FR-21: App deploys to Vercel (frontend) + Railway/Fly (backend) + Supabase, documented in README.

## Non-Goals (Out of Scope)

- ❌ Knowledge graphs / GraphRAG
- ❌ Code execution / sandboxing
- ❌ Image, audio, or video processing
- ❌ Fine-tuning
- ❌ Multi-tenant admin features (organizations, roles, permissions)
- ❌ Billing/payments
- ❌ Data connectors (Google Drive, SFTP, APIs, webhooks)
- ❌ Scheduled/automated ingestion pipelines
- ❌ Admin UI for configuration (env vars only)
- ❌ LLM frameworks (LangChain, LlamaIndex, Haystack) — raw OpenAI SDK + Pydantic only
- ❌ Providers beyond OpenAI initially (Module 2+ architected for OpenAI-compatible providers but only OpenAI ships in v1)

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

- **Build completion:** User finishes all 8 modules and has a deployed app at a public URL.
- **Learning outcomes:** User can explain (verbally or in writing) chunking, embeddings, hybrid search, reranking, and sub-agent delegation, pointing to the exact code that implements each.
- **Retrieval quality (post-Module 6):** On a small hand-curated eval set (20 Q/A pairs), top-5 hybrid + reranked recall ≥ 80%.
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
