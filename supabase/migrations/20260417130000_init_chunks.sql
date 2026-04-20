-- US-008: chunks table for the BYO retrieval pipeline.
-- documents already exists from US-007 (20260417120000_init_documents.sql);
-- this migration only adds the child chunks table. Embeddings land in US-009.
--
-- user_id is denormalised onto chunks so retrieval RLS is a single-column
-- check without a join; chunk inserts still verify the parent document is
-- owned by the same user as defence-in-depth against a forged document_id.

create table public.chunks (
  id uuid primary key default gen_random_uuid(),
  document_id uuid not null references public.documents(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  chunk_index int not null,
  content text not null,
  created_at timestamptz not null default now(),
  unique (document_id, chunk_index)
);

create index chunks_user_id_idx on public.chunks (user_id);
create index chunks_document_id_idx on public.chunks (document_id);

alter table public.chunks enable row level security;

create policy chunks_select_own on public.chunks
  for select using (auth.uid() = user_id);

create policy chunks_insert_own on public.chunks
  for insert with check (
    auth.uid() = user_id
    and exists (
      select 1 from public.documents d
      where d.id = chunks.document_id and d.user_id = auth.uid()
    )
  );

create policy chunks_update_own on public.chunks
  for update using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

create policy chunks_delete_own on public.chunks
  for delete using (auth.uid() = user_id);
