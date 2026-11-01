"""Web backend session for OpenSpace WebSearch and WebFetch tools.

OpenSpace has no backend/session abstraction for web tools; ``WebSearch`` and
``WebFetch`` are flat built-in tools.  OpenSpace keeps its BackendType.WEB
provider/session boundary, but the session now only registers those two
primitive tools by default.  The old Perplexity ``deep_research_agent`` path
is intentionally not part of this backend; deep research is handled by the
``deep-researcher`` subagent that composes ``web_search`` and ``web_fetch``.
"""

from __future__ import annotations

import os
from typing import Any, Iterable

from openspace.config import get_config
from openspace.config.grounding import WebConfig, WebSearchConfig
from openspace.grounding.core.session import BaseSession
from openspace.grounding.core.transport.connectors import BaseConnector
from openspace.grounding.core.types import BackendType, SessionConfig
from openspace.host_detection import load_runtime_env
from openspace.tools.web_fetch_tool import (
    FETCH_TIMEOUT_SECONDS,
    MAX_MARKDOWN_LENGTH,
    WebFetchTool,
)
from openspace.tools.web_search_tool import (
    ANTHROPIC_WEB_SEARCH_TOOL_TYPE,
    AnthropicServerWebSearchProvider,
    BraveSearchProvider,
    DuckDuckGoHTMLSearchProvider,
    MAX_SEARCH_USES,
    SerpAPISearchProvider,
    TavilySearchProvider,
    WebSearchProvider,
    WebSearchTool,
)
from openspace.utils.logging import Logger


load_runtime_env()
logger = Logger.get_logger(__name__)


