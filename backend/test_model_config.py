"""US-021 validation test for the typed provider-config surface.

Exercises `model_config.ProviderConfig.from_env` + `build_openai_client`
directly. The module reads only the process environment (no DB, no network),
so this test sets its own env and needs no secrets — it runs anywhere.

Covers the PRD validation test (minimal `OPENAI_API_KEY`/`OPENAI_MODEL`
config → an openai answerer client with no base_url override) plus the Azure
path and the fail-closed guards.

Run:
    python -m backend.test_model_config
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from openai import AsyncAzureOpenAI, AsyncOpenAI  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from model_config import (  # noqa: E402
    DEFAULT_AZURE_API_VERSION,
    ProviderConfig,
    build_openai_client,
)

# Every env var the resolver consults — cleared before each case so a real
# .env on the developer's machine can't leak into the controlled scenario.
_MANAGED_KEYS = (
    "LLM_PROVIDER",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_API_VERSION",
    "AZURE_OPENAI_DEPLOYMENT",  # US-024 deployment/model split
    # US-022 role-specific vars (embedder + judge).
    "EMBEDDER_PROVIDER",
    "EMBEDDER_API_KEY",
    "EMBEDDER_BASE_URL",
    "EMBEDDER_AZURE_OPENAI_ENDPOINT",
    "EMBEDDER_AZURE_OPENAI_API_VERSION",
    "EMBEDDER_AZURE_OPENAI_DEPLOYMENT",
    "JUDGE_PROVIDER",
    "JUDGE_API_KEY",
    "JUDGE_BASE_URL",
    "JUDGE_AZURE_OPENAI_ENDPOINT",
    "JUDGE_AZURE_OPENAI_API_VERSION",
    "JUDGE_AZURE_OPENAI_DEPLOYMENT",
)


@contextmanager
def _env(**overrides: str) -> Iterator[None]:
    """Run a case with exactly `overrides` set for the managed keys; restore
    the prior environment afterward."""
    saved = {k: os.environ.get(k) for k in _MANAGED_KEYS}
    try:
        for k in _MANAGED_KEYS:
            os.environ.pop(k, None)
        for k, v in overrides.items():
            os.environ[k] = v
        yield
    finally:
        for k, prev in saved.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_minimal_openai_config() -> None:
    """PRD validation test: only OPENAI_API_KEY + OPENAI_MODEL set."""
    with _env(OPENAI_API_KEY="sk-test", OPENAI_MODEL="gpt-4o-mini"):
        cfg = ProviderConfig.from_env("answerer")
        _check(cfg.provider == "openai", f"provider should default to openai, got {cfg.provider!r}")
        _check(cfg.api_key == "sk-test", "api_key should come from OPENAI_API_KEY")
        _check(cfg.base_url is None, "base_url should be None with no OPENAI_BASE_URL")
        client = build_openai_client(cfg)
        _check(isinstance(client, AsyncOpenAI), "client should be AsyncOpenAI")
        _check(
            not isinstance(client, AsyncAzureOpenAI),
            "openai config must not build an Azure client",
        )
    print("ok: minimal openai config -> openai answerer client, no base_url override")


def test_no_new_env_vars_required() -> None:
    """A single-provider deployment sets nothing beyond OPENAI_API_KEY."""
    with _env(OPENAI_API_KEY="sk-test"):
        cfg = ProviderConfig.from_env("answerer")
        _check(cfg.provider == "openai", "should resolve with only OPENAI_API_KEY")
        build_openai_client(cfg)
    print("ok: no new env vars required for existing single-provider deployments")


def test_openai_base_url_is_honored() -> None:
    """An OpenAI-compatible endpoint flows through base_url."""
    with _env(OPENAI_API_KEY="sk-test", OPENAI_BASE_URL="https://compat.example/v1"):
        cfg = ProviderConfig.from_env("answerer")
        _check(cfg.base_url == "https://compat.example/v1", "base_url should be read from OPENAI_BASE_URL")
        client = build_openai_client(cfg)
        _check(str(client.base_url).startswith("https://compat.example/v1"), "client base_url should reflect cfg")
    print("ok: OPENAI_BASE_URL honored for OpenAI-compatible endpoints")


def test_all_roles_resolve_in_us021() -> None:
    """from_env accepts every role; in US-021 each resolves to the base config
    (US-022 adds role-specific precedence)."""
    with _env(OPENAI_API_KEY="sk-test"):
        for role in ("answerer", "embedder", "judge"):
            cfg = ProviderConfig.from_env(role)  # type: ignore[arg-type]
            _check(cfg.provider == "openai", f"role {role} should resolve")
            _check(cfg.api_key == "sk-test", f"role {role} should read base key")
    print("ok: answerer/embedder/judge roles all resolve from base config")


def test_config_is_frozen() -> None:
    """ProviderConfig is immutable once built (frozen model)."""
    cfg = ProviderConfig(provider="openai", api_key="sk-test")
    try:
        cfg.api_key = "mutated"  # type: ignore[misc]
    except ValidationError:
        print("ok: ProviderConfig is frozen (mutation rejected)")
        return
    raise AssertionError("ProviderConfig should be frozen but mutation succeeded")


def test_azure_config() -> None:
    """US-024: a fully-configured azure answerer builds an AsyncAzureOpenAI with
    the explicitly-supplied api-version (no silent default)."""
    with _env(
        LLM_PROVIDER="azure",
        AZURE_OPENAI_API_KEY="az-key",
        AZURE_OPENAI_ENDPOINT="https://contoso.openai.azure.com",
        AZURE_OPENAI_API_VERSION="2024-10-21",
    ):
        cfg = ProviderConfig.from_env("answerer")
        _check(cfg.provider == "azure", "provider should be azure")
        _check(cfg.azure_endpoint == "https://contoso.openai.azure.com", "azure_endpoint should be read")
        _check(cfg.api_version == "2024-10-21", "api_version should be the supplied value")
        _check(cfg.azure_deployment is None, "azure_deployment is None when unset")
        client = build_openai_client(cfg)
        _check(isinstance(client, AsyncAzureOpenAI), "azure config should build AsyncAzureOpenAI")
    print("ok: fully-configured azure -> AsyncAzureOpenAI with the supplied api-version")


def test_azure_requires_its_own_key() -> None:
    """US-024 (supersedes the US-021 leniency): OPENAI_API_KEY no longer
    satisfies azure — AZURE_OPENAI_API_KEY is required, fail-closed."""
    with _env(
        LLM_PROVIDER="azure",
        OPENAI_API_KEY="sk-test",  # present but must NOT satisfy azure
        AZURE_OPENAI_ENDPOINT="https://contoso.openai.azure.com",
        AZURE_OPENAI_API_VERSION="2024-10-21",
    ):
        _expect_value_error(
            lambda: ProviderConfig.from_env("answerer"),
            "azure without AZURE_OPENAI_API_KEY (no OPENAI_API_KEY fallback)",
        )


def test_azure_deployment_pinned() -> None:
    """US-024 validation test: AZURE_OPENAI_DEPLOYMENT (distinct from the model
    id) pins the client URL to /openai/deployments/{deployment}/ — the
    deployment name, not the model id, is in the path."""
    with _env(
        LLM_PROVIDER="azure",
        AZURE_OPENAI_API_KEY="az-key",
        AZURE_OPENAI_ENDPOINT="https://acme.openai.azure.com",
        AZURE_OPENAI_API_VERSION="2024-10-21",
        AZURE_OPENAI_DEPLOYMENT="acme-chat-deploy",  # != any model id
    ):
        cfg = ProviderConfig.from_env("answerer")
        _check(cfg.azure_deployment == "acme-chat-deploy", "deployment should be read from env")
        client = build_openai_client(cfg)
        url = str(client.base_url)
        _check(
            "/openai/deployments/acme-chat-deploy" in url,
            f"deployment name should be templated into the URL path, got {url!r}",
        )
        _check(cfg.api_version == "2024-10-21", "api_version should be the supplied value (-> ?api-version=)")
    print("ok: AZURE_OPENAI_DEPLOYMENT pins the deployment name into the URL path")


def test_azure_deployment_unset_uses_model_arg() -> None:
    """With no deployment configured, the client is not pinned — the SDK uses
    the per-call model arg as the deployment (base_url has no /deployments/)."""
    with _env(
        LLM_PROVIDER="azure",
        AZURE_OPENAI_API_KEY="az-key",
        AZURE_OPENAI_ENDPOINT="https://acme.openai.azure.com",
        AZURE_OPENAI_API_VERSION="2024-10-21",
    ):
        cfg = ProviderConfig.from_env("answerer")
        _check(cfg.azure_deployment is None, "deployment should be None when unset")
        client = build_openai_client(cfg)
        _check(
            "/deployments/" not in str(client.base_url),
            "unpinned client must not bake a deployment into base_url",
        )
    print("ok: no deployment -> client unpinned, per-call model arg drives the deployment")


def test_azure_deployment_not_inherited_by_embedder() -> None:
    """The answerer's chat deployment must NOT leak to the embedder (a chat
    deployment is wrong for embeddings); the embedder reads its own var."""
    with _env(
        LLM_PROVIDER="azure",
        AZURE_OPENAI_API_KEY="az-key",
        AZURE_OPENAI_ENDPOINT="https://acme.openai.azure.com",
        AZURE_OPENAI_API_VERSION="2024-10-21",
        AZURE_OPENAI_DEPLOYMENT="acme-chat-deploy",
        EMBEDDER_AZURE_OPENAI_DEPLOYMENT="acme-embed-deploy",
    ):
        answerer = ProviderConfig.from_env("answerer")
        embedder = ProviderConfig.from_env("embedder")
        _check(answerer.azure_deployment == "acme-chat-deploy", "answerer keeps its chat deployment")
        _check(embedder.azure_deployment == "acme-embed-deploy", "embedder uses its own deployment, not inherited")
        _check(embedder.azure_endpoint == answerer.azure_endpoint, "embedder still inherits endpoint/version/key")
    print("ok: azure deployment is per-role, not inherited from the answerer")


def _expect_value_error(fn, label: str) -> None:
    try:
        fn()
    except ValueError:
        print(f"ok: {label} fails closed (ValueError)")
        return
    raise AssertionError(f"{label} should have raised ValueError")


def test_fail_closed_guards() -> None:
    """Misconfiguration raises at build time, never silently defaults."""
    with _env():  # nothing set
        _expect_value_error(lambda: ProviderConfig.from_env("answerer"), "missing OPENAI_API_KEY")
    with _env(LLM_PROVIDER="azur", OPENAI_API_KEY="sk-test"):
        _expect_value_error(lambda: ProviderConfig.from_env("answerer"), "invalid LLM_PROVIDER")
    with _env(LLM_PROVIDER="azure", AZURE_OPENAI_API_KEY="az-key"):
        _expect_value_error(lambda: ProviderConfig.from_env("answerer"), "azure without AZURE_OPENAI_ENDPOINT")
    with _env(LLM_PROVIDER="azure", AZURE_OPENAI_ENDPOINT="https://x.openai.azure.com"):
        _expect_value_error(lambda: ProviderConfig.from_env("answerer"), "azure without any api key")
    # US-024: api-version is now required — no silent default.
    with _env(
        LLM_PROVIDER="azure",
        AZURE_OPENAI_API_KEY="az-key",
        AZURE_OPENAI_ENDPOINT="https://x.openai.azure.com",
    ):
        _expect_value_error(
            lambda: ProviderConfig.from_env("answerer"), "azure without AZURE_OPENAI_API_VERSION"
        )


# ---------------------------------------------------------------------------
# US-022: per-role provider binding
# ---------------------------------------------------------------------------


def test_embedder_inherits_answerer_by_default() -> None:
    """With no EMBEDDER_*/JUDGE_* vars, every role resolves to the answerer
    config — a single-provider deployment sets nothing extra."""
    with _env(OPENAI_API_KEY="sk-test", OPENAI_BASE_URL="https://compat.example/v1"):
        answerer = ProviderConfig.from_env("answerer")
        embedder = ProviderConfig.from_env("embedder")
        judge = ProviderConfig.from_env("judge")
        _check(embedder == answerer, "embedder should inherit the answerer config")
        _check(judge == answerer, "judge should inherit the answerer config")
        _check(embedder.base_url == "https://compat.example/v1", "embedder should inherit base_url")
    print("ok: embedder + judge inherit the answerer config by default")


def test_embedder_diverges_to_azure() -> None:
    """The PRD validation test: answerer=openai, EMBEDDER_PROVIDER=azure with
    Azure embedder vars → split providers resolve independently."""
    with _env(
        OPENAI_API_KEY="sk-test",
        EMBEDDER_PROVIDER="azure",
        EMBEDDER_API_KEY="az-embed-key",
        EMBEDDER_AZURE_OPENAI_ENDPOINT="https://embed.openai.azure.com",
        EMBEDDER_AZURE_OPENAI_API_VERSION="2024-10-21",  # US-024: required for azure
    ):
        answerer = ProviderConfig.from_env("answerer")
        embedder = ProviderConfig.from_env("embedder")
        _check(answerer.provider == "openai", "answerer should stay openai")
        _check(embedder.provider == "azure", "embedder should be azure")
        _check(embedder.api_key == "az-embed-key", "embedder should use its own key")
        _check(
            embedder.azure_endpoint == "https://embed.openai.azure.com",
            "embedder should use its own Azure endpoint",
        )
        answerer_client = build_openai_client(answerer)
        embedder_client = build_openai_client(embedder)
        _check(
            isinstance(answerer_client, AsyncOpenAI)
            and not isinstance(answerer_client, AsyncAzureOpenAI),
            "answerer client should be plain OpenAI",
        )
        _check(isinstance(embedder_client, AsyncAzureOpenAI), "embedder client should be Azure")
    print("ok: answerer=openai + embedder=azure resolve to independent clients")


def test_embedder_field_level_overrides() -> None:
    """Role-specific vars override the answerer per field; unset fields inherit."""
    with _env(
        OPENAI_API_KEY="sk-answerer",
        EMBEDDER_API_KEY="sk-embedder",
    ):
        answerer = ProviderConfig.from_env("answerer")
        embedder = ProviderConfig.from_env("embedder")
        _check(embedder.provider == "openai", "embedder should inherit openai provider")
        _check(answerer.api_key == "sk-answerer", "answerer keeps its key")
        _check(embedder.api_key == "sk-embedder", "embedder overrides only the key")
    print("ok: role-specific vars override per field, inherit the rest")


def test_judge_can_diverge() -> None:
    """The judge role binds independently (cheaper-model capability is per
    call-site; here we prove the provider binding)."""
    with _env(
        OPENAI_API_KEY="sk-test",
        JUDGE_PROVIDER="azure",
        JUDGE_API_KEY="az-judge-key",
        JUDGE_AZURE_OPENAI_ENDPOINT="https://judge.openai.azure.com",
        JUDGE_AZURE_OPENAI_API_VERSION="2024-10-21",  # US-024: required for azure
    ):
        judge = ProviderConfig.from_env("judge")
        embedder = ProviderConfig.from_env("embedder")
        _check(judge.provider == "azure", "judge should be azure")
        _check(embedder.provider == "openai", "embedder should still inherit openai")
    print("ok: judge role binds independently of answerer + embedder")


def test_role_provider_typo_fails_closed() -> None:
    """An invalid role-specific provider raises (not silently ignored)."""
    with _env(OPENAI_API_KEY="sk-test", EMBEDDER_PROVIDER="azur"):
        _expect_value_error(
            lambda: ProviderConfig.from_env("embedder"), "invalid EMBEDDER_PROVIDER"
        )


def main() -> int:
    tests = [
        test_minimal_openai_config,
        test_no_new_env_vars_required,
        test_openai_base_url_is_honored,
        test_all_roles_resolve_in_us021,
        test_config_is_frozen,
        test_azure_config,
        test_azure_requires_its_own_key,
        test_azure_deployment_pinned,
        test_azure_deployment_unset_uses_model_arg,
        test_azure_deployment_not_inherited_by_embedder,
        test_fail_closed_guards,
        test_embedder_inherits_answerer_by_default,
        test_embedder_diverges_to_azure,
        test_embedder_field_level_overrides,
        test_judge_can_diverge,
        test_role_provider_typo_fails_closed,
    ]
    for t in tests:
        t()
    print(f"\nPASS: {len(tests)} model_config test groups")
    return 0


if __name__ == "__main__":
    sys.exit(main())
