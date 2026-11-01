"""WebFetchTool.

OpenSpace owns the engine-facing semantics:
``web_fetch`` is read-only, concurrency-safe, deferred, domain-permissioned,
and uses model-neutral ``LLMClient`` calls for secondary markdown processing.
"""

from __future__ import annotations

import asyncio
import html
import mimetypes
import os
import re
import tempfile
import time
from collections import OrderedDict
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping
from urllib.parse import urljoin, urlparse, urlunparse

import aiohttp

from openspace.grounding.core.permissions.types import (
    AddRulesUpdate,
    DecisionReasonOther,
    DecisionReasonRule,
    PermissionAllow,
    PermissionAsk,
    PermissionDeny,
    PermissionRule,
    PermissionRuleValue,
    ToolPermissionContext,
    parse_rule_value,
)
from openspace.grounding.core.tool.base import BaseTool
from openspace.grounding.core.types import BackendType, ToolResult, ToolStatus
from openspace.services.conversation.messages import get_assistant_message_text
from openspace.utils.logging import Logger

if TYPE_CHECKING:
    from openspace.llm import LLMClient

logger = Logger.get_logger(__name__)

WEB_FETCH_TOOL_NAME = "web_fetch"
WEB_FETCH_TOOL_ALIAS = "WebFetch"

CACHE_TTL_SECONDS = 15 * 60
MAX_CACHE_SIZE_BYTES = 50 * 1024 * 1024
MAX_URL_LENGTH = 2000
MAX_HTTP_CONTENT_LENGTH = 10 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 60
MAX_REDIRECTS = 10
MAX_MARKDOWN_LENGTH = 100_000

DESCRIPTION = """
- Fetches content from a specified URL and processes it using an AI model
- Takes a URL and a prompt as input
- Fetches the URL content, converts HTML to markdown
- Processes the content with the prompt using a small, fast model
- Returns the model's response about the content
- Use this tool when you need to retrieve and analyze web content

Usage notes:
  - IMPORTANT: If an MCP-provided web fetch tool is available, prefer using that tool instead of this one, as it may have fewer restrictions.
  - The URL must be a fully-formed valid URL
  - HTTP URLs will be automatically upgraded to HTTPS
  - The prompt should describe what information you want to extract from the page
  - This tool is read-only and does not modify any files
  - Results may be summarized if the content is very large
  - Includes a self-cleaning 15-minute cache for faster responses when repeatedly accessing the same URL
  - When a URL redirects to a different host, the tool will inform you and provide the redirect URL in a special format. You should then make a new WebFetch request with the redirect URL to fetch the content.
  - For GitHub URLs, prefer using the gh CLI via Bash instead (e.g., gh pr view, gh issue view, gh api).
"""

TOOL_PROMPT = (
    "IMPORTANT: WebFetch WILL FAIL for authenticated or private URLs. Before "
    "using this tool, check if the URL points to an authenticated service "
    "(e.g. Google Docs, Confluence, Jira, GitHub). If so, look for a "
    "specialized MCP tool that provides authenticated access.\n"
    f"{DESCRIPTION}"
)


