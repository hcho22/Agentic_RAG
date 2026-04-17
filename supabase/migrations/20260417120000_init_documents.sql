-- US-007: documents table + private Storage bucket for the Ingestion page.
-- Chunking pipeline (chunks table, embeddings) is added in later modules;
-- this migration is intentionally narrow: just the document record + blob store
-- so the UI shell can upload, list, and soft-delete.

create table public.documents (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  filename text not null,
  storage_path text not null,
  byte_size bigint not null,
  content_type text,
  status text not null default 'queued'
    check (status in ('queued', 'processing', 'ready', 'error')),
  error_message text,
  chunks_count integer not null default 0,
  uploaded_at timestamptz not null default now(),
  deleted_at timestamptz
);

create index documents_user_id_uploaded_at_idx
  on public.documents (user_id, uploaded_at desc)
  where deleted_at is null;

alter table public.documents enable row level security;

create policy documents_select_own on public.documents
  for select using (auth.uid() = user_id);

create policy documents_insert_own on public.documents
  for insert with check (auth.uid() = user_id);

create policy documents_update_own on public.documents
  for update using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

create policy documents_delete_own on public.documents
  for delete using (auth.uid() = user_id);

-- Private Storage bucket. Objects are namespaced by user_id in the path so the
-- RLS policies below can enforce ownership on every read/write/delete.
insert into storage.buckets (id, name, public)
  values ('documents', 'documents', false)
  on conflict (id) do nothing;

create policy "documents bucket read own"
  on storage.objects for select
  using (
    bucket_id = 'documents'
    and auth.uid()::text = (storage.foldername(name))[1]
  );

create policy "documents bucket insert own"
  on storage.objects for insert
  with check (
    bucket_id = 'documents'
    and auth.uid()::text = (storage.foldername(name))[1]
  );

create policy "documents bucket update own"
  on storage.objects for update
  using (
    bucket_id = 'documents'
    and auth.uid()::text = (storage.foldername(name))[1]
  );

create policy "documents bucket delete own"
  on storage.objects for delete
  using (
    bucket_id = 'documents'
    and auth.uid()::text = (storage.foldername(name))[1]
  );
