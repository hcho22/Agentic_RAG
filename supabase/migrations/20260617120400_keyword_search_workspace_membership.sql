-- US-004 (ADR-0002): enforce the workspace tenant boundary INSIDE keyword_search,
-- identically to match_chunks (US-003, 20260617120300). This leaves NO un-scoped
-- retrieval entry point: the keyword leg — and therefore the keyword half of
-- hybrid_search (backend/retrieval.py) — applies the same boundary as the vector
-- leg. Without this, a query that finished US-003 (vector locked) would still
-- leak across workspaces through the keyword leg of hybrid retrieval.
--
-- The clause is byte-identical to the one added to match_chunks and AND-ed under
-- the same owner-OR-ACL predicate, so the boundary is subtractive (even the owner
-- of a document loses visibility if not a member of its workspace). It references
-- only d.workspace_id (from the existing documents join), auth.uid(), and
-- workspace_membership — no role column, no function parameter carrying a
-- workspace id.
--
-- Because the clause is server-side and auth.uid()-resolved, NO change is needed
-- in backend/retrieval.py: the backend keeps passing only _supabase_headers(user)
-- and both halves of hybrid_search inherit the boundary automatically.
--
-- DROP-and-CREATE: signature and return shape are byte-identical to
-- 20260514150100_keyword_search_granting_principal.sql, so the GRANT is re-issued
-- unchanged. Over the Default Workspace the clause is a no-op (every legacy doc +
-- user is a member), so keyword/hybrid retrieval is unchanged for the legacy
-- corpus; the boundary only bites once a second workspace exists.

drop function if exists public.keyword_search(
  text, int, text[], text, date, date
);

create function public.keyword_search(
  query text,
  match_count int default 5,
  filter_topics text[] default null,
  filter_document_type text default null,
  filter_date_from date default null,
  filter_date_to date default null
)
returns table (
  id uuid,
  document_id uuid,
  chunk_index int,
  content text,
  similarity float,
  filename text,
  granting_principal_id uuid,
  granting_principal_display text
)
language sql
stable
security invoker
set search_path = public, pg_temp
as $$
  with q as (
    select websearch_to_tsquery('english'::regconfig, coalesce(query, '')) as tsq
  )
  select
    sub.id,
    sub.document_id,
    sub.chunk_index,
    sub.content,
    sub.similarity,
    sub.filename,
    sub.granting_principal_id,
    sub.granting_principal_display
  from (
    select distinct on (c.id)
      c.id,
      c.document_id,
      c.chunk_index,
      c.content,
      ts_rank_cd(c.content_tsv, q.tsq)::float as similarity,
      d.filename,
      case
        when c.user_id = auth.uid() then null::uuid
        else ca.principal_id
      end as granting_principal_id,
      case
        when c.user_id = auth.uid() then 'owner'::text
        when ca.principal_type = 'user' then (
          select p.email from public.profiles p where p.id = auth.uid()
        )
        when ca.principal_type = 'group' then (
          select pr.name from public.principals pr where pr.id = ca.principal_id
        )
      end as granting_principal_display
    from public.chunks c
    join public.documents d on d.id = c.document_id
    left join public.chunk_acl ca on ca.chunk_id = c.id
      and (
        (ca.principal_type = 'user' and ca.principal_id = auth.uid())
        or (
          ca.principal_type = 'group'
          and ca.principal_id in (
            select pm.principal_id
            from public.principal_membership pm
            where pm.member_user_id = auth.uid()
          )
        )
      )
    cross join q
    where c.content_tsv @@ q.tsq
      and d.deleted_at is null
      and (c.user_id = auth.uid() or ca.chunk_id is not null)
      and exists (
        select 1
        from public.workspace_membership wm
        where wm.workspace_id = d.workspace_id
          and wm.user_id = auth.uid()
      )
      and (
        filter_topics is null
        or (d.metadata ? 'topics' and d.metadata->'topics' ?| filter_topics)
      )
      and (
        filter_document_type is null
        or d.metadata->>'document_type' = filter_document_type
      )
      and (
        filter_date_from is null
        or (d.metadata->>'published_date')::date >= filter_date_from
      )
      and (
        filter_date_to is null
        or (d.metadata->>'published_date')::date <= filter_date_to
      )
    order by
      c.id,
      case
        when c.user_id = auth.uid() then 1
        when ca.principal_type = 'user' then 2
        when ca.principal_type = 'group' then 3
      end asc,
      ca.created_at asc nulls first,
      ca.principal_id asc nulls first
  ) sub
  order by sub.similarity desc, sub.id asc
  limit greatest(match_count, 0);
$$;

grant execute on function public.keyword_search(
  text, int, text[], text, date, date
) to authenticated;
