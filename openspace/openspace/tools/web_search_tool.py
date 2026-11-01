"""WebSearchTool.

OpenSpace exposes a read-only web search tool with provider adapters for
server-side search and local fallback search providers.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qs, unquote, urlparse

import aiohttp

from openspace.grounding.core.permissions.types import (
    AddRulesUpdate,
    PermissionPassthrough,
    PermissionRuleValue,
)
from openspace.grounding.core.tool.base import BaseTool
from openspace.grounding.core.types import BackendType, ToolResult, ToolStatus
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

WEB_SEARCH_TOOL_NAME = "web_search"
WEB_SEARCH_TOOL_ALIAS = "WebSearch"
ANTHROPIC_WEB_SEARCH_TOOL_TYPE = "web_search_20250305"
MAX_SEARCH_USES = 8
MAX_RESULT_SIZE_CHARS = 100_000
SEARCH_TIMEOUT_SECONDS = 30
DEFAULT_MAX_RESULTS = 8


def get_web_search_prompt(now: datetime | None = None) -> str:
    current_month_year = (now or datetime.now()).strftime("%B %Y")
    return f"""
- Allows OpenSpace to search the web and use the results to inform responses
- Provides up-to-date information for current events and recent data
- Returns search result information formatted as search result blocks, including links as markdown hyperlinks
- Use this tool for accessing information beyond Claude's knowledge cutoff
- Searches are performed automatically within a single API call

