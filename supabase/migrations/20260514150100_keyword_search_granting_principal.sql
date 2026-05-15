-- US-041 follow-up: extend keyword_search with the same granting-principal
-- columns added to match_chunks (20260514150000_match_chunks_granting_principal.sql)
-- so the demo badge renders for chunks that came in via the keyword half of
-- hybrid retrieval (or via keyword-only retrieval when RETRIEVAL_MODE=keyword).
--
-- Without this, hybrid retrieval would surface the badge only when the chunk
-- is also returned by the vector side — RRF's first-seen-wins keeps the
-- vector row when both sides find it, but for keyword-only matches the row
-- carries null granting fields and the badge is suppressed.
--
-- The precedence resolution mirrors match_chunks exactly: owner > direct
-- user grant > group grant, with a stable tie-break inside group grants by
-- chunk_acl.created_at ASC, principal_id ASC.

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