PREAPPROVED_HOSTS: frozenset[str] = frozenset(
    {
        # Agent platform documentation and related developer resources.
        "platform.claude.com",
        "code.claude.com",
        "modelcontextprotocol.io",
        "github.com/anthropics",
        "agentskills.io",
        # Top Programming Languages
        "docs.python.org",
        "en.cppreference.com",
        "docs.oracle.com",
        "learn.microsoft.com",
        "developer.mozilla.org",
        "go.dev",
        "pkg.go.dev",
        "www.php.net",
        "docs.swift.org",
        "kotlinlang.org",
        "ruby-doc.org",
        "doc.rust-lang.org",
        "www.typescriptlang.org",
        # Web & JavaScript Frameworks/Libraries
        "react.dev",
        "angular.io",
        "vuejs.org",
        "nextjs.org",
        "expressjs.com",
        "nodejs.org",
        "bun.sh",
        "jquery.com",
        "getbootstrap.com",
        "tailwindcss.com",
        "d3js.org",
        "threejs.org",
        "redux.js.org",
        "webpack.js.org",
        "jestjs.io",
        "reactrouter.com",
        # Python Frameworks & Libraries
        "docs.djangoproject.com",
        "flask.palletsprojects.com",
        "fastapi.tiangolo.com",
        "pandas.pydata.org",
        "numpy.org",
        "www.tensorflow.org",
        "pytorch.org",
        "scikit-learn.org",
        "matplotlib.org",
        "requests.readthedocs.io",
        "jupyter.org",
        # PHP Frameworks
        "laravel.com",
        "symfony.com",
        "wordpress.org",
        # Java Frameworks & Libraries
        "docs.spring.io",
        "hibernate.org",
        "tomcat.apache.org",
        "gradle.org",
        "maven.apache.org",
        # .NET & C# Frameworks
        "asp.net",
        "dotnet.microsoft.com",
        "nuget.org",
        "blazor.net",
        # Mobile Development
        "reactnative.dev",
        "docs.flutter.dev",
        "developer.apple.com",
        "developer.android.com",
        # Data Science & Machine Learning
        "keras.io",
        "spark.apache.org",
        "huggingface.co",
        "www.kaggle.com",
        # Databases
        "www.mongodb.com",
        "redis.io",
        "www.postgresql.org",
        "dev.mysql.com",
        "www.sqlite.org",
        "graphql.org",
        "prisma.io",
        # Cloud & DevOps
        "docs.aws.amazon.com",
        "cloud.google.com",
        "kubernetes.io",
        "www.docker.com",
        "www.terraform.io",
        "www.ansible.com",
        "vercel.com/docs",
        "docs.netlify.com",
        "devcenter.heroku.com",
        # Testing & Monitoring
        "cypress.io",
        "selenium.dev",
        # Game Development
        "docs.unity.com",
        "docs.unrealengine.com",
        # Other Essential Tools
        "git-scm.com",
        "nginx.org",
        "httpd.apache.org",
    }
)


def _split_preapproved_hosts() -> tuple[frozenset[str], Mapping[str, tuple[str, ...]]]:
    hosts: set[str] = set()
    paths: dict[str, list[str]] = {}
    for entry in PREAPPROVED_HOSTS:
        slash = entry.find("/")
        if slash == -1:
            hosts.add(entry)
            continue
        host = entry[:slash]
        prefix = entry[slash:]
        paths.setdefault(host, []).append(prefix)
    return frozenset(hosts), {host: tuple(prefixes) for host, prefixes in paths.items()}


_HOSTNAME_ONLY, _PATH_PREFIXES = _split_preapproved_hosts()


@dataclass(frozen=True)
class CacheEntry:
    bytes: int
    code: int
    code_text: str
    content: str
    content_type: str
    persisted_path: str | None = None
    persisted_size: int | None = None


@dataclass(frozen=True)
class RedirectInfo:
    type: str
    original_url: str
    redirect_url: str
    status_code: int


@dataclass(frozen=True)
class FetchedContent:
    content: str
    bytes: int
    code: int
    code_text: str
    content_type: str
    persisted_path: str | None = None
    persisted_size: int | None = None


@dataclass(frozen=True)
class HttpResponse:
    status: int
    status_text: str
    headers: Mapping[str, str]
    data: bytes


class EgressBlockedError(RuntimeError):
    def __init__(self, domain: str) -> None:
        super().__init__(
            '{"error_type":"EGRESS_BLOCKED","domain":"%s","message":"Access to %s is blocked by the network egress proxy."}'
            % (domain, domain)
        )
        self.domain = domain


