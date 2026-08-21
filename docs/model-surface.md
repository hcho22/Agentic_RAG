# Model surface: configuring your own model host

This kit does not hard-wire one AI vendor. Every piece of runtime generation
programs against the OpenAI **Chat Completions** (and **Embeddings**) request /
response / streaming contract, so a "provider" is anything that faithfully
speaks that contract. This page is the operator's reference for binding your own
host: every environment variable, the role-fallback precedence, a worked Azure
example, an honest capability matrix (tested vs supported-but-untested vs
out-of-scope), and the embedder re-index procedure.

Design rationale lives in **ADR-0006** (the model-surface boundary) and
**CONTEXT.md → "Model surface"**; this page is the how-to.

## The three runtime roles

Provider binds **per role**; model binds **per call-site**. There are exactly
three runtime roles, each resolved once at startup into a typed `ProviderConfig`
(`backend/model_config.py`):

| Role | What it does | Client |
| --- | --- | --- |
| **answerer** | All text generation — the chat answer *and* the five auxiliary helpers (metadata extraction, query planner, text-to-SQL, document subagent, the `llm` reranker), which share the answerer's provider and only vary the *model* per call-site (US-023). | `openai_client` |
| **embedder** | Embeds chunks at ingestion and queries at retrieval time. Guarded fail-closed against drift (US-027 — see [Embedder re-index](#embedder-re-index-procedure)). | `embedder_client` |
| **judge** | The runtime faithfulness judge (Chat Completions contract). | `judge_client` |

> The **offline cross-family Claude eval judge** is a different thing entirely —
> a fixed measurement instrument owned by the eval harness (native
> `AsyncAnthropic`, ADR-0005). It is **not** part of this surface and is excluded
> from every model-surface guard. Don't try to configure it here.

## Provider + connection variables (per role)

The **answerer** is the base role and reads the bare variables. The **embedder**
and **judge** read `EMBEDDER_*` / `JUDGE_*` variables and **fall back to the
answerer config** for anything unset — so a single-provider deployment sets only
the answerer vars, and "answer on Azure, embed on OpenAI" is just two extra vars.

### Answerer (base role — bare variables)

| Var | Provider | Required? | Notes |
| --- | --- | --- | --- |
| `LLM_PROVIDER` | both | no | `openai` (default) or `azure`. A typo fails closed at startup. |
| `OPENAI_API_KEY` | openai | yes (openai) | |
| `OPENAI_BASE_URL` | openai | no | Point at any OpenAI-compatible endpoint (supported-but-untested — see the matrix). |
| `AZURE_OPENAI_API_KEY` | azure | yes (azure) | **No `OPENAI_API_KEY` fallback** — the OpenAI key is never bled into Azure (US-024). |
| `AZURE_OPENAI_ENDPOINT` | azure | yes (azure) | `https://<resource>.openai.azure.com` |
| `AZURE_OPENAI_API_VERSION` | azure | yes (azure) | **No default** — a missing version fails closed at startup, not on the first request. |
| `AZURE_OPENAI_DEPLOYMENT` | azure | no | The deployment **name** (≠ model id — see below). Unset → the per-call model id is used as the deployment. |

`provider=azure` is **fail-closed**: all three of api-key / endpoint /
api-version must resolve or the client raises at build time (US-024). A
half-configured Azure host never starts.

### Embedder (`EMBEDDER_*`) and Judge (`JUDGE_*`)

Each role reads the same six suffixes, prefixed, and inherits from the answerer
when unset:

| Var (embedder shown; `JUDGE_*` identical) | Falls back to |
| --- | --- |
| `EMBEDDER_PROVIDER` | answerer provider |
| `EMBEDDER_API_KEY` | answerer api-key (same provider only, **and only when `EMBEDDER_BASE_URL` is not overridden** — see below) |
| `EMBEDDER_BASE_URL` | answerer `base_url` (openai only) |
| `EMBEDDER_AZURE_OPENAI_ENDPOINT` | answerer Azure endpoint |
| `EMBEDDER_AZURE_OPENAI_API_VERSION` | answerer Azure api-version |
| `EMBEDDER_AZURE_OPENAI_DEPLOYMENT` | **not inherited** (per-role — a chat deployment can't embed) |

The Azure **deployment** is the one var that is deliberately *not* inherited: a
chat deployment is the wrong target for embeddings, so each azure-bound role
either sets its own deployment or lets its per-call model id be the deployment.

> **Credential safety — overriding `*_BASE_URL` requires a role key.** A role
> that sets its own `EMBEDDER_BASE_URL` / `JUDGE_BASE_URL` points at a **distinct
> host**, so it must supply its own `EMBEDDER_API_KEY` / `JUDGE_API_KEY`. Setting
> the base_url override **without** a role api-key **fails closed at startup** —
> the answerer's `OPENAI_API_KEY` is never forwarded to a different host. The
> api-key is inherited only when the role talks to the *same* host (no base_url
> override).

## Model-selection variables (per call-site)

A `ProviderConfig` carries no model name — model selection stays per call-site,
all within the answerer provider (the aux helpers never switch `base_url`). Each
selector falls back so a single-model setup sets only `OPENAI_MODEL`.

> **Azure caveat — a pinned deployment makes these selectors inert.** When an
> azure-bound role pins `AZURE_OPENAI_DEPLOYMENT` (mode (a) below),
> `AsyncAzureOpenAI` URL-templates *every* request to
> `/openai/deployments/{deployment}/` and Azure routes by URL **path**, ignoring
> the request-body `model`. So for that role the per-call selectors
> (`OPENAI_MODEL`, `METADATA_MODEL`, `OPENAI_PLANNER_MODEL`, `OPENAI_SQL_MODEL`,
> `OPENAI_SUBAGENT_MODEL`, `OPENAI_RERANK_MODEL`) are **inert**: all five answerer
> helpers run on the single pinned chat deployment regardless of their overrides.
> To vary models per call-site on Azure, use **mode (b)** — name each Azure
> deployment identically to its model id and leave `AZURE_OPENAI_DEPLOYMENT`
> unset, so the per-call model id becomes the deployment.

| Var | Call-site | Falls back to |
| --- | --- | --- |
| `OPENAI_MODEL` | Answerer + default for all aux helpers | `gpt-4o-mini` |
| `METADATA_MODEL` | Document metadata extraction | `OPENAI_MODEL` |
| `OPENAI_PLANNER_MODEL` | Query planner | `OPENAI_MODEL` |
| `OPENAI_SQL_MODEL` | Text-to-SQL generation | `OPENAI_MODEL` |
| `OPENAI_SUBAGENT_MODEL` | Document subagent | `OPENAI_MODEL` |
| `OPENAI_RERANK_MODEL` | `llm` reranker (only when `RERANKER=llm`, or the eval runner's `--reranker llm`) | `OPENAI_MODEL` |
| `EMBEDDER_MODEL` | Embedder | `EMBEDDING_MODEL` → `text-embedding-3-small` |
| `JUDGE_MODEL` | Runtime faithfulness gate (US-048) + answer-completeness gate (issue #97) | `gpt-4o-mini` (does **not** chain to `OPENAI_MODEL`) |
| `JUDGE_TEMPERATURE` | Runtime faithfulness + answer-completeness gates | `0` (unset **or** blank); the literal `none` omits the parameter |
| `JUDGE_REASONING_EFFORT` | Runtime faithfulness + answer-completeness gates | unset (**omitted** unset **or** blank); any set value is passed through verbatim |
| `CHAT_MODE_DEFAULT` | Answerer chat surface | `responses` (OpenAI proper, no `base_url`) / `completions` (Azure or `openai` + `base_url`) |

> **Two independent ways a reasoning-model answerer breaks the aux helpers.**
> Four aux helpers generate text off the answerer model - the query planner
> (`OPENAI_PLANNER_MODEL`, `backend/planner.py:300`), text-to-SQL
> (`OPENAI_SQL_MODEL`, `backend/text_to_sql.py:315`), the `llm` reranker
> (`OPENAI_RERANK_MODEL`, `backend/reranking.py:231`), and the document sub-agent
> (`OPENAI_SUBAGENT_MODEL`, `backend/subagent.py:500`) - and each falls through to
> `OPENAI_MODEL` when its own selector is unset. Migrating `OPENAI_MODEL` to a
> reasoning family (o-series / `gpt-5-*`) can 400 an inheriting helper in **two
> independent ways**, and a pin is the remedy for either:
>
> 1. **The `temperature` argument.** Three of the helpers send a hardcoded
>    `temperature=0.0` on every call - the query planner (`backend/planner.py:308`),
>    text-to-SQL (`backend/text_to_sql.py:322`), and the `llm` reranker
>    (`backend/reranking.py:238`). A model that refuses the `temperature` *argument*
>    400s on the first such call. This is the same class the `JUDGE_TEMPERATURE`
>    note below describes, and it is the class the boot warning detects.
> 2. **Function tools on the Chat Completions surface.** On `/v1/chat/completions`
>    the answerer's completions fallback registers tools
>    (`tools.append(spawn_document_agent_tool_schema())`, `backend/main.py:1674`;
>    the call is at `backend/main.py:1679`), the document sub-agent runs with
>    `tool_choice="auto"` (`backend/subagent.py:507`), and the query planner with
>    `tool_choice="required"` (`backend/planner.py:307`) - all three **always send
>    function tools**. US-119 reproduced a first-party OpenAI 400 in which a
>    reasoning model refuses function tools on the completions surface at any
>    `reasoning_effort` above `none`. So such a model 400s on the **tools
>    themselves**, *independent of `temperature`*. This is a broader class than
>    class 1 and is why the sub-agent must be pinned too: it sends no `temperature`
>    and cannot hit class 1, but on the completions surface it does **not** merely
>    degrade its `tool_choice="auto"` loop - it 400s on the tools. (The main
>    answerer's default Responses mode does not send tools this way; its completions
>    fallback - Azure, or `openai` + `base_url` - does, and the sub-agent and
>    planner always run on completions.)
>
> Startup logs **one** advisory warning naming each unpinned helper and its pinning
> env var (`warn_if_answerer_rejects_temperature`, `backend/escalation.py`); it
> detects the **class-1** `temperature` refusal only, by a best-effort name match
> against `_TEMPERATURE_REFUSING_MODEL_PREFIXES` (minus the non-reasoning `-chat`
> marker), wrong in both directions: a refusing model under an unknown name gets no
> warning, and a matching name is a family guess rather than an observed refusal, so
> **verify before pinning**. A helper whose selector carries any explicit value is
> the operator's deliberate choice and is skipped; only unset/empty selectors are
> named. Unlike the judge warning below this is **not** widget-scoped - these
> helpers run on the core knowledge-assistant path that every deploy runs, so it
> takes no `support_configured` gate. It never raises, never blocks startup, and
> never changes model selection. Remedy for **either** class: pin each named helper
> to a compatible model via its env var.

> **`JUDGE_MODEL` is a `judge`-role selector, not an answerer one.** Its
> provider/connection comes from the `judge` role's `JUDGE_*` binding (above),
> not the answerer, and unlike the aux-helper selectors it defaults to a cheap
> model **without** chaining through `OPENAI_MODEL` — the per-reply runtime gate
> stays cheap even behind a large answerer. This non-chaining is also what keeps
> the gates **pinned** across an answerer migration: migrating `OPENAI_MODEL` to a
> temperature-refusing reasoning model does **not** drag the judge with it, so the
> gates' `temperature=0` pin (below) is unaffected - the judge stays on its own
> non-reasoning model unless you deliberately point `JUDGE_MODEL` at a reasoning
> one. On a non-OpenAI judge, set
> `JUDGE_MODEL` to your deployment/model id; an unset/wrong value just makes the
> judge call fail, which fails **closed** (escalate), never auto-sends a reply -
> but read the residual-risk paragraph under `JUDGE_TEMPERATURE` below before
> repointing production: a judge that fails on *every* call latches the
> conversations it hits to `escalated` permanently (issue #105).

> **`JUDGE_TEMPERATURE` pins the two runtime gates' sampler.** Both gates fail
> **closed**, and a gate that returns a different verdict on identical input is
> sampling one rather than deciding it - the 2026-08-03 E7 investigation measured
> the answer gate returning `answers=true` on 2 of 5 identical calls for the same
> (question, draft) pair (issue #104). Both gates therefore send `temperature=0`
> by default. That **removes the sampler** as a source of variance in the
> send/escalate verdict; it is best-effort, not a guarantee - no `seed` is passed
> and provider-side temperature 0 is itself best-effort, so a differing verdict on
> an identical pair is unlikely rather than impossible.
>
> **Two different rejections, two different remedies.** Do not reach for the
> opt-out on both.
>
> 1. **The deployment rejects the `temperature` *argument*.** First-party OpenAI
>    **reasoning models** (o-series, `gpt-5-*`) do this, at *any* value including
>    the shipped default, so it is a 400 on **every** call from the moment you
>    point `JUDGE_MODEL` there. **Remedy: `JUDGE_TEMPERATURE=none`**, which omits
>    the parameter entirely. No number works.
> 2. **The endpoint rejects the *value* as outside its own accepted range** (an
>    Anthropic-compatible endpoint caps at 1.0 and answers `temperature: Input
>    should be less than or equal to 1`). This **cannot happen at the default**:
>    the default is `0`, which is inside every provider's range including `[0,1]`.
>    It only fires once you have explicitly set a number above that endpoint's cap.
>    **Remedy: set a value that endpoint accepts** - `0` keeps the gates pinned.
>    `JUDGE_TEMPERATURE=none` also stops the 400, but it un-pins a safety gate you
>    could have kept pinned, so use it only if you would rather send nothing at all.
>
> The knob validates `[0,2]` (OpenAI's range) and that is unchanged - but it does
> **not** protect you from case 2, and is not trying to: a validator cannot know
> each bring-your-own endpoint's range. Clearing `[0,2]` is necessary, not
> sufficient.
>
> Case 1 is also flagged **at boot**: startup logs a warning when `JUDGE_MODEL`
> matches a **known** reasoning-model name and the pin is in effect. That check is
> a hand-maintained name match (`_TEMPERATURE_REFUSING_MODEL_PREFIXES` in
> `backend/escalation.py` - edit it when a new refusing model ships), minus any
> name carrying the non-reasoning `-chat` marker (`_NON_REASONING_CHAT_MARKER`,
> e.g. `gpt-5-chat-latest`). It is **best-effort only**, wrong in both directions.
> It does not guarantee detection, does not prevent the breakage, and does not
> make the upgrade safe: a judge model it does not recognise - anything newer than
> the list, or an OpenAI-compatible endpoint under another name - gets **no
> warning at all**. The `-chat` exclusion is a naming *convention* rather than a
> list, so it holds across dotted point releases instead of rotting, but it is
> correspondingly broad: it errs toward **more missed warnings**, never toward
> more false alarms, so a refusing deployment whose name happens to carry `-chat`
> is also silently skipped. And a name it *does* match is a family guess rather than
> an observed refusal, so **the warning is not proof your judge rejects the
> parameter** - verify that judge calls are actually 400ing before setting
> `JUDGE_TEMPERATURE=none`, or you will un-pin a gate that was working. It reduces
> the chance of a silent surprise; it is not a safety net. It never blocks startup
> and never changes a gate decision.
>
> The warning is also scoped to the **support-widget** surface: those gates only
> run on the widget path, so a knowledge-assistant-only deploy (no
> `SUPABASE_SERVICE_ROLE_KEY`, every `/widget/*` route 503s) makes no judge call
> and gets no warning regardless of `JUDGE_MODEL` / `JUDGE_TEMPERATURE`. Nothing
> in this section applies to such a deploy.
>
> The remedy is deliberately a typed-out configuration statement rather than
> something the gates infer from the provider's error text. Classifying free-text
> 400s from arbitrary providers on a safety path is unsafe in *both* directions -
> read too loosely, a gateway echoing the request payload into an unrelated error
> un-pins a safety gate; read too strictly, a real rejection goes unrecognised and
> the gate fails closed anyway. So a judge call is exactly one call with no retry,
> and every failure - auth, rate limit, timeout, network, any 400 - fails closed.
>
> **Why that matters, and the residual risk.** A judge that fails on *every* call
> makes both gates fail closed, `run_deflection_pipeline` returns
> `action='escalated'`, and the latch site in
> `backend/main.py` tests only `result.turn.escalated` - it cannot tell that from
> a **deliberate** ADR-0003 escalate, so it calls `_escalate_conversation_safe`
> and pins `conversations.status='escalated'`. That transition is one-way and
> DB-trigger-enforced (AGENTS.md invariant 5), so the blast radius is **permanent
> per-conversation bot silence**, not merely lost deflection. Repairing the
> configuration stops NEW conversations from latching; it does **not** un-latch
> the ones already latched, which stay `escalated` with the bot silent in them,
> because the status transition cannot be reversed. The failure is not
> self-healing, so verify a new `JUDGE_MODEL` answers before pointing production
> at it. The underlying defect - that a transient or misconfigured judge failure
> is indistinguishable from a deliberate escalate
> at the latch site, contradicting both invariant 8 ("a degraded/transient failure
> defers this turn but does NOT latch") and the `_escalate_conversation_safe`
> docstring - is **not yet fixed**; issue #105 tracks it.
>
> **How the knob resolves.** The literal `none` is the **only** way to un-pin -
> unset and blank both mean the pinned default, so a bare `-e JUDGE_TEMPERATURE`
> or a trailing `JUDGE_TEMPERATURE=` in
> a `.env` cannot silently return a safety gate to sampling. A value that is not
> a number, not finite, or outside `[0,2]` logs a warning and falls back to `0`
> rather than failing the boot: a fat-fingered knob must not take a safety gate
> offline. Setting a real number is an explicit operator decision to give up that
> determinism.

> **`JUDGE_REASONING_EFFORT` - the reasoning-model judge knob (ADR-0013).** A
> reasoning-model judge (`gpt-5-mini`, adopted per ADR-0013) is latency-viable on
> the inline reply path only at `reasoning_effort=minimal`; the two gates now read
> `JUDGE_REASONING_EFFORT` from env and splat it into their one judge call. The
> default is **omit**, not a value: unset **or** blank sends no `reasoning_effort`
> at all, so the call stays byte-identical to what a non-reasoning judge
> (`gpt-4o-mini`) has always sent - which is required, because a non-reasoning
> judge *400s* on the parameter and `gpt-5.4-mini` *400s on the value* `minimal`.
> A value is validated by nowhere in this repo: whatever you set is passed to the
> judge API verbatim (surrounding whitespace stripped) and the API accepts or
> rejects it, so pick the value your judge model accepts. Startup logs a second
> advisory warning (`warn_if_judge_rejects_reasoning_effort`,
> `backend/escalation.py`), the mirror of the temperature one: it fires when
> `JUDGE_REASONING_EFFORT` is set **but** `JUDGE_MODEL` does **not** look like a
> known reasoning family - the migration slip of setting the value while leaving the
> non-reasoning default `gpt-4o-mini` in place, which 400s the parameter and latches
> conversations via the same issue #105 path. It is the same widget-scoped
> best-effort name heuristic, wrong in both directions (a reasoning model under an
> unrecognised name gets a spurious warning), so **verify before changing config**.
> **The adopted
> `gpt-5-mini` judge config is `JUDGE_MODEL=gpt-5-mini` + `JUDGE_TEMPERATURE=none`
> + `JUDGE_REASONING_EFFORT=minimal`.** `JUDGE_TEMPERATURE=none` is mandatory there
> (`gpt-5-mini` 400s on any temperature); the boot warning above **correctly**
> fires for `gpt-5-mini` until you set it, and that config **deliberately** relaxes
> invariant 8's temperature-0 determinism pin as a quality trade (halving the
> dangerous false-resolve rate). See ADR-0013 for the full rationale and evidence.

Rerankers (`COHERE_RERANK_MODEL` / `VOYAGE_RERANK_MODEL`) are a **separate
provider axis** (dedicated rerank endpoints) and are not part of this surface.

## Worked example: Azure OpenAI

Azure addresses **deployment names**, not model ids: a request URL-templates to
`/openai/deployments/{deployment}/chat/completions?api-version=…`. Keep the
deployment **name** distinct from the per-call model **id**. Two ways to satisfy
this:

- **(a) Set the deployment explicitly** per azure-bound role (clearest), or
- **(b) Name your Azure deployment identically to the model id** and leave the
  deployment var unset — the SDK then uses the per-call model id as the
  deployment.

### All-Azure (answerer + embedder + judge on one Azure resource)

```bash
LLM_PROVIDER=azure
AZURE_OPENAI_ENDPOINT=https://my-resource.openai.azure.com
AZURE_OPENAI_API_VERSION=2024-10-21
AZURE_OPENAI_API_KEY=<azure-key>

# Answerer: deployment NAME (what you called it in the Azure portal). Because
# this is pinned (mode (a)), ALL five answerer helpers run on this one
# deployment — the per-call *_MODEL selectors are inert (see the Azure caveat
# above). Use mode (b) if you need to vary models per call-site.
AZURE_OPENAI_DEPLOYMENT=gpt-4o-mini-prod
# Per-call model id. With the deployment pinned above, Azure ignores this in the
# request body and routes by URL path; it only takes effect in mode (b).
OPENAI_MODEL=gpt-4o-mini

# Embedder: its own deployment — a chat deployment cannot embed. (Endpoint,
# api-version and key are inherited from the answerer; only the deployment and
# model are role-specific.)
EMBEDDER_AZURE_OPENAI_DEPLOYMENT=text-embedding-3-small-prod
EMBEDDER_MODEL=text-embedding-3-small
```

### Split: answer on Azure, embed on OpenAI

```bash
LLM_PROVIDER=azure
AZURE_OPENAI_ENDPOINT=https://my-resource.openai.azure.com
AZURE_OPENAI_API_VERSION=2024-10-21
AZURE_OPENAI_API_KEY=<azure-key>
AZURE_OPENAI_DEPLOYMENT=gpt-4o-prod
OPENAI_MODEL=gpt-4o

# Embedder overrides back to OpenAI proper — two vars:
EMBEDDER_PROVIDER=openai
EMBEDDER_API_KEY=sk-...
EMBEDDER_MODEL=text-embedding-3-small
```

**Auth is api-key only in v1.** Microsoft Entra ID (AAD) token auth is a
documented future seam, intentionally deferred (see the matrix).

### Verify it took effect

`GET /healthz` reports the resolved binding so you can confirm a split
deployment without reading logs:

```json
{
  "providers":        { "answerer": "azure", "embedder": "openai", "judge": "azure" },
  "azure_deployments":{ "answerer": "gpt-4o-prod", "judge": null },
  "embedding_model":  "text-embedding-3-small"
}
```

`azure_deployments` lists only azure-bound roles; `null` there means "no explicit
deployment — the per-call model id is used as the deployment."

## F3 capability matrix

What is tested, what is supported-but-untested, and what is deliberately out of
scope. "Tested" means exercised in CI / verified end-to-end.

| Target / capability | Status | Notes |
| --- | --- | --- |
| `provider=openai` (OpenAI proper) | ✅ **Tested** | First-class. The default. |
| `provider=azure` (Azure OpenAI) | ✅ **Tested** | Deployment-vs-model split, path-templating, `api-version` query param, **api-key auth only**. |
| `provider=openai` + custom `base_url` (vLLM, Together, Groq, Ollama, LM Studio, …) | ⚠️ **Supported, untested** | Anything that faithfully speaks the OpenAI Chat Completions + Embeddings contract should work, but it is **not** in CI — validate it yourself. |
| Native non-OpenAI **runtime** APIs (Anthropic Messages, Bedrock, Vertex native SDKs) | ❌ **Out of scope** | A non-OpenAI model reaches this surface **only** via an OpenAI-compatible endpoint. No native adapters. |
| **Responses mode** (`CHAT_MODE_DEFAULT=responses`: hosted `file_search` + server-side `previous_response_id` threading) | ⚠️ **OpenAI proper only (no `base_url` override), non-portable** | Requires `provider=openai` with **no** `base_url` override — the Responses endpoint doesn't exist on Azure or any OpenAI-compatible `base_url` host. Fails closed at startup on any non-capable answerer (FR-M4) — never a silent downgrade. `completions` is the portable cross-provider path and the default everywhere else. |
| Azure **Entra ID / AAD-token** auth | 🚧 **Deferred** | api-key auth only in v1; documented future seam (ADR-0006). |
| Per-call-site **provider / `base_url`** split | ❌ **Out of scope** | Provider binds per *role*; one chat host serves all text generation. Only the *model* varies per call-site. |
| Cohere / Voyage **rerankers** as a model-surface role | ❌ **Separate axis** | Dedicated rerank endpoints (`RERANKER=cohere|voyage`), orthogonal to answerer/embedder/judge. |
| Offline cross-family **Claude eval judge** | ❌ **Owned by the eval harness** | Native `AsyncAnthropic`, fixed instrument (ADR-0005). Not configurable through this surface. |

## Embedder re-index procedure

The retrieval index only works when the query embedding and the stored chunk
embeddings come from the **same** embedder. Swapping the embedder silently
breaks recall — most dangerously in the *same-dims-different-model* case (e.g.
`text-embedding-3-small` and `text-embedding-ada-002` are **both** 1536-dim, so
nothing errors). US-026 stamps the corpus (`embedding_config`: model + dim), and
the **US-027 startup guard** probe-embeds one string and **refuses to start** if
the running embedder's model or dimension no longer matches the stamp.

When the guard fires, the error names the stamped vs configured model/dim and
the remedy below — this section and that error are kept in sync. There are two
ways to clear it:

### Option A — revert the embedder (keep the existing corpus)

Set the embedder back to the stamped model (the model named in the error) and
restart. Nothing to re-index.

### Option B — re-embed the corpus under the new embedder

A **re-*embed*** recomputes every chunk's vector *in place*, under the new
model/provider:

1. **If the dimension changes** (e.g. `1536 → 3072`), first migrate the
   `chunks.embedding vector(N)` column to the new dimension. (Same model at the
   same dimension — a pure model swap — skips this step.)
2. **Re-embed** every chunk with the configured embedder (the bulk re-index path
   — the corpus / wikipedia seeders, run as service-role).
3. The re-index **overwrites** the `embedding_config` stamp to match what it just
   produced, so the guard passes on the next startup.

A re-embed **preserves chunk UUIDs**, and therefore the `chunk_acl` grants keyed
on those UUIDs survive — permissions and document identity are untouched. This is
the opposite of **re-*chunking*** (a different chunk size or a content edit),
which destroys chunk UUIDs and with them every grant (the *re-chunking caveat* in
[permissions-aware-rag.md](./permissions-aware-rag.md)). The US-027 remedy is
always a re-embed, never a re-chunk.

> The guard reads the stamp via the service-role key. If
> `SUPABASE_SERVICE_ROLE_KEY` is unset the stamp can't be read at startup (its
> RLS hides it from `anon`), so the guard logs `embedder_guard.disabled` and
> skips — set the service-role key to keep drift detection on.

## See also

- **CONTEXT.md → "Model surface (Phase 2, ADR-0006)"** — the condensed in-repo summary and the design rationale (the ADR-0006 decision set: per-role provider binding, Chat Completions contract, deferred seams).
- **`backend/model_config.py`** — the typed `ProviderConfig` and the env→config resolution these tables describe (authoritative if this page ever drifts).
- **README.md → "Environment variables"** — the full env-var reference for the rest of the backend.
