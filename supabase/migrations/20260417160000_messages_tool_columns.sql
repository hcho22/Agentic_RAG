-- US-012: Chat Completions is stateless, so we need to persist enough of every
-- turn (incl. intermediate assistant tool-call turns and the tool results) to
-- rebuild the conversation on the next request / after a page refresh.
--
-- Changes to public.messages:
--   * content is now nullable — an assistant turn that only emits tool_calls
--     often has no user-visible text.
--   * tool_calls jsonb — OpenAI-format list of {id, type, function: {name, arguments}}
--     attached to assistant rows that requested tools. Null for other roles.
--   * tool_call_id text — set on role='tool' rows to link the result back to
--     the assistant call that produced it.
--   * name text — optional tool name on role='tool' rows, for trace fidelity.
--
-- RLS is already scoped via the parent thread; no policy changes are needed.

alter table public.messages alter column content drop not null;

alter table public.messages add column tool_calls jsonb;
alter table public.messages add column tool_call_id text;
alter table public.messages add column name text;