class _SizedTTLCache:
    def __init__(self, *, max_size_bytes: int | None = None, max_entries: int | None = None, ttl_seconds: float) -> None:
        self.max_size_bytes = max_size_bytes
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._items: OrderedDict[str, tuple[float, int, Any]] = OrderedDict()
        self._size = 0

    def clear(self) -> None:
        self._items.clear()
        self._size = 0

    def get(self, key: str) -> Any | None:
        value = self._items.get(key)
        if value is None:
            return None
        expires_at, size, payload = value
        if expires_at <= time.time():
            self._items.pop(key, None)
            self._size -= size
            return None
        self._items.move_to_end(key)
        return payload

    def set(self, key: str, payload: Any, *, size: int = 1) -> None:
        size = max(1, int(size))
        old = self._items.pop(key, None)
        if old is not None:
            self._size -= old[1]
        self._items[key] = (time.time() + self.ttl_seconds, size, payload)
        self._size += size
        self._evict()

    def _evict(self) -> None:
        now = time.time()
        for key in list(self._items.keys()):
            expires_at, size, _ = self._items[key]
            if expires_at <= now:
                self._items.pop(key, None)
                self._size -= size
        while self.max_entries is not None and len(self._items) > self.max_entries:
            _, (_, size, _) = self._items.popitem(last=False)
            self._size -= size
        while self.max_size_bytes is not None and self._size > self.max_size_bytes and self._items:
            _, (_, size, _) = self._items.popitem(last=False)
            self._size -= size


_URL_CACHE = _SizedTTLCache(max_size_bytes=MAX_CACHE_SIZE_BYTES, ttl_seconds=CACHE_TTL_SECONDS)


def clear_web_fetch_cache() -> None:
    _URL_CACHE.clear()


def is_preapproved_host(hostname: str, pathname: str) -> bool:
    if hostname in _HOSTNAME_ONLY:
        return True
    prefixes = _PATH_PREFIXES.get(hostname)
    if not prefixes:
        return False
    for prefix in prefixes:
        if pathname == prefix or pathname.startswith(prefix + "/"):
            return True
    return False


def is_preapproved_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return bool(parsed.hostname) and is_preapproved_host(parsed.hostname, parsed.path or "/")
    except Exception:
        return False


def validate_url(url: str) -> bool:
    if len(url) > MAX_URL_LENGTH:
        return False
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False
    if len(parsed.hostname.split(".")) < 2:
        return False
    return True


def is_permitted_redirect(original_url: str, redirect_url: str) -> bool:
    try:
        original = urlparse(original_url)
        redirected = urlparse(redirect_url)
    except Exception:
        return False
    if redirected.scheme != original.scheme:
        return False
    if redirected.port != original.port:
        return False
    if redirected.username or redirected.password:
        return False

    def strip_www(hostname: str | None) -> str:
        return (hostname or "").removeprefix("www.")

    return strip_www(original.hostname) == strip_www(redirected.hostname)


async def _http_get_no_redirects(
    url: str,
    signal: asyncio.Event | None = None,
    *,
    request_timeout: int = FETCH_TIMEOUT_SECONDS,
    user_agent: str = "OpenSpace WebFetch",
) -> HttpResponse:
    if signal is not None and signal.is_set():
        raise asyncio.CancelledError("WebFetch aborted")
    timeout = aiohttp.ClientTimeout(total=request_timeout)
    headers = {
        "Accept": "text/markdown, text/html, */*",
        "User-Agent": user_agent,
    }
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url, allow_redirects=False) as response:
            if signal is not None and signal.is_set():
                raise asyncio.CancelledError("WebFetch aborted")
            raw = await response.content.read(MAX_HTTP_CONTENT_LENGTH + 1)
            if len(raw) > MAX_HTTP_CONTENT_LENGTH:
                raise RuntimeError(
                    f"Response content exceeds {MAX_HTTP_CONTENT_LENGTH} byte WebFetch limit"
                )
            return HttpResponse(
                status=response.status,
                status_text=response.reason or "",
                headers={k.lower(): v for k, v in response.headers.items()},
                data=raw,
            )


