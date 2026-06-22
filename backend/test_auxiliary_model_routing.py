"""US-023 validation test: auxiliary helpers stay answerer-role.

The five auxiliary text-gen helpers — metadata, planner, SQL-gen, subagent,
and the `llm` reranker — must (1) keep their per-call-site *model* selectors,
and (2) run on the shared **answerer** client, never constructing their own
client or reading a provider/`base_url` env. Model selection is per call-site;
provider/`base_url` is one chat host per deployment (ADR-0006).

This test reads only the process environment and the helper source files — no
DB, no network, no secrets — so it runs anywhere.

PRD validation test (US-023): set the answerer to `azure` and
`OPENAI_PLANNER_MODEL=gpt-4o-mini`; confirm the helpers select the override
*model* while the provider stays the answerer's Azure binding.

Run:
    python -m backend.test_auxiliary_model_routing
"""

from __future__ import annotations

import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

import httpx  # noqa: E402
from openai import AsyncAzureOpenAI  # noqa: E402

from metadata import get_metadata_model  # noqa: E402
from model_config import ProviderConfig, build_openai_client  # noqa: E402
from planner import get_planner_model  # noqa: E402
from reranking import LlmReranker, build_reranker  # noqa: E402
from subagent import get_subagent_model  # noqa: E402
from text_to_sql import get_sql_model  # noqa: E402

# The five model selectors and their per-call override env vars (AC1). Each
# falls through to OPENAI_MODEL, then a hard default — provider-independent.
_SELECTORS = [
    ("metadata", get_metadata_model, "METADATA_MODEL"),
    ("planner", get_planner_model, "OPENAI_PLANNER_MODEL"),
    ("sql", get_sql_model, "OPENAI_SQL_MODEL"),
    ("subagent", get_subagent_model, "OPENAI_SUBAGENT_MODEL"),
    # the llm reranker resolves its model inside build_reranker; covered by its
    # own routing test below, but its override var is exercised here too.
]

# Every model/provider env var any selector or resolver consults — cleared
# before each case so a real .env can't leak into the controlled scenario.
_MANAGED_KEYS = (
    "OPENAI_MODEL",
    "METADATA_MODEL",
    "OPENAI_PLANNER_MODEL",
    "OPENAI_SQL_MODEL",
    "OPENAI_SUBAGENT_MODEL",
    "OPENAI_RERANK_MODEL",
    "LLM_PROVIDER",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_API_VERSION",
)

# Helper modules that must stay answerer-role (AC2/AC3).
_HELPER_FILES = ("metadata.py", "planner.py", "text_to_sql.py", "subagent.py", "reranking.py")


@contextmanager
def _env(**overrides: str) -> Iterator[None]:
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


def test_selectors_honor_per_call_override() -> None:
    """AC1: each selector returns its own override when set."""
    for name, fn, var in _SELECTORS:
        with _env(**{var: "model-override", "OPENAI_MODEL": "base-model"}):
            _check(fn() == "model-override", f"{name}: {var} override should win")
    print("ok: each selector honors its per-call model override")


def test_selectors_fall_through_to_openai_model() -> None:
    """AC1: with no per-call override, every selector falls back to OPENAI_MODEL."""
    for name, fn, _var in _SELECTORS:
        with _env(OPENAI_MODEL="base-model"):
            _check(fn() == "base-model", f"{name}: should fall through to OPENAI_MODEL")
    print("ok: selectors fall through to OPENAI_MODEL when no override is set")


def test_selectors_have_a_hard_default() -> None:
    """AC1: with neither var set, selectors default rather than raise."""
    for name, fn, _var in _SELECTORS:
        with _env():
            _check(bool(fn()), f"{name}: should return a non-empty default model")
    print("ok: selectors default to a model when nothing is configured")


