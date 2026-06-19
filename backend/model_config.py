"""US-021/US-022: typed provider-config for the OpenAI-compatible model surface (ADR-0006).

Today the model surface is a single bare `AsyncOpenAI(api_key=…)` built in
main.py, with connection settings re-read from `os.environ` ad hoc across
several modules. This module replaces that with one validated place that maps
a *model role* (answerer / embedder / runtime-judge) to a provider and its
connection params, plus a factory that turns that config into a configured
async client.

Two providers are first-class: `openai` (the default — and any
OpenAI-compatible endpoint via `base_url`) and `azure` (Azure OpenAI). The
portable contract programmed against everywhere is the OpenAI Chat Completions
request/response/streaming shape; `AsyncAzureOpenAI` is an `AsyncOpenAI`
subclass, so that surface is identical for either provider (ADR-0006).

Azure target (US-024): `provider=azure` is **fail-closed** — it requires
`AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_VERSION`, and `AZURE_OPENAI_API_KEY`
all present (no api-version default, no `OPENAI_API_KEY` fallback), so a
half-configured Azure host raises at build time rather than failing on the
first request. Azure addresses per-resource **deployment names**, not model
ids: an optional `AZURE_OPENAI_DEPLOYMENT` (per role) is the deployment, kept
**distinct from** the per-call-site `*_MODEL` selectors (US-023). When set it
is pinned on the client so requests URL-template to
`/openai/deployments/{deployment}/chat/completions?api-version=…`; when unset
the SDK uses the per-call `model` argument as the deployment. Auth is
**api-key only in v1** — Microsoft Entra ID (AAD) token auth is a documented
future seam, deferred (ADR-0006 / F3 capability matrix). All generation code
keeps calling the Chat Completions shape unchanged; only client construction
here differs between providers (no Azure branch in planner/metadata/SQL/
subagent/reranker).

Per-role binding (US-022): each role resolves from its own env precedence —
role-specific vars (`EMBEDDER_PROVIDER` / `EMBEDDER_API_KEY` / `EMBEDDER_BASE_URL`
/ `EMBEDDER_AZURE_OPENAI_*`; the `JUDGE_*` equivalents) that **fall back to the
answerer config** when unset. The answerer is the base role and reads the bare
vars (`LLM_PROVIDER`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `AZURE_OPENAI_*`).
So "answer on Azure, embed on OpenAI" is just two env vars, and a single-provider
deployment sets nothing extra — embedder/judge inherit the answerer. One
exception (credential safety): a role that overrides its own `*_BASE_URL` points
at a distinct host, so it must supply its own `*_API_KEY` — the answerer's
openai api_key is inherited only when the role does NOT override base_url (same
host); overriding base_url without a role key fails closed.

Model *selection* stays per call-site (e.g. `OPENAI_MODEL` / `EMBEDDER_MODEL`):
a ProviderConfig deliberately carries no model name. Rerankers (Cohere/Voyage,
reranking.py) are a SEPARATE provider axis and are not part of this surface.

This mirrors the env→value-object→`build_*` factory shape already used by
`build_web_search_provider` (web_search.py) and `build_reranker`
(reranking.py): a small validated config resolved from env, consumed by a
factory that raises on misconfiguration at build time rather than at first
request.
"""

from __future__ import annotations

import os
from typing import Literal

from openai import AsyncAzureOpenAI, AsyncOpenAI
from pydantic import BaseModel, ConfigDict

Provider = Literal["openai", "azure"]
Role = Literal["answerer", "embedder", "judge"]
# US-025: the runtime chat surface. `responses` (OpenAI Responses API: hosted
# file_search + server-side previous_response_id threading) runs on OpenAI proper
# only (provider=openai with no base_url override); `completions` (Chat
# Completions) is the portable cross-provider path. See `responses_capable` /
# `resolve_chat_mode_default` for the fail-closed binding (FR-M4).
ChatMode = Literal["responses", "completions"]

# Env-var prefix per role. The answerer (None) reads the bare base vars; the
# other roles read `{PREFIX}_*` and fall back to the answerer config.
_ROLE_PREFIX: dict[Role, str | None] = {
    "answerer": None,
    "embedder": "EMBEDDER",
    "judge": "JUDGE",
}

