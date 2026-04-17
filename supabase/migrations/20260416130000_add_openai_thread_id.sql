-- US-004: track the OpenAI Responses API conversation handle per Supabase thread.
-- Stores the most recent response id so the backend can pass it as
-- `previous_response_id` to continue the conversation server-side.

alter table public.threads
  add column openai_thread_id text;
