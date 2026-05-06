"""US-024: `web_search` fallback tool.

When `search_documents` returns nothing relevant for the user's question, the
agent calls this tool instead of hallucinating from the model's parametric
memory. Provider is selected by `WEB_SEARCH_PROVIDER` env (`tavily`, `brave`,
`serpapi`, or `none` — default). All providers return a uniform
`WebSearchResult` shape (`title`, `url`, `snippet`) so the agent's behaviour
doesn't change when ops swaps backends.

Like the reranker (US-022), the tool is opt-in: with `WEB_SEARCH_PROVIDER=none`
or no API key, `is_enabled()` returns False and the tool is omitted from the
chat tools list — keeps existing deploys working without forcing a vendor key.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger("agentic_rag.backend.web_search")

WebSearchProviderName = Literal["tavily", "brave", "serpapi", "none"]

DEFAULT_TOP_K = 5
MAX_TOP_K = 20
DEFAULT_TIMEOUT_S = 10.0


class WebSearchInput(BaseModel):
    query: str = Field(
        ...,
        min_length=1,
        description=(
            "Web search query. Use this only after `search_documents` returns "
            "no relevant chunks for questions whose answer can't be in the "
            "user's local documents (current events, recent news, public "
            "facts outside the corpus)."
        ),
    )
    top_k: int = Field(
        default=DEFAULT_TOP_K,
        ge=1,
        le=MAX_TOP_K,
        description="Max number of web results to return (1..20).",
    )


class WebSearchResult(BaseModel):
    title: str
    url: str
    snippet: str


def get_web_search_provider_name() -> WebSearchProviderName:
    """`WEB_SEARCH_PROVIDER` env: `none` (default) | `tavily` | `brave` | `serpapi`."""
    raw = (os.environ.get("WEB_SEARCH_PROVIDER") or "none").strip().lower()
    if raw not in ("tavily", "brave", "serpapi", "none"):
        raise ValueError(
            f"WEB_SEARCH_PROVIDER must be one of tavily|brave|serpapi|none, got {raw!r}"
        )
    return raw  # type: ignore[return-value]


def get_web_search_timeout_s() -> float:
    """`WEB_SEARCH_TIMEOUT_S` env, default 10s. A web search that hangs longer
    than this is almost always a vendor outage — fail open so the agent can
    fall back to general knowledge rather than block the user's whole turn."""
    raw = os.environ.get("WEB_SEARCH_TIMEOUT_S")
    if raw is None or raw == "":
        return DEFAULT_TIMEOUT_S
    try:
        v = float(raw)
    except ValueError as e:
        raise ValueError(f"WEB_SEARCH_TIMEOUT_S must be a float, got {raw!r}") from e
    if v <= 0:
        raise ValueError(f"WEB_SEARCH_TIMEOUT_S must be > 0, got {v}")
    return v


class WebSearchProvider(ABC):
    """Provider-neutral web search.

    Implementations MUST tolerate empty `query` (return `[]`) and MUST surface
    HTTP errors via `httpx.HTTPStatusError` so the caller can decide whether
    to retry or fall back. The returned list is never longer than `top_k`.
    """

    name: str

    @abstractmethod
    async def search(self, query: str, top_k: int) -> list[WebSearchResult]:
        ...


class NullProvider(WebSearchProvider):
    """Returns no results. Used when WEB_SEARCH_PROVIDER=none so callers can
    invoke a provider uniformly without a `if provider == 'none'` branch."""

    name = "none"

    async def search(self, query: str, top_k: int) -> list[WebSearchResult]:
        return []


class TavilyProvider(WebSearchProvider):
    """Tavily Search API. Bearer-auth, JSON POST, returns ranked results
    keyed by `content`. Tavily's search is LLM-tuned (snippets favour direct
    answers) which usually plays well with chat-style follow-ups."""

    name = "tavily"
    ENDPOINT = "https://api.tavily.com/search"

    def __init__(self, http: httpx.AsyncClient, api_key: str) -> None:
        self.http = http
        self.api_key = api_key

    async def search(self, query: str, top_k: int) -> list[WebSearchResult]:
        if not query.strip():
            return []
        r = await self.http.post(
            self.ENDPOINT,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "query": query,
                "max_results": min(top_k, MAX_TOP_K),
                "search_depth": "basic",
            },
        )
        r.raise_for_status()
        body = r.json()
        results: list[WebSearchResult] = []
        for item in body.get("results", []):
            url = item.get("url")
            title = item.get("title") or url or ""
            content = item.get("content") or ""
            if not url:
                continue
            results.append(WebSearchResult(title=title, url=url, snippet=content))
        return results[:top_k]