# A recent GA api-version, used ONLY as a defensive fallback for a ProviderConfig
# constructed directly (e.g. in a test). The env path `from_env` does NOT default
# it — US-024 requires AZURE_OPENAI_API_VERSION to be set and fails closed if it
# is missing (no silent version pinning).
DEFAULT_AZURE_API_VERSION = "2024-10-21"


def _env(name: str) -> str | None:
    """Read an env var, treating empty/whitespace as unset."""
    return (os.environ.get(name) or "").strip() or None


def _role_env(prefix: str | None, suffix: str) -> str | None:
    """Read a role-specific env var (`{PREFIX}_{SUFFIX}`); None for the answerer
    (whose vars are the bare base names, read separately)."""
    if prefix is None:
        return None
    return _env(f"{prefix}_{suffix}")


def _validate_provider(raw: str) -> Provider:
    """Validate a provider string, failing closed on a typo (ADR-0006)."""
    value = raw.strip().lower()
    if value not in ("openai", "azure"):
        raise ValueError(f"provider must be one of openai|azure, got {raw!r}")
    return value  # type: ignore[return-value]


def _role_label(prefix: str | None) -> str:
    return "answerer" if prefix is None else prefix.lower()


def _var_label(prefix: str | None, suffix: str) -> str:
    return suffix if prefix is None else f"{prefix}_{suffix}"


