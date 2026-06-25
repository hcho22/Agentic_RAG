-- US-070 (ADR-0008, on ADR-0002): add the SAME ordinary non-security
-- active-workspace narrowing filter to keyword_search that
-- 20260624150000_match_chunks_workspace_filter.sql adds to match_chunks.
--
-- WHY BOTH LEGS: hybrid_search (backend/retrieval.py) fuses match_chunks with
-- keyword_search. If only the vector leg honoured `filter_workspace_id`, a
-- keyword-only hit from a different workspace could still enter the fused result
-- set, so the active-workspace narrowing would be half-applied. Mirroring the
-- filter onto the keyword leg keeps the narrowing coherent across the whole
-- hybrid result — exactly as 20260617120400 mirrored the *membership* clause
-- onto keyword_search so no retrieval entry point was left un-scoped. (This is a
-- narrowing filter, not the trust boundary; the boundary is the
-- workspace_membership EXISTS clause, already present on both legs and untouched
-- here.) When `filter_workspace_id` is null it is a no-op.
--
-- DROP-and-CREATE: byte-identical to
-- 20260617120400_keyword_search_workspace_membership.sql except for the new
-- trailing `filter_workspace_id uuid default null` parameter and the single
-- narrowing AND clause. Return shape unchanged ⇒ GRANT re-issued unchanged.
-- Existing callers omit the param ⇒ null ⇒ identical rows (E4/E6 + permissions
-- evals pass bit-for-bit).

drop function if exists public.keyword_search(
  text, int, text[], text, date, date
);

create function public.keyword_search(
  query text,
  match_count int default 5,
  filter_topics text[] default null,
  filter_document_type text default null,
  filter_date_from date default null,
  filter_date_to date default null,
  filter_workspace_id uuid default null
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
      -- US-070 non-security narrowing filter (mirror of match_chunks): when set,
      -- restrict to one workspace's documents. AND-ed, so subtractive only.
      and (filter_workspace_id is null or d.workspace_id = filter_workspace_id)
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
  text, int, text[], text, date, date, uuid
) to authenticated;