FetchFn = Callable[[str, asyncio.Event | None], Awaitable[HttpResponse]]


async def get_with_permitted_redirects(
    url: str,
    signal: asyncio.Event | None,
    redirect_checker: Callable[[str, str], bool],
    *,
    depth: int = 0,
    fetcher: FetchFn | None = None,
) -> HttpResponse | RedirectInfo:
    if depth > MAX_REDIRECTS:
        raise RuntimeError(f"Too many redirects (exceeded {MAX_REDIRECTS})")
    fetch = fetcher or _http_get_no_redirects
    response = await fetch(url, signal)

    if response.status in {301, 302, 307, 308}:
        location = response.headers.get("location")
        if not location:
            raise RuntimeError("Redirect missing Location header")
        redirect_url = urljoin(url, location)
        if redirect_checker(url, redirect_url):
            return await get_with_permitted_redirects(
                redirect_url,
                signal,
                redirect_checker,
                depth=depth + 1,
                fetcher=fetch,
            )
        return RedirectInfo(
            type="redirect",
            original_url=url,
            redirect_url=redirect_url,
            status_code=response.status,
        )

    if response.status == 403 and response.headers.get("x-proxy-error") == "blocked-by-allowlist":
        hostname = urlparse(url).hostname or url
        raise EgressBlockedError(hostname)
    if response.status >= 400:
        raise RuntimeError(f"HTTP {response.status} {response.status_text}".strip())
    return response


def _upgrade_http_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme == "http":
        parsed = parsed._replace(scheme="https")
        return urlunparse(parsed)
    return url


def _is_binary_content_type(content_type: str) -> bool:
    base = content_type.split(";", 1)[0].strip().lower()
    if not base:
        return False
    if base.startswith("text/"):
        return False
    if base in {
        "application/json",
        "application/javascript",
        "application/x-javascript",
        "application/xml",
        "application/xhtml+xml",
        "application/rss+xml",
        "application/atom+xml",
        "image/svg+xml",
    }:
        return False
    return True


def _persist_binary_content(raw: bytes, content_type: str) -> tuple[str, int] | None:
    try:
        base_dir = Path(os.getenv("OPENSPACE_WEB_FETCH_OUTPUT_DIR", "") or tempfile.gettempdir())
        target_dir = base_dir / "openspace" / "webfetch"
        target_dir.mkdir(parents=True, exist_ok=True)
        ext = mimetypes.guess_extension(content_type.split(";", 1)[0].strip()) or ".bin"
        path = target_dir / f"webfetch-{int(time.time() * 1000)}-{os.urandom(3).hex()}{ext}"
        path.write_bytes(raw)
        return str(path), len(raw)
    except Exception as exc:
        logger.debug("Failed to persist WebFetch binary content: %s", exc)
        return None


