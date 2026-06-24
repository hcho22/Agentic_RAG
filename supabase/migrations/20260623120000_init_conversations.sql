-- US-066 (ADR-0004 + ADR-0008, layered on ADR-0002): the support-conversation
-- surface (Epic E) gets its OWN table pair — `conversations` +
-- `conversation_messages` — deliberately NOT an extension of `threads` /
-- `messages`.
--
-- WHY A NEW TABLE PAIR (do not collapse these): `threads`/`messages` carry a
-- leak-proof OWNER-ONLY predicate (`auth.uid() = user_id`, see
-- 20260416120000_init_threads_messages.sql). Support conversations need
-- WORKSPACE-MEMBERSHIP visibility (any member of the workspace can read/claim
-- the queue), a fundamentally different boundary. Branching the owner-only
-- predicate on a `kind` discriminator would put two trust models in one policy
-- and is exactly the footgun PRD Risk #3 / ADR-0004 reject. So this migration
-- adds a parallel pair and leaves `threads`/`messages` and their policies
-- completely untouched.
--
-- The visibility boundary here is the SAME ADR-0002 membership clause used by
-- match_chunks / the chunks-documents RLS: an EXISTS against
-- workspace_membership keyed on (workspace_id, auth.uid()) — membership PRESENCE
-- only. `workspace_membership.role` is administrative-only (ADR-0002) and must
-- never enter a visibility predicate, so it appears in NONE of the policies
-- below. `conversation_messages` delegates to its parent conversation's
-- membership, exactly mirroring how `messages` delegates to `threads`.

-- conversations: one support conversation, owned by a workspace, optionally
-- driven by the per-workspace support bot (bot_user_id, lazily provisioned in
-- US-069) and optionally claimed by a human agent (claimed_by/claimed_at, the
-- ADR-0008 handoff). customer_email is captured opportunistically; the customer
-- is anonymous and structurally off the Supabase JWT trust surface (US-071).
create table public.conversations (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references public.workspaces(id),
  bot_user_id uuid references auth.users(id) on delete set null,
  customer_email text,
  status text not null default 'active',
  escalated_at timestamptz,
  channel text not null default 'widget',
  claimed_by uuid references auth.users(id) on delete set null,
  claimed_at timestamptz,
  created_at timestamptz not null default now()
);

-- (workspace_id, status) serves the agent-queue list — "escalated conversations
-- in my workspace" — which filters on exactly these two columns.
create index conversations_workspace_id_status_idx
  on public.conversations (workspace_id, status);

-- conversation_messages mirrors the `messages` shape so the same transcript
-- rendering / model-message plumbing works unchanged. tool_calls / tool_call_id
-- / name are kept for schema parity with assistant tool-call turns; they are
-- null/unused for widget conversations (the support bot's deflection pipeline is
-- deterministic control flow, not the agentic tool loop), present so a future
-- tool-using surface needs no migration.
create table public.conversation_messages (
  id uuid primary key default gen_random_uuid(),
  conversation_id uuid not null references public.conversations(id) on delete cascade,
  role text not null check (role in ('user', 'assistant', 'system', 'tool')),
  content text,
  tool_calls jsonb,
  tool_call_id text,
  name text,
  created_at timestamptz not null default now()
);

create index conversation_messages_conversation_id_created_at_idx
  on public.conversation_messages (conversation_id, created_at asc);

alter table public.conversations enable row level security;
alter table public.conversation_messages enable row level security;

-- conversations: visible to / writable by any MEMBER of the owning workspace.
-- The EXISTS is the canonical ADR-0002 membership clause (workspace_id +
-- user_id = auth.uid()); `role` is intentionally absent. Writes (INSERT/UPDATE)
-- are gated on membership of the row's workspace in the same spirit as the
-- threads/messages owner-only writes — a member can create a conversation in a
-- workspace they belong to and update (claim/escalate/resolve) one there, and
-- nothing in a workspace they don't. DELETE is membership-gated too for
-- consistency with the threads/messages precedent (it grants delete to the
-- owner); conversation lifecycle is status-driven, so deletes are not an
-- expected path, but leaving DELETE deny-by-default would diverge from the
-- mirrored table without reason.
create policy conversations_select_member on public.conversations
  for select using (
    exists (
      select 1 from public.workspace_membership wm
      where wm.workspace_id = conversations.workspace_id
        and wm.user_id = auth.uid()
    )
  );

create policy conversations_insert_member on public.conversations
  for insert with check (
    exists (
      select 1 from public.workspace_membership wm
      where wm.workspace_id = conversations.workspace_id
        and wm.user_id = auth.uid()
    )
  );

create policy conversations_update_member on public.conversations
  for update using (
    exists (
      select 1 from public.workspace_membership wm
      where wm.workspace_id = conversations.workspace_id
        and wm.user_id = auth.uid()
    )
  )
  with check (
    exists (
      select 1 from public.workspace_membership wm
      where wm.workspace_id = conversations.workspace_id
        and wm.user_id = auth.uid()
    )
  );

create policy conversations_delete_member on public.conversations
  for delete using (
    exists (
      select 1 from public.workspace_membership wm
      where wm.workspace_id = conversations.workspace_id
        and wm.user_id = auth.uid()
    )
  );

-- conversation_messages inherit access from their parent conversation's
-- workspace membership — the exact delegation pattern messages → threads uses in
-- 20260416120000_init_threads_messages.sql, one level deeper (message →
-- conversation → workspace_membership). No `role` predicate here either.
create policy conversation_messages_select_member on public.conversation_messages
  for select using (
    exists (
      select 1 from public.conversations c
      join public.workspace_membership wm on wm.workspace_id = c.workspace_id
      where c.id = conversation_messages.conversation_id
        and wm.user_id = auth.uid()
    )
  );

create policy conversation_messages_insert_member on public.conversation_messages
  for insert with check (
    exists (
      select 1 from public.conversations c
      join public.workspace_membership wm on wm.workspace_id = c.workspace_id
      where c.id = conversation_messages.conversation_id
        and wm.user_id = auth.uid()
    )
  );

create policy conversation_messages_update_member on public.conversation_messages
  for update using (
    exists (
      select 1 from public.conversations c
      join public.workspace_membership wm on wm.workspace_id = c.workspace_id
      where c.id = conversation_messages.conversation_id
        and wm.user_id = auth.uid()
    )
  )
  with check (
    exists (
      select 1 from public.conversations c
      join public.workspace_membership wm on wm.workspace_id = c.workspace_id
      where c.id = conversation_messages.conversation_id
        and wm.user_id = auth.uid()
    )
  );

create policy conversation_messages_delete_member on public.conversation_messages
  for delete using (
    exists (
      select 1 from public.conversations c
      join public.workspace_membership wm on wm.workspace_id = c.workspace_id
      where c.id = conversation_messages.conversation_id
        and wm.user_id = auth.uid()
    )
  );
