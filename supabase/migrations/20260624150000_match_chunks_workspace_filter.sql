-- US-070 (ADR-0008, on ADR-0002): add an ORDINARY NON-SECURITY active-workspace
-- narrowing filter to match_chunks.
--
-- This is NOT a trust boundary. The boundary stays exactly what US-003
-- (20260617120300) made it: the owner-OR-ACL predicate AND the
-- workspace_membership EXISTS clause, both resolved from auth.uid() — never from
-- a backend-passed id. `filter_workspace_id` is a *narrowing* filter in the same
-- class as `filter_topics` / `filter_document_type`: when null it is a no-op;
-- when set it only ever REMOVES rows whose document is in a different workspace.
-- It can never WIDEN visibility (it is AND-ed, like every other filter), so a
-- wrong/forgotten value can only under-return within what auth.uid() may already
-- see — it cannot leak across the membership boundary. CONTEXT "Active workspace"
-- and ADR-0002 (the active-workspace path value is a non-security UX filter); the
-- US-007 acceptance note (prd-phase2 line 176) explicitly reserved this as "an
-- ordinary non-security narrowing filter (alongside filter_topics/
-- filter_document_type)" and the index `documents_workspace_id_idx`
-- (20260617120200) was pre-created to serve it.
--
-- FIRST CONSUMER: the support-bot per-turn retrieval (US-070). The bot is one
-- principal per workspace (US-069), so its membership already scopes it to a
-- single workspace; this filter is the explicit, defence-in-depth narrowing the
-- deflection pipeline passes from the conversation's workspace_id — the
-- conversation knows which workspace it belongs to, so retrieval states that
-- intent rather than relying on the bot happening to be a sole-workspace member.
-- The authenticated /api/chat path still passes NO workspace_id (US-007 left the
-- resolved active workspace deliberately out of retrieval), so for it this is a
-- pure no-op.
--
-- DROP-and-CREATE: the body is byte-identical to
-- 20260617120300_match_chunks_workspace_membership.sql except for (a) the new
-- trailing `filter_workspace_id uuid default null` parameter and (b) the single
-- narrowing AND clause added beside the other filter_* clauses. The return shape
-- is unchanged, so the GRANT is re-issued unchanged. Over any existing caller
-- (which omits the new param ⇒ null ⇒ no-op) match_chunks returns identical rows,
-- so E4/E6 and the permissions evals pass bit-for-bit.

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
  ef_search int default null,
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
      and exists (
        select 1
        from public.workspace_membership wm
        where wm.workspace_id = d.workspace_id
          and wm.user_id = auth.uid()
      )
      -- US-070 non-security narrowing filter: when set, restrict to one
      -- workspace's documents. AND-ed like the metadata filters below, so it can
      -- only subtract within what the membership clause above already allows.
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
  order by sub.distance asc
  limit greatest(match_count, 0);
end;
$$;

grant execute on function public.match_chunks(
  extensions.vector(1536), float, int, text[], text, date, date, int, uuid
) to authenticated;