class _MarkdownHTMLParser(HTMLParser):
    """Small Turndown-like fallback for HTML pages.

    It intentionally handles only structural tags that matter for model input.
    This keeps WebFetch dependency-free; exact Turndown parity is documented as
    an OS runtime difference in the checklist completion note.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.href_stack: list[str | None] = []
        self.skip_depth = 0
        self.list_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if tag in {"script", "style", "noscript"}:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in {"p", "div", "section", "article", "header", "footer", "br"}:
            self._newline()
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            level = int(tag[1])
            self._newline()
            self.parts.append("#" * level + " ")
        elif tag in {"strong", "b"}:
            self.parts.append("**")
        elif tag in {"em", "i"}:
            self.parts.append("_")
        elif tag == "a":
            self.href_stack.append(attrs_dict.get("href"))
            self.parts.append("[")
        elif tag in {"ul", "ol"}:
            self.list_depth += 1
            self._newline()
        elif tag == "li":
            self._newline()
            self.parts.append("  " * max(0, self.list_depth - 1) + "- ")
        elif tag == "pre":
            self._newline()
            self.parts.append("```\n")
        elif tag == "code":
            self.parts.append("`")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self.skip_depth:
            self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if tag in {"p", "div", "section", "article", "header", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "li"}:
            self._newline()
        elif tag in {"strong", "b"}:
            self.parts.append("**")
        elif tag in {"em", "i"}:
            self.parts.append("_")
        elif tag == "a":
            href = self.href_stack.pop() if self.href_stack else None
            self.parts.append("]")
            if href:
                self.parts.append(f"({href})")
        elif tag in {"ul", "ol"}:
            self.list_depth = max(0, self.list_depth - 1)
            self._newline()
        elif tag == "pre":
            self.parts.append("\n```")
            self._newline()
        elif tag == "code":
            self.parts.append("`")

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        text = re.sub(r"\s+", " ", html.unescape(data))
        if text.strip():
            self.parts.append(text)

    def markdown(self) -> str:
        text = "".join(self.parts)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _newline(self) -> None:
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")


def html_to_markdown(html_content: str) -> str:
    parser = _MarkdownHTMLParser()
    parser.feed(html_content)
    parser.close()
    return parser.markdown()


async def get_url_markdown_content(
    url: str,
    abort_event: asyncio.Event | None = None,
    *,
    fetcher: FetchFn | None = None,
    request_timeout: int = FETCH_TIMEOUT_SECONDS,
    user_agent: str = "OpenSpace WebFetch",
) -> FetchedContent | RedirectInfo:
    if not validate_url(url):
        raise RuntimeError("Invalid URL")

    cached = _URL_CACHE.get(url)
    if cached is not None:
        return FetchedContent(
            content=cached.content,
            bytes=cached.bytes,
            code=cached.code,
            code_text=cached.code_text,
            content_type=cached.content_type,
            persisted_path=cached.persisted_path,
            persisted_size=cached.persisted_size,
        )

    upgraded_url = _upgrade_http_url(url)

    effective_fetcher = fetcher
    if effective_fetcher is None:
        async def effective_fetcher(target: str, signal: asyncio.Event | None) -> HttpResponse:
            return await _http_get_no_redirects(
                target,
                signal,
                request_timeout=request_timeout,
                user_agent=user_agent,
            )

    response = await get_with_permitted_redirects(
        upgraded_url,
        abort_event,
        is_permitted_redirect,
        fetcher=effective_fetcher,
    )
    if isinstance(response, RedirectInfo):
        return response

    raw = bytes(response.data)
    content_type = response.headers.get("content-type", "")
    persisted_path: str | None = None
    persisted_size: int | None = None
    if _is_binary_content_type(content_type):
        persisted = _persist_binary_content(raw, content_type)
        if persisted is not None:
            persisted_path, persisted_size = persisted

    content = raw.decode("utf-8", errors="replace")
    if "text/html" in content_type:
        markdown_content = html_to_markdown(content)
        content_bytes = len(markdown_content.encode("utf-8"))
    else:
        markdown_content = content
        content_bytes = len(raw)

    entry = CacheEntry(
        bytes=len(raw),
        code=response.status,
        code_text=response.status_text,
        content=markdown_content,
        content_type=content_type,
        persisted_path=persisted_path,
        persisted_size=persisted_size,
    )
    _URL_CACHE.set(url, entry, size=max(1, content_bytes))
    return FetchedContent(
        content=entry.content,
        bytes=entry.bytes,
        code=entry.code,
        code_text=entry.code_text,
        content_type=entry.content_type,
        persisted_path=entry.persisted_path,
        persisted_size=entry.persisted_size,
    )


def make_secondary_model_prompt(markdown_content: str, prompt: str, is_preapproved_domain: bool) -> str:
    if is_preapproved_domain:
        guidelines = (
            "Provide a concise response based on the content above. Include "
            "relevant details, code examples, and documentation excerpts as needed."
        )
    else:
        guidelines = """Provide a concise response based only on the content above. In your response:
 - Enforce a strict 125-character maximum for quotes from any source document. Open Source Software is ok as long as we respect the license.
 - Use quotation marks for exact language from articles; any language outside of the quotation should never be word-for-word the same.
 - You are not a lawyer and never comment on the legality of your own prompts and responses.
 - Never produce or reproduce exact song lyrics."""
    return f"""