class ProviderConfig(BaseModel):
    """Frozen connection config for one model role.

    Carries the provider and its connection params only — never a model name
    (model selection stays per call-site, ADR-0006). `base_url` is the seam
    for OpenAI-compatible endpoints; `azure_endpoint` / `api_version` /
    `azure_deployment` are the Azure-only params (ignored when
    `provider == "openai"`). `azure_deployment` is the per-resource deployment
    name (US-024), distinct from the per-call-site model id; when set it pins
    the Azure client to `/openai/deployments/{azure_deployment}/…`, when None
    the SDK uses the per-call `model` argument as the deployment.
    """

    model_config = ConfigDict(frozen=True)

    provider: Provider
    api_key: str
    base_url: str | None = None
    azure_endpoint: str | None = None
    api_version: str | None = None
    azure_deployment: str | None = None

    @classmethod
    def from_env(cls, role: Role) -> ProviderConfig:
        """Build a role's connection config from the process environment.

        Intended to be called once at module/startup load, never per request.
        The answerer resolves from the bare base vars; embedder/judge resolve
        from their `{PREFIX}_*` vars and fall back to the answerer config when
        unset, so a single-provider deployment sets nothing extra.
        """
        if role not in _ROLE_PREFIX:
            raise ValueError(f"unknown model role: {role!r}")
        base = None if role == "answerer" else cls.from_env("answerer")
        return cls._resolve(_ROLE_PREFIX[role], base)

    @classmethod
    def _resolve(cls, prefix: str | None, base: ProviderConfig | None) -> ProviderConfig:
        # Provider: role-specific var → (answerer only) LLM_PROVIDER → the
        # answerer config's provider → the default.
        provider_raw = _role_env(prefix, "PROVIDER")
        if provider_raw is None and prefix is None:
            provider_raw = _env("LLM_PROVIDER")
        if provider_raw is not None:
            provider: Provider = _validate_provider(provider_raw)
        elif base is not None:
            provider = base.provider
        else:
            provider = "openai"

        if provider == "azure":
            # US-024: fail-closed. All three Azure params must resolve (own var
            # → bare AZURE_OPENAI_* → inherited from an azure answerer). No
            # OPENAI_API_KEY fallback (don't bleed the OpenAI key into Azure)
            # and no api-version default — a missing var raises here, not at the
            # first request.
            azure_base = base if base is not None and base.provider == "azure" else None
            api_key = (
                _role_env(prefix, "API_KEY")
                or _env("AZURE_OPENAI_API_KEY")
                or (azure_base.api_key if azure_base else None)
            )
            if not api_key:
                raise ValueError(
                    f"{_role_label(prefix)} azure provider requires an API key "
                    f"(set {_var_label(prefix, 'API_KEY')} / AZURE_OPENAI_API_KEY)"
                )
            endpoint = (
                _role_env(prefix, "AZURE_OPENAI_ENDPOINT")
                or _env("AZURE_OPENAI_ENDPOINT")
                or (azure_base.azure_endpoint if azure_base else None)
            )
            if not endpoint:
                raise ValueError(
                    f"{_role_label(prefix)} azure provider requires "
                    f"{_var_label(prefix, 'AZURE_OPENAI_ENDPOINT')} (or AZURE_OPENAI_ENDPOINT)"
                )
            api_version = (
                _role_env(prefix, "AZURE_OPENAI_API_VERSION")
                or _env("AZURE_OPENAI_API_VERSION")
                or (azure_base.api_version if azure_base else None)
            )
            if not api_version:
                raise ValueError(
                    f"{_role_label(prefix)} azure provider requires "
                    f"{_var_label(prefix, 'AZURE_OPENAI_API_VERSION')} (or AZURE_OPENAI_API_VERSION)"
                )
            # Deployment is per-role and NOT inherited from the answerer (a chat
            # deployment is wrong for embeddings); unset → None → the SDK uses
            # the per-call model arg as the deployment.
            deployment = (
                _role_env(prefix, "AZURE_OPENAI_DEPLOYMENT")
                if prefix is not None
                else _env("AZURE_OPENAI_DEPLOYMENT")
            )
            return cls(
                provider="azure",
                api_key=api_key,
                azure_endpoint=endpoint,
                api_version=api_version,
                azure_deployment=deployment,
            )

        # openai (and any OpenAI-compatible endpoint via *_BASE_URL).
        role_api_key = _role_env(prefix, "API_KEY")
        role_base_url = _role_env(prefix, "BASE_URL")
        # Fail closed: a role that overrides its own base_url is pointing at a
        # DISTINCT host, so it must carry its own api key. Inheriting the
        # answerer's api_key (or bare OPENAI_API_KEY) here would silently forward
        # that credential to a third-party/self-hosted host the operator only
        # meant to redirect traffic to — a credential-exposure footgun. The key
        # is inherited only when the role talks to the same host (no base_url
        # override).
        if role_base_url is not None and role_api_key is None:
            raise ValueError(
                f"{_role_label(prefix)} openai provider sets "
                f"{_var_label(prefix, 'BASE_URL')} (a distinct host) but no "
                f"{_var_label(prefix, 'API_KEY')} — refusing to forward the answerer "
                f"OPENAI_API_KEY to a different host; set {_var_label(prefix, 'API_KEY')}."
            )
        api_key = (
            role_api_key
            or (base.api_key if base is not None and base.provider == "openai" else None)
            or _env("OPENAI_API_KEY")
        )
        if not api_key:
            raise ValueError(
                f"{_role_label(prefix)} openai provider requires an API key "
                f"(set {_var_label(prefix, 'API_KEY')} / OPENAI_API_KEY)"
            )
        base_url = role_base_url
        if base_url is None:
            base_url = (
                base.base_url if base is not None and base.provider == "openai" else None
            ) or _env("OPENAI_BASE_URL")
        return cls(provider="openai", api_key=api_key, base_url=base_url)


