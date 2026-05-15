-- US-041: extend match_chunks with two new columns per row that explain *why*
-- the viewer can see the chunk:
--   granting_principal_id      uuid    — null for owner; principal_id otherwise
--   granting_principal_display text    — 'owner' | viewer's email | group name
--
-- Precedence when more than one rule grants access to the same chunk:
--   1. owner (chunks.user_id = auth.uid())
--   2. direct user grant (chunk_acl.principal_type='user' and principal_id=auth.uid())
--   3. group grant (via principal_membership)
-- Within group grants the choice is made deterministic by tie-breaking on
-- chunk_acl.created_at ASC, then ca.principal_id ASC — so the badge shown to
-- the viewer is stable across runs even when multiple groups grant the same
-- chunk.
--
-- Implementation: a DISTINCT ON (c.id) inner subquery with a CASE-driven
-- ORDER BY applies the precedence; the outer query re-sorts by HNSW distance
-- and applies LIMIT. The HNSW index is still used for the inner WHERE-time
-- filter (the planner can push the (1 - dist) >= match_threshold predicate
-- down to the index scan); we just lose the ability to use the index for the
-- final ordering. For typical match_count (5–50) this is fine — the inner
-- result set is already small after the threshold + permission filter.
--
-- Return type changes, so DROP-then-CREATE rather than CREATE OR REPLACE.

drop function if exists public.match_chunks(
  extensions.vector(1536), float, int, text[], text, date, date, int
);

create function public.match_chunks(
  query_embedding extensions.vector(1536),
  match_threshold float default 0.3,
  match_count int default 5,
  filter_topics text[] default null,
  filter_document_type text default null,
  filter_date_from date default null,
  filter_date_to date default null,
  ef_search int default null
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
language plpgsql
stable
security invoker
set search_path = public, extensions, pg_temp
as $$
begin
  if ef_search is not null then
    perform set_config('hnsw.ef_search', ef_search::text, true);
  end if;

  return query
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
      (1 - (c.embedding <=> query_embedding))::float as similarity,
      (c.embedding <=> query_embedding) as distance,
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
    where c.embedding is not null
      and d.deleted_at is null
      and (1 - (c.embedding <=> query_embedding)) >= match_threshold
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
  order by sub.distance asc
  limit greatest(match_count, 0);
end;
$$;

grant execute on function public.match_chunks(
  extensions.vector(1536), float, int, text[], text, date, date, int
) to authenticated;
