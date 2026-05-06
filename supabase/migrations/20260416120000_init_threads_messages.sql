-- Initial schema: threads + messages with RLS scoped to auth.uid().
-- pgvector is enabled here so later modules (embeddings) can add vector columns
-- without a separate extension migration.

create extension if not exists vector with schema extensions;
create extension if not exists pgcrypto;

create table public.threads (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  title text,
  created_at timestamptz not null default now()
);

create index threads_user_id_created_at_idx
  on public.threads (user_id, created_at desc);

create table public.messages (
  id uuid primary key default gen_random_uuid(),
  thread_id uuid not null references public.threads(id) on delete cascade,
  role text not null check (role in ('user', 'assistant', 'system', 'tool')),
  content text not null,
  created_at timestamptz not null default now()
);

create index messages_thread_id_created_at_idx
  on public.messages (thread_id, created_at asc);

alter table public.threads enable row level security;
alter table public.messages enable row level security;

create policy threads_select_own on public.threads
  for select using (auth.uid() = user_id);

create policy threads_insert_own on public.threads
  for insert with check (auth.uid() = user_id);

create policy threads_update_own on public.threads
  for update using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

create policy threads_delete_own on public.threads
  for delete using (auth.uid() = user_id);

-- messages inherit access from their parent thread's owner
create policy messages_select_own on public.messages
  for select using (
    exists (
      select 1 from public.threads t
      where t.id = messages.thread_id and t.user_id = auth.uid()
    )
  );

create policy messages_insert_own on public.messages
  for insert with check (
    exists (
      select 1 from public.threads t
      where t.id = messages.thread_id and t.user_id = auth.uid()
    )
  );

create policy messages_update_own on public.messages
  for update using (
    exists (
      select 1 from public.threads t
      where t.id = messages.thread_id and t.user_id = auth.uid()
    )
  )
  with check (
    exists (
      select 1 from public.threads t
      where t.id = messages.thread_id and t.user_id = auth.uid()
    )
  );

create policy messages_delete_own on public.messages
  for delete using (
    exists (
      select 1 from public.threads t
      where t.id = messages.thread_id and t.user_id = auth.uid()
    )
  );