class WebConnector(BaseConnector[Any]):
    """No-op lifecycle connector for local web tools.

    WebSearch/WebFetch own their HTTP/API clients per call.  A session-level
    connector is still supplied because BaseSession expects one, but listing
    tools must not require OpenRouter, Anthropic, or any other network setup.
    """

    def __init__(self) -> None:
        self._connected = False

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def invoke(self, name: str, params: dict[str, Any]) -> Any:
        raise NotImplementedError(f"WebConnector has no session RPC method: {name}")

    async def request(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("Web backend tools perform their own HTTP requests")


class WebSession(BaseSession):
    backend_type = BackendType.WEB

    def __init__(
        self,
        *,
        session_id: str,
        config: SessionConfig,
        web_config: WebConfig | dict[str, Any] | None = None,
        auto_connect: bool = True,
        auto_initialize: bool = True,
    ) -> None:
        super().__init__(
            connector=WebConnector(),
            session_id=session_id,
            backend_type=BackendType.WEB,
            auto_connect=auto_connect,
            auto_initialize=auto_initialize,
        )
        self.config = config
        self.web_config = _coerce_web_config(web_config)

    @property
    def web_connector(self) -> WebConnector:
        return self.connector

    async def initialize(self) -> dict[str, Any]:
        """Register OpenSpace web tools without opening a network connection."""

        if not self.is_connected:
            await self.connect()

        if self.tools:
            logger.debug("Web session %s already initialized, skipping", self.session_id)
            return self._session_info()

        search_cfg = self.web_config.search
        fetch_cfg = self.web_config.fetch
        self.tools = [
            WebSearchTool(
                providers=_build_search_providers(search_cfg),
                allowed_domains=list(search_cfg.allowed_domains),
                blocked_domains=list(search_cfg.blocked_domains),
                max_searches_per_call=_positive_int(
                    search_cfg.max_searches_per_call,
                    MAX_SEARCH_USES,
                ),
            ),
            WebFetchTool(
                summarize_model=_resolve_env_placeholder(fetch_cfg.summarize_model),
                max_content_length=_positive_int(
                    fetch_cfg.max_content_length,
                    MAX_MARKDOWN_LENGTH,
                ),
                request_timeout=_positive_int(
                    fetch_cfg.request_timeout,
                    FETCH_TIMEOUT_SECONDS,
                ),
                user_agent=fetch_cfg.user_agent or "OpenSpace WebFetch",
                preapproved_domains=list(fetch_cfg.preapproved_domains),
            ),
        ]

        logger.info(
            "Initialized Web session %s with tools: %s",
            self.session_id,
            [tool.name for tool in self.tools],
        )
        return self._session_info()

    def _session_info(self) -> dict[str, Any]:
        search_cfg = self.web_config.search
        return {
            "tools": [tool.name for tool in self.tools],
            "backend": BackendType.WEB.value,
            "web_search": {
                "server_tool_type": ANTHROPIC_WEB_SEARCH_TOOL_TYPE,
                "max_searches_per_call": search_cfg.max_searches_per_call,
                "providers": [
                    getattr(provider, "name", provider.__class__.__name__)
                    for provider in _build_search_providers(search_cfg)
                ],
            },
        }


def _coerce_web_config(config: WebConfig | dict[str, Any] | None) -> WebConfig:
    if isinstance(config, WebConfig):
        return config
    if isinstance(config, dict):
        return WebConfig.model_validate(config)
    loaded = get_config().get_backend_config(BackendType.WEB.value)
    if isinstance(loaded, WebConfig):
        return loaded
    if isinstance(loaded, dict):
        return WebConfig.model_validate(loaded)
    return WebConfig()


def _resolve_env_placeholder(value: str | None) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    if value.startswith("${") and value.endswith("}") and len(value) > 3:
        return os.getenv(value[2:-1]) or None
    return os.path.expandvars(value)


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
        return parsed if parsed > 0 else default
    except Exception:
        return default


def _provider_names(search_cfg: WebSearchConfig) -> list[str]:
    explicit = [name.strip() for name in search_cfg.provider_order if name.strip()]
    if explicit:
        return _dedupe(explicit)

    names: list[str] = []
    api_key = _resolve_env_placeholder(search_cfg.search_api_key) or os.getenv("ANTHROPIC_API_KEY")
    if api_key:
        names.append("anthropic")
    fallback = search_cfg.fallback_search_provider or ""
    names.extend(_split_csv(fallback))
    if not names:
        names.append("duckduckgo")
    return _dedupe(names)


def _split_csv(value: str | Iterable[str]) -> list[str]:
    if isinstance(value, str):
        raw_items = value.split(",")
    else:
        raw_items = list(value)
    return [
        str(item).strip()
        for item in raw_items
        if str(item).strip() and str(item).strip().lower() not in {"none", "disabled", "false"}
    ]


def _dedupe(names: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for name in names:
        normalized = name.strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _build_search_providers(search_cfg: WebSearchConfig) -> tuple[WebSearchProvider, ...]:
    max_results = _positive_int(search_cfg.max_searches_per_call, MAX_SEARCH_USES)
    providers: list[WebSearchProvider] = []
    for name in _provider_names(search_cfg):
        try:
            provider = _build_search_provider(name, search_cfg, max_results=max_results)
        except ValueError as exc:
            logger.warning("%s", exc)
            continue
        providers.append(provider)
    return tuple(providers)


def _build_search_provider(
    name: str,
    search_cfg: WebSearchConfig,
    *,
    max_results: int,
) -> WebSearchProvider:
    normalized = name.strip().lower()
    if normalized in {"anthropic", "claude", "server"}:
        return AnthropicServerWebSearchProvider(
            api_key=_resolve_env_placeholder(search_cfg.search_api_key),
            model=search_cfg.search_model,
            base_url=_resolve_env_placeholder(search_cfg.search_base_url),
            max_uses=max_results,
        )
    if normalized == "tavily":
        return TavilySearchProvider(max_results=max_results)
    if normalized == "brave":
        return BraveSearchProvider(max_results=max_results)
    if normalized in {"serpapi", "serp"}:
        return SerpAPISearchProvider(max_results=max_results)
    if normalized in {"duckduckgo", "ddg"}:
        return DuckDuckGoHTMLSearchProvider(max_results=max_results)
    raise ValueError(f"Unknown web search provider configured for WebSession: {name}")


__all__ = [
    "WebConnector",
    "WebSession",
]
