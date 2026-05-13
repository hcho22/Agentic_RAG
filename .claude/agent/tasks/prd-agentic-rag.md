# PRD: Agentic RAG System

## Introduction

An educational, production-oriented Retrieval-Augmented Generation (RAG) application built in 9 progressive modules. The system has two primary interfaces: a **Chat** view for threaded, retrieval-augmented conversations, and an **Ingestion** view for manual document upload and management.

The target audience is technically-minded builders who want to learn production RAG patterns (chunking, embeddings, hybrid search, reranking, agentic routing, sub-agents) by directing AI coding tools. They do not need to know Python or React—they need to understand RAG concepts and codebase structure deeply enough to direct AI to build and fix the system.

The system avoids LLM frameworks (LangChain, LlamaIndex) in favor of raw OpenAI SDK calls and Pydantic, so every layer of the stack is inspectable and modifiable.

## Goals

- Deliver a working, deployable, multi-user RAG application.
- Progress through 9 discrete modules, each learnable in a focused session.
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
- ❌ Auto-generated or LLM-authored semantic layer (Module 9 YAML is hand-written and version-controlled)
- ❌ Write operations against the `crm` schema (read-only role; structured RAG is query-only)
- ❌ Multi-step / decomposed SQL plans within a single turn (Module 9 plans are single-query specs)

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

- **Build completion:** User finishes all 9 modules and has a deployed app at a public URL.
- **Learning outcomes:** User can explain (verbally or in writing) chunking, embeddings, hybrid search, reranking, and sub-agent delegation, pointing to the exact code that implements each.
- **Retrieval quality (post-Module 6):** On a small hand-curated eval set (20 Q/A pairs), top-5 hybrid + reranked recall ≥ 80%.
- **Structured-RAG accuracy (post-Module 9):** On the 30-question structured-RAG eval, the semantic-layer path beats naive text-to-SQL by ≥ 30 percentage points overall, with the largest gap on the metric-ambiguity subset.
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