class BraveProvider(WebSearchProvider):
    """Brave Web Search API. GET-style with subscription header."""

    name = "brave"
    ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, http: httpx.AsyncClient, api_key: str) -> None:
        self.http = http
        self.api_key = api_key

    async def search(self, query: str, top_k: int) -> list[WebSearchResult]:
        if not query.strip():
            return []
        r = await self.http.get(
            self.ENDPOINT,
            headers={
                "X-Subscription-Token": self.api_key,
                "Accept": "application/json",
            },
            params={"q": query, "count": min(top_k, MAX_TOP_K)},
        )
        r.raise_for_status()
        body = r.json()
        items = body.get("web", {}).get("results", []) or []
        results: list[WebSearchResult] = []
        for item in items:
            url = item.get("url")
            title = item.get("title") or url or ""
            description = item.get("description") or ""
            if not url:
                continue
            results.append(WebSearchResult(title=title, url=url, snippet=description))
        return results[:top_k]


class SerpApiProvider(WebSearchProvider):
    """SerpAPI Google engine. Useful for parity with how Google ranks; pricier
    per-query than Tavily/Brave so leave it as opt-in."""

    name = "serpapi"
    ENDPOINT = "https://serpapi.com/search"

    def __init__(self, http: httpx.AsyncClient, api_key: str) -> None:
        self.http = http
        self.api_key = api_key

    async def search(self, query: str, top_k: int) -> list[WebSearchResult]:
        if not query.strip():
            return []
        r = await self.http.get(
            self.ENDPOINT,
            params={
                "q": query,
                "engine": "google",
                "api_key": self.api_key,
                "num": min(top_k, MAX_TOP_K),
            },
        )
        r.raise_for_status()
        body = r.json()
        items = body.get("organic_results") or []
        results: list[WebSearchResult] = []
        for item in items:
            url = item.get("link")
            title = item.get("title") or url or ""
            snippet = item.get("snippet") or ""
            if not url:
                continue
            results.append(WebSearchResult(title=title, url=url, snippet=snippet))
        return results[:top_k]


def build_web_search_provider(
    name: WebSearchProviderName,
    *,
    http: httpx.AsyncClient,
) -> WebSearchProvider:
    """Factory matching `WEB_SEARCH_PROVIDER` env to a concrete provider.

    Hosted backends raise on missing API keys at build time so configuration
    mistakes surface immediately rather than at first request.
    """
    if name == "none":
        return NullProvider()
    if name == "tavily":
        api_key = os.environ.get("TAVILY_API_KEY")
        if not api_key:
            raise ValueError("WEB_SEARCH_PROVIDER=tavily requires TAVILY_API_KEY")
        return TavilyProvider(http=http, api_key=api_key)
    if name == "brave":
        api_key = os.environ.get("BRAVE_SEARCH_API_KEY")
        if not api_key:
            raise ValueError(
                "WEB_SEARCH_PROVIDER=brave requires BRAVE_SEARCH_API_KEY"
            )
        return BraveProvider(http=http, api_key=api_key)
    if name == "serpapi":
        api_key = os.environ.get("SERPAPI_API_KEY")
        if not api_key:
            raise ValueError("WEB_SEARCH_PROVIDER=serpapi requires SERPAPI_API_KEY")
        return SerpApiProvider(http=http, api_key=api_key)
    raise ValueError(f"unhandled web search provider: {name}")  # pragma: no cover


def is_enabled() -> bool:
    """True when a real provider is configured. Used by main.py to decide
    whether to expose `web_search` to the agent. We probe just the env var
    here (not the API key) to avoid raising during the cheap `tools[]` build;
    `build_web_search_provider` is the source of truth on missing keys."""
    return get_web_search_provider_name() != "none"


def web_search_tool_schema() -> dict[str, Any]:
    """Chat Completions `tools[]` entry for the web_search tool.

    The description doubles as the agent's routing instruction: prefer local
    retrieval, only fall back to web search when local search returns nothing
    relevant. The system-prompt block in main.py reinforces the same rule —
    we say it twice on purpose because models occasionally skim system text.
    """
    return {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the public web for information not in the user's "
                "ingested documents. Returns top-N results with title, URL, "
                "and a short snippet. Use this ONLY after `search_documents` "
                "returns no relevant chunks, or when the question is "
                "obviously about current events / public facts that aren't "
                "in the user's local corpus. When you cite a web result in "
                "your reply, include the URL so the user can click through."
            ),
            "parameters": WebSearchInput.model_json_schema(),
        },
    }


async def web_search(
    *,
    http: httpx.AsyncClient,
    query: str,
    top_k: int = DEFAULT_TOP_K,
    provider: WebSearchProvider | None = None,
) -> list[WebSearchResult]:
    """Run a web search via the configured provider.

    `provider` is an optional injection seam for tests; production callers
    pass `http` and let this function build the provider from env. Errors
    propagate so the caller can serialise them into the tool result for the
    agent to react to (typically: log, fall back to general knowledge).
    """
    if provider is None:
        provider = build_web_search_provider(
            get_web_search_provider_name(), http=http
        )
    log.info(
        "web_search.execute provider=%s top_k=%d query=%r",
        provider.name,
        top_k,
        query[:200],
    )
    return await provider.search(query, top_k)