def build_openai_client(cfg: ProviderConfig) -> AsyncOpenAI:
    """Factory: a configured async client for `cfg`.

    Returns a plain `AsyncOpenAI` for `openai` (honoring an optional `base_url`
    for OpenAI-compatible endpoints) and an `AsyncAzureOpenAI` for `azure`.
    Because `AsyncAzureOpenAI` subclasses `AsyncOpenAI`, the Chat Completions
    surface the rest of the app programs against is identical (ADR-0006).

    Azure (US-024): auth is **api-key only** (`api_key=`) — Microsoft Entra ID
    (AAD) `azure_ad_token` auth is intentionally not wired in v1 (documented
    future seam, ADR-0006 / F3 capability matrix). When `cfg.azure_deployment`
    is set it pins the client to `/openai/deployments/{deployment}/`, so every
    request URL-templates to the deployment name (not the model id) with the
    `api-version` query param; when None the SDK uses the per-call `model`
    argument as the deployment.
    """
    if cfg.provider == "azure":
        if not cfg.azure_endpoint:
            raise ValueError("provider=azure requires azure_endpoint")
        # azure_deployment is omitted when None so the SDK falls back to the
        # per-call model arg as the deployment.
        kwargs: dict = {
            "api_key": cfg.api_key,
            "azure_endpoint": cfg.azure_endpoint,
            "api_version": cfg.api_version or DEFAULT_AZURE_API_VERSION,
        }
        if cfg.azure_deployment:
            kwargs["azure_deployment"] = cfg.azure_deployment
        return AsyncAzureOpenAI(**kwargs)
    return AsyncOpenAI(api_key=cfg.api_key, base_url=cfg.base_url)


def responses_capable(answerer: ProviderConfig) -> bool:
    """US-025: whether the answerer can serve the Responses API.

    Responses mode (hosted `file_search` + server-side `previous_response_id`
    threading) exists on **OpenAI proper only** — `provider=openai` with no
    `base_url` override. An OpenAI-*compatible* host reached via `base_url`
    (vLLM, Together, Groq, Ollama, …) faithfully speaks Chat Completions but has
    no Responses endpoint, so it is NOT responses-capable even though its
    provider is `openai`. Azure is likewise out (provider != openai). This is
    the single source of truth for both the default-mode resolution
    (`resolve_chat_mode_default`) and the per-request `mode` gate in main.py.
    """
    return answerer.provider == "openai" and answerer.base_url is None


def resolve_chat_mode_default(answerer: ProviderConfig, raw: str | None) -> ChatMode:
    """US-025: resolve + validate the process-wide default chat mode (FR-M4).

    `responses` (OpenAI Responses API — hosted `file_search` + server-side
    `previous_response_id` threading) runs on **OpenAI proper only** (`openai`
    provider with no `base_url` override) and is non-portable; `completions`
    (Chat Completions) is the cross-provider path every provider speaks.
    Resolved against the already-validated answerer config (provider AND
    base_url), so the portable path is the cross-provider default and a
    non-portable combination can never look "accepted" while silently stripping
    file_search / server-side threading:

      * `raw` empty/unset → `responses` for a responses-capable answerer
        (preserves the US-004 default), `completions` otherwise (Azure, or an
        openai answerer with a base_url override);
      * an explicit `responses` under a non-responses-capable answerer → **fail
        closed** with a `RuntimeError` naming the offending config and the remedy
        (never a silent downgrade to `completions`);
      * `completions` is always honored;
      * any other value → `ValueError` (typo, like the provider validation).

    Pure + import-light by design: it reads no environment itself (the caller
    passes `os.environ.get("CHAT_MODE_DEFAULT")`), so the startup guard is
    unit-testable without importing the FastAPI app. Intended to be called once
    at startup, never per request.
    """
    explicit = (raw or "").strip().lower() or None
    if explicit is not None and explicit not in ("responses", "completions"):
        raise ValueError(
            f"CHAT_MODE_DEFAULT must be 'responses' or 'completions', got {explicit!r}"
        )
    if not responses_capable(answerer):
        if explicit == "responses":
            reason = (
                f"provider={answerer.provider!r}"
                if answerer.provider != "openai"
                else f"an OpenAI-compatible host (OPENAI_BASE_URL={answerer.base_url!r})"
            )
            raise RuntimeError(
                "chat-mode 'responses' requires OpenAI proper (provider=openai with "
                f"no base_url override), but the resolved answerer is {reason}. "
                "Responses mode (hosted file_search + server-side "
                "previous_response_id threading) is OpenAI-only and non-portable — it "
                "cannot run on this host. Set CHAT_MODE_DEFAULT=completions, or use "
                "provider=openai with no base_url override."
            )
        return "completions"
    # responses-capable answerer (openai proper, no base_url): honor an explicit
    # choice, else keep the historical `responses` default (US-004).
    return explicit or "responses"  # type: ignore[return-value]