Web page content:
---
{markdown_content}
---

{prompt}

{guidelines}
"""


async def apply_prompt_to_markdown(
    prompt: str,
    markdown_content: str,
    abort_event: asyncio.Event | None = None,
    *,
    is_non_interactive_session: bool = False,
    is_preapproved_domain: bool = False,
    llm_client: LLMClient | None = None,
    model: str | None = None,
    max_markdown_length: int = MAX_MARKDOWN_LENGTH,
) -> str:
    truncated_content = (
        markdown_content[:max_markdown_length] + "\n\n[Content truncated due to length...]"
        if len(markdown_content) > max_markdown_length
        else markdown_content
    )
    model_prompt = make_secondary_model_prompt(
        truncated_content,
        prompt,
        is_preapproved_domain,
    )
    model_override = model or os.getenv("OPENSPACE_WEB_FETCH_MODEL")
    if llm_client is None:
        from openspace.llm import LLMClient

        llm_client = LLMClient(model=model_override) if model_override else LLMClient()
    client = llm_client
    call_model = getattr(client, "call_model_with_fallback", None) or client.call_model
    response = await call_model(
        messages=[{"role": "user", "content": model_prompt}],
        abort_event=abort_event,
        max_tokens=int(os.getenv("OPENSPACE_WEB_FETCH_MAX_TOKENS", "4096")),
    )
    if abort_event is not None and abort_event.is_set():
        raise asyncio.CancelledError("WebFetch aborted")
    text = get_assistant_message_text(response.assistant_message)
    return text if text else "No response from model"


def web_fetch_tool_input_to_permission_rule_content(input_: Mapping[str, Any]) -> str:
    try:
        url = str(input_.get("url", ""))
        parsed = urlparse(url)
        if not parsed.hostname:
            return f"input:{input_}"
        return f"domain:{parsed.hostname}"
    except Exception:
        return f"input:{input_}"


def _find_rule_by_content(
    permission_context: ToolPermissionContext,
    behavior: str,
    rule_content: str,
) -> PermissionRule | None:
    buckets = {
        "allow": permission_context.always_allow_rules,
        "deny": permission_context.always_deny_rules,
        "ask": permission_context.always_ask_rules,
    }[behavior]
    for source, raw_rules in buckets.items():
        for raw in raw_rules or ():
            try:
                value = parse_rule_value(raw)
            except ValueError:
                continue
            if value.tool_name == WEB_FETCH_TOOL_NAME and value.rule_content == rule_content:
                return PermissionRule(source=source, rule_behavior=behavior, rule_value=value)
    return None


def build_suggestions(rule_content: str) -> tuple[AddRulesUpdate, ...]:
    return (
        AddRulesUpdate(
            destination="localSettings",
            rules=(PermissionRuleValue(tool_name=WEB_FETCH_TOOL_NAME, rule_content=rule_content),),
            behavior="allow",
        ),
    )


def _redirect_status_text(status_code: int) -> str:
    if status_code == 301:
        return "Moved Permanently"
    if status_code == 308:
        return "Permanent Redirect"
    if status_code == 307:
        return "Temporary Redirect"
    return "Found"


def _format_file_size(size: int) -> str:
    units = ("B", "KB", "MB", "GB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


class WebFetchTool(BaseTool):
    backend_type = BackendType.WEB
    _name = WEB_FETCH_TOOL_NAME
    should_defer = True
    search_hint = "fetch and extract content from a URL"
    max_result_size_chars = 100_000
    _is_read_only = True
    _is_concurrency_safe = True
    _description = TOOL_PROMPT
    parameter_descriptions = {
        "url": "The URL to fetch content from",
        "prompt": "The prompt to run on the fetched content",
    }

    def __init__(
        self,
        *,
        llm_client: LLMClient | None = None,
        summarize_model: str | None = None,
        max_content_length: int = MAX_MARKDOWN_LENGTH,
        request_timeout: int = FETCH_TIMEOUT_SECONDS,
        user_agent: str = "OpenSpace WebFetch",
        preapproved_domains: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        super().__init__()
        self._llm_client = llm_client
        self._summarize_model = summarize_model or None
        self._max_content_length = max_content_length
        self._request_timeout = request_timeout
        self._user_agent = user_agent
        self._preapproved_domains = tuple(preapproved_domains or ())
        self._current_context: Any | None = None

    def get_prompt(self, context: Any = None) -> str:
        return TOOL_PROMPT

    def set_context(self, context: Any) -> "WebFetchTool":
        self._current_context = context
        return self

    async def validate_input(self, input: dict[str, Any], context: Any = None) -> str | None:
        url = str(input.get("url", ""))
        if not validate_url(url):
            return f'Error: Invalid URL "{url}". The URL provided could not be parsed.'
        return None

    async def check_permissions(self, input: dict[str, Any], context: Any = None):
        rule_content = web_fetch_tool_input_to_permission_rule_content(input)
        permission_context = getattr(context, "permission_context", None)
        if permission_context is None:
            return PermissionDeny(
                message=(
                    f"{WEB_FETCH_TOOL_NAME} cannot run because the tool runtime "
                    "is missing permission context."
                ),
                decision_reason=DecisionReasonOther(reason="missing permission context"),
            )

        deny_rule = _find_rule_by_content(permission_context, "deny", rule_content)
        if deny_rule is not None:
            return PermissionDeny(
                message=f"{WEB_FETCH_TOOL_NAME} denied access to {rule_content}.",
                decision_reason=DecisionReasonRule(rule=deny_rule),
            )

        ask_rule = _find_rule_by_content(permission_context, "ask", rule_content)
        if ask_rule is not None:
            return PermissionAsk(
                message=(
                    f"The assistant requested permissions to use {WEB_FETCH_TOOL_NAME}, "
                    "but you haven't granted it yet."
                ),
                decision_reason=DecisionReasonRule(rule=ask_rule),
                suggestions=build_suggestions(rule_content),
            )

        allow_rule = _find_rule_by_content(permission_context, "allow", rule_content)
        if allow_rule is not None:
            return PermissionAllow(
                updated_input=input,
                decision_reason=DecisionReasonRule(rule=allow_rule),
            )

        try:
            parsed = urlparse(str(input.get("url", "")))
            if parsed.hostname and self._is_preapproved_host(parsed.hostname, parsed.path or "/"):
                return PermissionAllow(
                    updated_input=input,
                    decision_reason=DecisionReasonOther(reason="Preapproved host"),
                )
        except Exception:
            pass

        return PermissionAsk(
            message=(
                f"The assistant requested permissions to use {WEB_FETCH_TOOL_NAME}, "
                "but you haven't granted it yet."
            ),
            suggestions=build_suggestions(rule_content),
        )

    async def _arun(self, url: str, prompt: str) -> ToolResult:
        start = time.time()
        context = self._current_context
        abort_event = getattr(context, "abort_event", None)
        response = await get_url_markdown_content(
            url,
            abort_event,
            request_timeout=self._request_timeout,
            user_agent=self._user_agent,
        )

        if isinstance(response, RedirectInfo):
            status_text = _redirect_status_text(response.status_code)
            message = (
                "REDIRECT DETECTED: The URL redirects to a different host.\n\n"
                f"Original URL: {response.original_url}\n"
                f"Redirect URL: {response.redirect_url}\n"
                f"Status: {response.status_code} {status_text}\n\n"
                "To complete your request, I need to fetch content from the redirected URL. "
                f"Please use {WEB_FETCH_TOOL_NAME} again with these parameters:\n"
                f'- url: "{response.redirect_url}"\n'
                f'- prompt: "{prompt}"'
            )
            return self._success_result(
                result=message,
                url=url,
                code=response.status_code,
                code_text=status_text,
                bytes_=len(message.encode("utf-8")),
                duration_ms=(time.time() - start) * 1000,
                content_type="text/plain",
            )

        is_preapproved = self._is_preapproved_url(url)
        if (
            is_preapproved
            and "text/markdown" in response.content_type
            and len(response.content) < self._max_content_length
        ):
            result = response.content
        else:
            model = (
                getattr(context, "web_fetch_model", None)
                if context is not None
                else None
            ) or self._summarize_model
            result = await apply_prompt_to_markdown(
                prompt,
                response.content,
                abort_event,
                is_non_interactive_session=bool(getattr(context, "is_async_agent", False)),
                is_preapproved_domain=is_preapproved,
                llm_client=self._llm_client or getattr(context, "llm_client", None),
                model=model,
                max_markdown_length=self._max_content_length,
            )

        if response.persisted_path:
            result += (
                f"\n\n[Binary content ({response.content_type}, "
                f"{_format_file_size(response.persisted_size or response.bytes)}) "
                f"also saved to {response.persisted_path}]"
            )

        return self._success_result(
            result=result,
            url=url,
            code=response.code,
            code_text=response.code_text,
            bytes_=response.bytes,
            duration_ms=(time.time() - start) * 1000,
            content_type=response.content_type,
            persisted_path=response.persisted_path,
            persisted_size=response.persisted_size,
        )

    def _success_result(
        self,
        *,
        result: str,
        url: str,
        code: int,
        code_text: str,
        bytes_: int,
        duration_ms: float,
        content_type: str,
        persisted_path: str | None = None,
        persisted_size: int | None = None,
    ) -> ToolResult:
        metadata = {
            "tool": self.name,
            "result": result,
            "url": url,
            "code": code,
            "code_text": code_text,
            "codeText": code_text,
            "bytes": bytes_,
            "duration_ms": duration_ms,
            "durationMs": duration_ms,
            "content_type": content_type,
        }
        if persisted_path:
            metadata["persisted_path"] = persisted_path
            metadata["persisted_size"] = persisted_size
        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=result,
            metadata=metadata,
        )

    def _is_preapproved_url(self, url: str) -> bool:
        try:
            parsed = urlparse(url)
            return bool(parsed.hostname) and self._is_preapproved_host(
                parsed.hostname,
                parsed.path or "/",
            )
        except Exception:
            return False

    def _is_preapproved_host(self, hostname: str, pathname: str) -> bool:
        if is_preapproved_host(hostname, pathname):
            return True
        hostname = hostname.lower()
        pathname = pathname or "/"
        for entry in self._preapproved_domains:
            host, _, prefix = entry.lower().partition("/")
            if hostname != host.removeprefix("www."):
                if hostname.removeprefix("www.") != host.removeprefix("www."):
                    continue
            if not prefix:
                return True
            path_prefix = "/" + prefix.strip("/")
            if pathname == path_prefix or pathname.startswith(path_prefix + "/"):
                return True
        return False


__all__ = [
    "WEB_FETCH_TOOL_ALIAS",
    "DESCRIPTION",
    "MAX_MARKDOWN_LENGTH",
    "PREAPPROVED_HOSTS",
    "WEB_FETCH_TOOL_NAME",
    "WebFetchTool",
    "apply_prompt_to_markdown",
    "build_suggestions",
    "clear_web_fetch_cache",
    "get_url_markdown_content",
    "get_with_permitted_redirects",
    "html_to_markdown",
    "is_permitted_redirect",
    "is_preapproved_host",
    "is_preapproved_url",
    "make_secondary_model_prompt",
    "validate_url",
    "web_fetch_tool_input_to_permission_rule_content",
]