def test_helpers_never_construct_a_client_or_read_provider_env() -> None:
    """AC2/AC3: no helper opens its own client, reads an OpenAI provider/
    connection env, or sets a per-call base_url. (Cohere/Voyage keys in
    reranking.py are a SEPARATE reranker provider axis and are allowed.)"""
    construct = re.compile(r"Async(?:Azure)?OpenAI\s*\(")
    provider_env = re.compile(
        r"os\.environ(?:\.get)?\s*[\(\[]\s*['\"]"
        r"(OPENAI_API_KEY|LLM_PROVIDER|OPENAI_BASE_URL|AZURE_OPENAI_[A-Z_]+"
        r"|EMBEDDER_[A-Z_]+|JUDGE_[A-Z_]+)"
    )
    base_url_arg = re.compile(r"\bbase_url\s*=")  # \b so database_url= can't match
    for fname in _HELPER_FILES:
        src = (BACKEND / fname).read_text()
        _check(not construct.search(src), f"{fname}: must not construct an OpenAI client")
        _check(not provider_env.search(src), f"{fname}: must not read an OpenAI provider/connection env")
        _check(not base_url_arg.search(src), f"{fname}: must not set a per-call base_url")
    print("ok: no helper constructs a client / reads a provider env / sets base_url")


def test_llm_reranker_routes_the_answerer_client() -> None:
    """AC2 (positive): under answerer=azure the llm reranker holds exactly the
    answerer's Azure client and selects its own model override."""
    with _env(
        LLM_PROVIDER="azure",
        AZURE_OPENAI_API_KEY="az-key",
        AZURE_OPENAI_ENDPOINT="https://contoso.openai.azure.com",
        AZURE_OPENAI_API_VERSION="2024-10-21",  # US-024: required for azure
        OPENAI_RERANK_MODEL="gpt-4o-mini",
    ):
        answerer_client = build_openai_client(ProviderConfig.from_env("answerer"))
        _check(isinstance(answerer_client, AsyncAzureOpenAI), "answerer client should be Azure")
        # The llm branch of build_reranker never touches http (no request is
        # issued), so a fresh, unused AsyncClient is enough to satisfy the
        # signature without an event loop.
        reranker = build_reranker(
            "llm", http=httpx.AsyncClient(), openai_client=answerer_client
        )
        # bare assert so mypy narrows Reranker -> LlmReranker for the attr reads
        assert isinstance(reranker, LlmReranker), "llm reranker should be built"
        _check(
            reranker.openai_client is answerer_client,
            "llm reranker must hold the *answerer* client, not its own",
        )
        _check(reranker.model == "gpt-4o-mini", "OPENAI_RERANK_MODEL override should apply")
    print("ok: llm reranker runs on the answerer client; model override applies")


def test_prd_validation_scenario() -> None:
    """PRD US-023 validation: answerer=azure + OPENAI_PLANNER_MODEL=gpt-4o-mini
    → planner selects the override model while the provider stays Azure (model
    override does not split the provider)."""
    with _env(
        LLM_PROVIDER="azure",
        AZURE_OPENAI_API_KEY="az-key",
        AZURE_OPENAI_ENDPOINT="https://contoso.openai.azure.com",
        AZURE_OPENAI_API_VERSION="2024-10-21",  # US-024: required for azure
        OPENAI_PLANNER_MODEL="gpt-4o-mini",
    ):
        client = build_openai_client(ProviderConfig.from_env("answerer"))
        # Azure client subclasses AsyncOpenAI → identical Chat Completions
        # surface; the per-call model override applies within that one provider.
        _check(isinstance(client, AsyncAzureOpenAI), "answerer provider should be Azure")
        _check(get_planner_model() == "gpt-4o-mini", "planner model override should apply within Azure")
    print("ok: PRD scenario — azure provider + per-call planner model override")


def main() -> int:
    tests = [
        test_selectors_honor_per_call_override,
        test_selectors_fall_through_to_openai_model,
        test_selectors_have_a_hard_default,
        test_helpers_never_construct_a_client_or_read_provider_env,
        test_llm_reranker_routes_the_answerer_client,
        test_prd_validation_scenario,
    ]
    for t in tests:
        t()
    print(f"\nPASS: {len(tests)} auxiliary-model-routing test groups")
    return 0


if __name__ == "__main__":
    sys.exit(main())
