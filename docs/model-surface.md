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
| `OPENAI_RERANK_MODEL` | `llm` reranker (only when `RERANKER=llm`) | `OPENAI_MODEL` |
| `EMBEDDER_MODEL` | Embedder | `EMBEDDING_MODEL` → `text-embedding-3-small` |
| `JUDGE_MODEL` | Runtime faithfulness gate (US-048) | `gpt-4o-mini` (does **not** chain to `OPENAI_MODEL`) |
| `CHAT_MODE_DEFAULT` | Answerer chat surface | `responses` (OpenAI proper, no `base_url`) / `completions` (Azure or `openai` + `base_url`) |

> **`JUDGE_MODEL` is a `judge`-role selector, not an answerer one.** Its
> provider/connection comes from the `judge` role's `JUDGE_*` binding (above),
> not the answerer, and unlike the aux-helper selectors it defaults to a cheap
> model **without** chaining through `OPENAI_MODEL` — the per-reply runtime gate
> stays cheap even behind a large answerer. On a non-OpenAI judge, set
> `JUDGE_MODEL` to your deployment/model id; an unset/wrong value just makes the
> judge call fail, which fails **closed** (escalate), never auto-sends a reply.

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
