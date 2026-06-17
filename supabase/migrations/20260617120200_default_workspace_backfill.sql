-- US-002 (ADR-0002): migrate all existing data into a single Default Workspace
-- so that switching on the SUBTRACTIVE workspace boundary (US-003 onward) hides
-- nothing that was previously visible.
--
-- The boundary is subtractive — a chunk is visible only if its document's
-- workspace_id is one the viewer belongs to — so unlike the additive chunk_acl
-- model there is NO safe "do nothing" default: a document or user left out of a
-- workspace becomes invisible to everyone (even its owner). This migration
-- therefore ACTIVELY places every existing user and every existing document into
-- one Default Workspace, making the membership clause a no-op for the legacy
-- corpus (E4 passes bit-for-bit; the boundary only bites once a SECOND workspace
-- exists — proven additively by E6 in US-009).
--
-- The Default Workspace uses a FIXED, deterministic UUID (not gen_random_uuid)
-- so seed scripts, tests, and evals can reference it as a constant. The corpus
-- sentinel user and the two synthetic eval viewers are created at SEED time —
-- AFTER this migration runs — so they are not present for the auth.users
-- backfill below; db_seed/corpus_seed.py and evals/retrieval/runner.py add their
-- Default-Workspace memberships (DEFAULT_WORKSPACE_ID) at seed time so the
-- membership clause stays inert for the eval corpus.

-- 1. The single Default Workspace. Workspace creation is operator-level (no
--    self-serve INSERT policy); seeding it here is the operator action.
insert into public.workspaces (id, name)
values ('00000000-0000-0000-0000-0000000000d0', 'Default Workspace')
on conflict (id) do nothing;

-- 2. Backfill every EXISTING user as a member. On a fresh `supabase db reset`
--    auth.users is empty at migration time (corpus/eval users are inserted later
--    by the Python seeders, which add their own membership rows); on a
--    production upgrade this captures the real user base. Idempotent.
insert into public.workspace_membership (workspace_id, user_id, role)
select '00000000-0000-0000-0000-0000000000d0', id, 'member'
from auth.users
on conflict do nothing;

-- 3. documents.workspace_id — staged so the migration applies on a table that
--    already holds the Module-11 corpus: add nullable, backfill, THEN constrain.
--    A single NOT NULL add would fail on the existing rows.
alter table public.documents
  add column workspace_id uuid;

update public.documents
   set workspace_id = '00000000-0000-0000-0000-0000000000d0'
 where workspace_id is null;

-- The column DEFAULT is TRANSITIONAL: it keeps existing INSERT sites (the
-- frontend upload in frontend/src/lib/ingestion.ts and the auxiliary seeders)
-- writing into the Default Workspace until US-007 stamps the *resolved active*
-- workspace explicitly at ingestion. It also guarantees workspace_id is never
-- NULL — the subtractive boundary's core invariant. US-007 supplies an explicit
-- value (overriding this default) and may drop the default.
alter table public.documents
  alter column workspace_id set default '00000000-0000-0000-0000-0000000000d0',
  alter column workspace_id set not null;

alter table public.documents
  add constraint documents_workspace_id_fkey
  foreign key (workspace_id) references public.workspaces(id);

-- workspace_id lives on documents ONLY (not denormalized onto chunks):
-- match_chunks already joins documents (20260514150000_match_chunks_granting_principal.sql
-- line 92), so the US-003 membership clause reads d.workspace_id from that join.
-- This index serves that workspace filter / join.
create index documents_workspace_id_idx
  on public.documents (workspace_id);