CRITICAL REQUIREMENT - You MUST follow this:
  - After answering the user's question, you MUST include a "Sources:" section at the end of your response
  - In the Sources section, list all relevant URLs from the search results as markdown hyperlinks: [Title](URL)
  - This is MANDATORY - never skip including sources in your response
  - Example format:

    [Your answer here]

    Sources:
    - [Source Title 1](https://example.com/1)
    - [Source Title 2](https://example.com/2)

Usage notes:
  - Domain filtering is supported to include or block specific websites
  - Web search is only available in the US

IMPORTANT - Use the correct year in search queries:
  - The current month is {current_month_year}. You MUST use this year when searching for recent information, documentation, or current events.
  - Example: If the user asks for "latest React docs", search for "React documentation" with the current year, NOT last year
"""


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str | None = None

    def to_dict(self, *, include_snippet: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {"title": self.title, "url": self.url}
        if include_snippet and self.snippet:
            data["snippet"] = self.snippet
        return data


@dataclass(frozen=True)
class SearchResult:
    tool_use_id: str
    content: tuple[SearchHit, ...]

    def to_dict(self, *, include_snippet: bool = True) -> dict[str, Any]:
        return {
            "tool_use_id": self.tool_use_id,
            "content": [hit.to_dict(include_snippet=include_snippet) for hit in self.content],
        }


@dataclass(frozen=True)
class WebSearchOutput:
    query: str
    results: tuple[SearchResult | str, ...]
    durationSeconds: float
    provider: str | None = None

    @property
    def duration_seconds(self) -> float:
        return self.durationSeconds

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "results": [
                item if isinstance(item, str) else item.to_dict()
                for item in self.results
            ],
            "durationSeconds": self.durationSeconds,
            "duration_seconds": self.durationSeconds,
            "provider": self.provider,
        }


@dataclass(frozen=True)
class ProviderSearchResponse:
    results: tuple[SearchResult | str, ...]
    provider: str


class WebSearchProviderError(RuntimeError):
    pass


class WebSearchProvider:
    name = "base"

    async def search(
        self,
        *,
        query: str,
        allowed_domains: Sequence[str] | None = None,
        blocked_domains: Sequence[str] | None = None,
        context: Any = None,
        abort_event: asyncio.Event | None = None,
    ) -> ProviderSearchResponse:
        raise NotImplementedError


def make_tool_schema(
    input_: Mapping[str, Any],
    *,
    max_uses: int = MAX_SEARCH_USES,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": ANTHROPIC_WEB_SEARCH_TOOL_TYPE,
        "name": "web_search",
        "max_uses": max_uses,
    }
    allowed_domains = input_.get("allowed_domains")
    blocked_domains = input_.get("blocked_domains")
    if allowed_domains:
        schema["allowed_domains"] = allowed_domains
    if blocked_domains:
        schema["blocked_domains"] = blocked_domains
    return schema


def _attr_or_key(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _iter_blocks(result: Any) -> Iterable[Any]:
    if result is None:
        return ()
    if isinstance(result, Mapping):
        content = result.get("content")
        if isinstance(content, list):
            return content
        return ()
    content = getattr(result, "content", None)
    if isinstance(content, list):
        return content
    return ()


def _hit_from_any(raw: Any) -> SearchHit:
    title = str(_attr_or_key(raw, "title", "") or "")
    url = str(_attr_or_key(raw, "url", "") or "")
    snippet = _attr_or_key(raw, "snippet", None)
    if snippet is None:
        snippet = _attr_or_key(raw, "content", None)
    if snippet is None:
        snippet = _attr_or_key(raw, "description", None)
    snippet_text = str(snippet).strip() if snippet else None
    return SearchHit(title=title, url=url, snippet=snippet_text)


def make_output_from_search_response(
    result: Any,
    query: str,
    duration_seconds: float,
    *,
    provider: str | None = None,
) -> WebSearchOutput:
    # OpenSpace expects: optional leading text, then repeated
    # server_tool_use → web_search_tool_result → text/citations.
    results: list[SearchResult | str] = []
    text_acc = ""
    in_text = True

    for block in _iter_blocks(result):
        block_type = _attr_or_key(block, "type")
        if block_type == "server_tool_use":
            if in_text:
                in_text = False
                if text_acc.strip():
                    results.append(text_acc.strip())
                text_acc = ""
            continue

        if block_type == "web_search_tool_result":
            content = _attr_or_key(block, "content")
            if not isinstance(content, list):
                error_code = _attr_or_key(content, "error_code", None)
                if error_code is None and isinstance(content, Mapping):
                    error_code = content.get("error_code")
                message = f"Web search error: {error_code or 'unknown'}"
                logger.error(message)
                results.append(message)
                continue
            hits = tuple(_hit_from_any(item) for item in content)
            results.append(
                SearchResult(
                    tool_use_id=str(_attr_or_key(block, "tool_use_id", "") or ""),
                    content=hits,
                )
            )
            continue

        if block_type == "text":
            text = str(_attr_or_key(block, "text", "") or "")
            if in_text:
                text_acc += text
            else:
                in_text = True
                text_acc = text

    if text_acc:
        results.append(text_acc.strip())

    return WebSearchOutput(
        query=query,
        results=tuple(results),
        durationSeconds=duration_seconds,
        provider=provider,
    )


def format_search_output_for_model(output: WebSearchOutput) -> str:
    formatted = f'Web search results for query: "{output.query}"\n\n'
    for item in output.results:
        if item is None:
            continue
        if isinstance(item, str):
            if item.strip():
                formatted += item.strip() + "\n\n"
            continue
        if item.content:
            formatted += (
                "Links: "
                + json.dumps(
                    [hit.to_dict() for hit in item.content],
                    ensure_ascii=False,
                )
                + "\n\n"
            )
        else:
            formatted += "No links found.\n\n"
    formatted += (
        "\nREMINDER: You MUST include the sources above in your response to the "
        "user using markdown hyperlinks."
    )
    return formatted.strip()


def validate_domains(
    allowed_domains: Sequence[str] | None,
    blocked_domains: Sequence[str] | None,
) -> str | None:
    if allowed_domains and blocked_domains:
        return "Error: Cannot specify both allowed_domains and blocked_domains in the same request"
    for label, domains in (("allowed_domains", allowed_domains), ("blocked_domains", blocked_domains)):
        for domain in domains or ():
            if not isinstance(domain, str) or not domain.strip():
                return f"Error: {label} entries must be non-empty domain strings"
            if "://" in domain or "/" in domain:
                return f"Error: {label} entries must be domains, not URLs"
    return None


def _normalize_domain(domain: str) -> str:
    domain = domain.strip().lower()
    if domain.startswith("*."):
        domain = domain[2:]
    return domain[4:] if domain.startswith("www.") else domain


def _host_matches_domain(host: str, domain: str) -> bool:
    host = _normalize_domain(host)
    domain = _normalize_domain(domain)
    return host == domain or host.endswith("." + domain)


def domain_allowed(
    url: str,
    allowed_domains: Sequence[str] | None = None,
    blocked_domains: Sequence[str] | None = None,
) -> bool:
    hostname = urlparse(url).hostname
    if not hostname:
        return False
    if allowed_domains:
        return any(_host_matches_domain(hostname, domain) for domain in allowed_domains)
    if blocked_domains:
        return not any(_host_matches_domain(hostname, domain) for domain in blocked_domains)
    return True


def filter_hits_by_domain(
    hits: Iterable[SearchHit],
    allowed_domains: Sequence[str] | None = None,
    blocked_domains: Sequence[str] | None = None,
) -> tuple[SearchHit, ...]:
    return tuple(
        hit
        for hit in hits
        if hit.url and domain_allowed(hit.url, allowed_domains, blocked_domains)
    )


def build_permission_suggestions() -> tuple[AddRulesUpdate, ...]:
    return (
        AddRulesUpdate(
            destination="localSettings",
            rules=(PermissionRuleValue(tool_name=WEB_SEARCH_TOOL_NAME),),
            behavior="allow",
        ),
    )


class AnthropicServerWebSearchProvider(WebSearchProvider):
    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        max_uses: int = MAX_SEARCH_USES,
    ) -> None:
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self.model = model or os.getenv(
            "OPENSPACE_WEB_SEARCH_ANTHROPIC_MODEL",
            os.getenv("OPENSPACE_WEB_SEARCH_MODEL", "claude-sonnet-4-5-20250929"),
        )
        self.base_url = base_url or os.getenv("OPENSPACE_WEB_SEARCH_BASE_URL")
        self.max_uses = max_uses

    async def search(
        self,
        *,
        query: str,
        allowed_domains: Sequence[str] | None = None,
        blocked_domains: Sequence[str] | None = None,
        context: Any = None,
        abort_event: asyncio.Event | None = None,
    ) -> ProviderSearchResponse:
        if not self.api_key:
            raise WebSearchProviderError("ANTHROPIC_API_KEY is not set")
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:  # pragma: no cover - dependency exists in pyproject
            raise WebSearchProviderError("anthropic package is not installed") from exc

        client_kwargs: dict[str, Any] = {"api_key": self.api_key}
        if self.base_url:
            client_kwargs["base_url"] = self.base_url
        client = AsyncAnthropic(**client_kwargs)
        kwargs = {
            "model": self.model,
            "max_tokens": int(os.getenv("OPENSPACE_WEB_SEARCH_MAX_TOKENS", "4096")),
            "system": "You are an assistant for performing a web search tool use",
            "messages": [
                {
                    "role": "user",
                    "content": "Perform a web search for the query: " + query,
                }
            ],
            "tools": [
                make_tool_schema(
                    {
                        "allowed_domains": list(allowed_domains) if allowed_domains else None,
                        "blocked_domains": list(blocked_domains) if blocked_domains else None,
                    },
                    max_uses=self.max_uses,
                )
            ],
        }
        betas = os.getenv("OPENSPACE_WEB_SEARCH_ANTHROPIC_BETAS")
        if betas:
            kwargs["betas"] = [item.strip() for item in betas.split(",") if item.strip()]

        task = asyncio.create_task(client.beta.messages.create(**kwargs))
        if abort_event is not None:
            abort_task = asyncio.create_task(abort_event.wait())
            done, pending = await asyncio.wait(
                {task, abort_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for pending_task in pending:
                pending_task.cancel()
            if abort_task in done:
                task.cancel()
                raise asyncio.CancelledError("WebSearch aborted")
        response = await task
        output = make_output_from_search_response(response, query, 0.0, provider=self.name)
        return ProviderSearchResponse(results=output.results, provider=self.name)


class TavilySearchProvider(WebSearchProvider):
    name = "tavily"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        endpoint: str | None = None,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> None:
        self.api_key = api_key or os.getenv("TAVILY_API_KEY")
        self.endpoint = endpoint or os.getenv(
            "OPENSPACE_TAVILY_SEARCH_URL", "https://api.tavily.com/search"
        )
        self.max_results = max_results

    async def search(
        self,
        *,
        query: str,
        allowed_domains: Sequence[str] | None = None,
        blocked_domains: Sequence[str] | None = None,
        context: Any = None,
        abort_event: asyncio.Event | None = None,
    ) -> ProviderSearchResponse:
        if not self.api_key:
            raise WebSearchProviderError("TAVILY_API_KEY is not set")
        payload: dict[str, Any] = {
            "api_key": self.api_key,
            "query": query,
            "max_results": self.max_results,
            "include_answer": True,
            "include_raw_content": False,
        }
        if allowed_domains:
            payload["include_domains"] = list(allowed_domains)
        if blocked_domains:
            payload["exclude_domains"] = list(blocked_domains)
        data = await _post_json(self.endpoint, payload, abort_event=abort_event)
        hits = tuple(
            SearchHit(
                title=str(item.get("title") or item.get("url") or ""),
                url=str(item.get("url") or ""),
                snippet=str(item.get("content") or "").strip() or None,
            )
            for item in data.get("results", []) if isinstance(item, Mapping)
        )
        hits = filter_hits_by_domain(hits, allowed_domains, blocked_domains)
        results: list[SearchResult | str] = []
        answer = str(data.get("answer") or "").strip()
        if answer:
            results.append(answer)
        results.append(SearchResult(tool_use_id="web_search_tavily", content=hits))
        return ProviderSearchResponse(results=tuple(results), provider=self.name)


class BraveSearchProvider(WebSearchProvider):
    name = "brave"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        endpoint: str | None = None,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> None:
        self.api_key = api_key or os.getenv("BRAVE_SEARCH_API_KEY")
        self.endpoint = endpoint or os.getenv(
            "OPENSPACE_BRAVE_SEARCH_URL", "https://api.search.brave.com/res/v1/web/search"
        )
        self.max_results = max_results

    async def search(
        self,
        *,
        query: str,
        allowed_domains: Sequence[str] | None = None,
        blocked_domains: Sequence[str] | None = None,
        context: Any = None,
        abort_event: asyncio.Event | None = None,
    ) -> ProviderSearchResponse:
        if not self.api_key:
            raise WebSearchProviderError("BRAVE_SEARCH_API_KEY is not set")
        data = await _get_json(
            self.endpoint,
            params={"q": query, "count": str(self.max_results)},
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": self.api_key,
            },
            abort_event=abort_event,
        )
        web = data.get("web") if isinstance(data, Mapping) else None
        raw_results = web.get("results", []) if isinstance(web, Mapping) else []
        hits = tuple(
            SearchHit(
                title=str(item.get("title") or item.get("url") or ""),
                url=str(item.get("url") or ""),
                snippet=_strip_html(str(item.get("description") or "")).strip() or None,
            )
            for item in raw_results if isinstance(item, Mapping)
        )
        hits = filter_hits_by_domain(hits, allowed_domains, blocked_domains)
        return ProviderSearchResponse(
            results=(SearchResult(tool_use_id="web_search_brave", content=hits),),
            provider=self.name,
        )


class SerpAPISearchProvider(WebSearchProvider):
    name = "serpapi"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        endpoint: str | None = None,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> None:
        self.api_key = api_key or os.getenv("SERPAPI_API_KEY")
        self.endpoint = endpoint or os.getenv(
            "OPENSPACE_SERPAPI_SEARCH_URL", "https://serpapi.com/search.json"
        )
        self.max_results = max_results

    async def search(
        self,
        *,
        query: str,
        allowed_domains: Sequence[str] | None = None,
        blocked_domains: Sequence[str] | None = None,
        context: Any = None,
        abort_event: asyncio.Event | None = None,
    ) -> ProviderSearchResponse:
        if not self.api_key:
            raise WebSearchProviderError("SERPAPI_API_KEY is not set")
        data = await _get_json(
            self.endpoint,
            params={
                "engine": "google",
                "q": query,
                "api_key": self.api_key,
                "num": str(self.max_results),
            },
            abort_event=abort_event,
        )
        raw_results = data.get("organic_results", []) if isinstance(data, Mapping) else []
        hits = tuple(
            SearchHit(
                title=str(item.get("title") or item.get("link") or ""),
                url=str(item.get("link") or ""),
                snippet=str(item.get("snippet") or "").strip() or None,
            )
            for item in raw_results if isinstance(item, Mapping)
        )
        hits = filter_hits_by_domain(hits, allowed_domains, blocked_domains)
        return ProviderSearchResponse(
            results=(SearchResult(tool_use_id="web_search_serpapi", content=hits),),
            provider=self.name,
        )


class DuckDuckGoHTMLSearchProvider(WebSearchProvider):
    name = "duckduckgo"

    def __init__(self, *, endpoint: str | None = None, max_results: int = DEFAULT_MAX_RESULTS) -> None:
        self.endpoint = endpoint or os.getenv(
            "OPENSPACE_DUCKDUCKGO_SEARCH_URL", "https://html.duckduckgo.com/html/"
        )
        self.max_results = max_results

    async def search(
        self,
        *,
        query: str,
        allowed_domains: Sequence[str] | None = None,
        blocked_domains: Sequence[str] | None = None,
        context: Any = None,
        abort_event: asyncio.Event | None = None,
    ) -> ProviderSearchResponse:
        body = await _get_text(
            self.endpoint,
            params={"q": query},
            headers={"User-Agent": "OpenSpace WebSearch/1.0"},
            abort_event=abort_event,
        )
        parser = _DuckDuckGoHTMLParser()
        parser.feed(body)
        hits = filter_hits_by_domain(parser.results, allowed_domains, blocked_domains)[: self.max_results]
        return ProviderSearchResponse(
            results=(SearchResult(tool_use_id="web_search_duckduckgo", content=hits),),
            provider=self.name,
        )


class _DuckDuckGoHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[SearchHit] = []
        self._current_href: str | None = None
        self._current_title: list[str] = []
        self._capture_title = False
        self._capture_snippet = False
        self._pending_snippet_for: int | None = None
        self._snippet: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}
        classes = set(attrs_dict.get("class", "").split())
        if tag == "a" and "result__a" in classes:
            self._current_href = _unwrap_duckduckgo_url(attrs_dict.get("href", ""))
            self._current_title = []
            self._capture_title = True
            return
        if "result__snippet" in classes and self.results:
            self._capture_snippet = True
            self._pending_snippet_for = len(self.results) - 1
            self._snippet = []

    def handle_data(self, data: str) -> None:
        if self._capture_title:
            self._current_title.append(data)
        if self._capture_snippet:
            self._snippet.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._capture_title:
            title = html.unescape("".join(self._current_title)).strip()
            if self._current_href and title:
                self.results.append(SearchHit(title=title, url=self._current_href))
            self._capture_title = False
            self._current_href = None
            self._current_title = []
        if self._capture_snippet and tag in {"a", "div"}:
            snippet = html.unescape(" ".join(self._snippet)).strip()
            idx = self._pending_snippet_for
            if snippet and idx is not None and 0 <= idx < len(self.results):
                hit = self.results[idx]
                self.results[idx] = SearchHit(hit.title, hit.url, snippet)
            self._capture_snippet = False
            self._pending_snippet_for = None
            self._snippet = []


async def _request_text(
    method: str,
    url: str,
    *,
    params: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
    json_payload: Mapping[str, Any] | None = None,
    abort_event: asyncio.Event | None = None,
) -> tuple[str, str | None]:
    timeout = aiohttp.ClientTimeout(total=SEARCH_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        request_task = asyncio.create_task(
            session.request(
                method,
                url,
                params=params,
                headers=headers,
                json=json_payload,
            )
        )
        if abort_event is not None:
            abort_task = asyncio.create_task(abort_event.wait())
            done, pending = await asyncio.wait(
                {request_task, abort_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for pending_task in pending:
                pending_task.cancel()
            if abort_task in done:
                request_task.cancel()
                raise asyncio.CancelledError("WebSearch aborted")
        response = await request_task
        async with response:
            body = await response.text()
            if response.status >= 400:
                raise WebSearchProviderError(
                    f"HTTP {response.status} from {url}: {body[:500]}"
                )
            return body, response.headers.get("content-type")


async def _get_json(
    url: str,
    *,
    params: Mapping[str, str],
    headers: Mapping[str, str] | None = None,
    abort_event: asyncio.Event | None = None,
) -> Any:
    body, _content_type = await _request_text(
        "GET", url, params=params, headers=headers, abort_event=abort_event
    )
    return json.loads(body)


async def _post_json(
    url: str,
    payload: Mapping[str, Any],
    *,
    abort_event: asyncio.Event | None = None,
) -> Any:
    body, _content_type = await _request_text(
        "POST", url, json_payload=payload, abort_event=abort_event
    )
    return json.loads(body)


async def _get_text(
    url: str,
    *,
    params: Mapping[str, str],
    headers: Mapping[str, str] | None = None,
    abort_event: asyncio.Event | None = None,
) -> str:
    body, _content_type = await _request_text(
        "GET", url, params=params, headers=headers, abort_event=abort_event
    )
    return body


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", html.unescape(text))


def _unwrap_duckduckgo_url(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    uddg = query.get("uddg")
    if uddg:
        return unquote(uddg[0])
    return url


def _provider_from_name(name: str, *, max_results: int = DEFAULT_MAX_RESULTS) -> WebSearchProvider:
    normalized = name.strip().lower()
    if normalized in {"anthropic", "claude", "server"}:
        return AnthropicServerWebSearchProvider(max_uses=max_results)
    if normalized == "tavily":
        return TavilySearchProvider(max_results=max_results)
    if normalized == "brave":
        return BraveSearchProvider(max_results=max_results)
    if normalized in {"serpapi", "serp"}:
        return SerpAPISearchProvider(max_results=max_results)
    if normalized in {"duckduckgo", "ddg"}:
        return DuckDuckGoHTMLSearchProvider(max_results=max_results)
    raise WebSearchProviderError(f"Unknown web search provider: {name}")


def get_default_provider_names() -> tuple[str, ...]:
    configured = os.getenv("OPENSPACE_WEB_SEARCH_PROVIDER")
    if configured:
        return tuple(item.strip() for item in configured.split(",") if item.strip())
    names: list[str] = []
    if os.getenv("ANTHROPIC_API_KEY"):
        names.append("anthropic")
    if os.getenv("TAVILY_API_KEY"):
        names.append("tavily")
    if os.getenv("BRAVE_SEARCH_API_KEY"):
        names.append("brave")
    if os.getenv("SERPAPI_API_KEY"):
        names.append("serpapi")
    names.append("duckduckgo")
    return tuple(names)


async def run_web_search(
    query: str,
    *,
    allowed_domains: Sequence[str] | None = None,
    blocked_domains: Sequence[str] | None = None,
    providers: Sequence[WebSearchProvider] | None = None,
    max_results: int = DEFAULT_MAX_RESULTS,
    context: Any = None,
    abort_event: asyncio.Event | None = None,
) -> WebSearchOutput:
    start = time.time()
    provider_list = list(
        providers
        or [_provider_from_name(name, max_results=max_results) for name in get_default_provider_names()]
    )
    errors: list[str] = []
    for provider in provider_list:
        if abort_event is not None and abort_event.is_set():
            raise asyncio.CancelledError("WebSearch aborted")
        try:
            response = await provider.search(
                query=query,
                allowed_domains=allowed_domains,
                blocked_domains=blocked_domains,
                context=context,
                abort_event=abort_event,
            )
            return WebSearchOutput(
                query=query,
                results=response.results,
                durationSeconds=time.time() - start,
                provider=response.provider,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = f"{provider.name}: {exc}"
            logger.warning("Web search provider failed: %s", message)
            errors.append(message)
    raise WebSearchProviderError("All web search providers failed: " + "; ".join(errors))


class WebSearchTool(BaseTool):
    backend_type = BackendType.WEB
    _name = WEB_SEARCH_TOOL_NAME
    should_defer = True
    search_hint = "search the web for current information"
    max_result_size_chars = MAX_RESULT_SIZE_CHARS
    _is_read_only = True
    _is_concurrency_safe = True
    _description = get_web_search_prompt()
    parameter_descriptions = {
        "query": "The search query to use",
        "allowed_domains": "Only include search results from these domains",
        "blocked_domains": "Never include search results from these domains",
    }

    def __init__(
        self,
        *,
        providers: Sequence[WebSearchProvider] | None = None,
        allowed_domains: Sequence[str] | None = None,
        blocked_domains: Sequence[str] | None = None,
        max_searches_per_call: int = MAX_SEARCH_USES,
    ) -> None:
        super().__init__()
        self._providers = tuple(providers) if providers is not None else None
        self._default_allowed_domains = tuple(allowed_domains or ())
        self._default_blocked_domains = tuple(blocked_domains or ())
        self._max_searches_per_call = max_searches_per_call
        self._current_context: Any | None = None

    def get_prompt(self, context: Any = None) -> str:
        return get_web_search_prompt()

    def set_context(self, context: Any) -> "WebSearchTool":
        self._current_context = context
        return self

    async def validate_input(self, input: dict[str, Any], context: Any = None) -> str | None:
        query = str(input.get("query", "") or "")
        if not query:
            return "Error: Missing query"
        if len(query) < 2:
            return "Error: query must contain at least 2 characters"
        return validate_domains(input.get("allowed_domains"), input.get("blocked_domains"))

    async def check_permissions(self, input: dict[str, Any], context: Any = None):
        return PermissionPassthrough(
            message="WebSearchTool requires permission.",
            suggestions=build_permission_suggestions(),
        )

    async def _arun(
        self,
        query: str,
        allowed_domains: list[str] | None = None,
        blocked_domains: list[str] | None = None,
    ) -> ToolResult:
        effective_allowed = (
            allowed_domains
            if allowed_domains is not None
            else list(self._default_allowed_domains) or None
        )
        effective_blocked = (
            blocked_domains
            if blocked_domains is not None
            else list(self._default_blocked_domains) or None
        )
        validation_error = await self.validate_input(
            {
                "query": query,
                "allowed_domains": effective_allowed,
                "blocked_domains": effective_blocked,
            }
        )
        if validation_error is not None:
            return ToolResult(status=ToolStatus.ERROR, content=validation_error, error=validation_error)

        context = self._current_context
        abort_event = getattr(context, "abort_event", None)
        output = await run_web_search(
            query,
            allowed_domains=effective_allowed,
            blocked_domains=effective_blocked,
            providers=self._providers,
            max_results=self._max_searches_per_call,
            context=context,
            abort_event=abort_event,
        )
        content = format_search_output_for_model(output)
        metadata = output.to_dict()
        metadata.update(
            {
                "tool": self.name,
                "duration_seconds": output.durationSeconds,
                "durationSeconds": output.durationSeconds,
            }
        )
        return ToolResult(status=ToolStatus.SUCCESS, content=content, metadata=metadata)


__all__ = [
    "ANTHROPIC_WEB_SEARCH_TOOL_TYPE",
    "WEB_SEARCH_TOOL_ALIAS",
    "MAX_SEARCH_USES",
    "SearchHit",
    "SearchResult",
    "WebSearchOutput",
    "WebSearchProvider",
    "WebSearchProviderError",
    "AnthropicServerWebSearchProvider",
    "TavilySearchProvider",
    "BraveSearchProvider",
    "SerpAPISearchProvider",
    "DuckDuckGoHTMLSearchProvider",
    "WEB_SEARCH_TOOL_NAME",
    "WebSearchTool",
    "build_permission_suggestions",
    "domain_allowed",
    "filter_hits_by_domain",
    "format_search_output_for_model",
    "get_default_provider_names",
    "get_web_search_prompt",
    "make_output_from_search_response",
    "make_tool_schema",
    "run_web_search",
    "validate_domains",
]
