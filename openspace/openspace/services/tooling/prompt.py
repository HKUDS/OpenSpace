from __future__ import annotations

import copy
import hashlib
import inspect
import json
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from openspace.grounding.core.tool import BaseTool
from openspace.grounding.core.types import BackendType
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


DEFAULT_TOOL_SCHEMA_CACHE_SIZE = 512
ChatCompletionToolParam = dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolPromptContext:
    """Stable, provider-agnostic context used to render model-facing tool prompts."""

    cwd: str | None = None
    model: str | None = None
    capability_profile: str | None = None
    permission_mode: str | None = None
    backend_scope: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    all_tools: tuple[str, ...] = ()
    deferred_tools: tuple[str, ...] = ()
    discovered_tools: tuple[str, ...] = ()
    extra: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_runtime(
        cls,
        *,
        cwd: str | None = None,
        model: str | None = None,
        capability_profile: str | None = None,
        permission_context: Any | None = None,
        permission_mode: str | None = None,
        tools: Sequence[BaseTool] | None = None,
        all_tools: Sequence[BaseTool] | None = None,
        deferred_tools: Sequence[BaseTool] | Sequence[str] | None = None,
        discovered_tools: Iterable[str] | None = None,
        backend_scope: Iterable[str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> "ToolPromptContext":
        resolved_mode = (
            permission_mode
            or getattr(permission_context, "mode", None)
            or None
        )
        return cls(
            cwd=cwd,
            model=model,
            capability_profile=(
                str(capability_profile) if capability_profile is not None else None
            ),
            permission_mode=str(resolved_mode) if resolved_mode is not None else None,
            backend_scope=tuple(sorted(str(v) for v in (backend_scope or ()) if v)),
            tools=_stable_tool_names(tools or ()),
            all_tools=_stable_tool_names(all_tools or ()),
            deferred_tools=_stable_mixed_tool_names(deferred_tools or ()),
            discovered_tools=tuple(sorted(str(v) for v in (discovered_tools or ()) if v)),
            extra=tuple(
                sorted((str(k), _stable_json(v)) for k, v in (extra or {}).items())
            ),
        )

    @classmethod
    def from_context(
        cls,
        context: Any | None,
        *,
        tools: Sequence[BaseTool] | None = None,
        model: str | None = None,
    ) -> "ToolPromptContext":
        if isinstance(context, ToolPromptContext):
            return context
        if context is None:
            return cls.from_runtime(tools=tools, model=model)

        all_tools = getattr(context, "all_tools", None) or tools or ()
        deferred_names = getattr(context, "deferred_tool_names", None) or ()
        return cls.from_runtime(
            cwd=getattr(context, "cwd", None),
            model=model or getattr(context, "model", None),
            capability_profile=getattr(context, "capability_profile", None),
            permission_context=getattr(context, "permission_context", None),
            permission_mode=getattr(context, "permission_mode", None),
            tools=tools or getattr(context, "tools", None) or (),
            all_tools=all_tools,
            deferred_tools=deferred_names,
            discovered_tools=getattr(context, "discovered_tool_names", None) or (),
            backend_scope=getattr(context, "backend_scope", None) or (),
        )

    def cache_fingerprint(self) -> str:
        return hashlib.sha256(_stable_json(self).encode("utf-8")).hexdigest()


@dataclass(slots=True)
class ToolSchemaCache:
    """Small LRU cache for rendered model-facing tool schemas."""

    max_size: int = DEFAULT_TOOL_SCHEMA_CACHE_SIZE
    _items: OrderedDict[str, ChatCompletionToolParam] = field(default_factory=OrderedDict)

    def get(self, key: str) -> ChatCompletionToolParam | None:
        value = self._items.get(key)
        if value is None:
            return None
        self._items.move_to_end(key)
        return copy.deepcopy(value)

    def set(self, key: str, value: ChatCompletionToolParam) -> None:
        self._items[key] = copy.deepcopy(value)
        self._items.move_to_end(key)
        while len(self._items) > self.max_size:
            self._items.popitem(last=False)

    def clear(self) -> None:
        self._items.clear()


_GLOBAL_TOOL_SCHEMA_CACHE = ToolSchemaCache()


def clear_tool_schema_cache() -> None:
    _GLOBAL_TOOL_SCHEMA_CACHE.clear()


async def tool_to_openai_schema(
    tool: BaseTool,
    *,
    llm_name: str | None = None,
    prompt_context: ToolPromptContext | Any | None = None,
    sanitize_schema,
    use_cache: bool = True,
) -> ChatCompletionToolParam:
    """Render a BaseTool into OpenAI-compatible tool schema.

    The model-facing description mirrors OpenSpace's ``tool.prompt(options)`` path:
    prefer ``tool.get_prompt(context)`` / ``tool.get_prompt()`` and fall back to
    the short ``ToolSchema.description`` when no prompt renderer exists.
    """

    context = ToolPromptContext.from_context(
        prompt_context,
        tools=getattr(prompt_context, "tools", None) if prompt_context is not None else None,
    )
    name = llm_name or tool.schema.name
    cache_key = _schema_cache_key(tool, name, context)
    started = time.perf_counter()
    if use_cache:
        cached = _GLOBAL_TOOL_SCHEMA_CACHE.get(cache_key)
        if cached is not None:
            _record_schema_cache_event(
                prompt_context,
                context=context,
                tool=tool,
                llm_name=name,
                cache_key=cache_key,
                cache_hit=True,
                render_duration_ms=(time.perf_counter() - started) * 1000.0,
            )
            return cached

    description = await render_tool_prompt(tool, context)
    function_def: dict[str, Any] = {
        "name": name,
        "description": _with_backend_label(tool, description),
    }
    if tool.schema.parameters:
        function_def["parameters"] = sanitize_schema(tool.schema.parameters)
    else:
        function_def["parameters"] = {"type": "object", "properties": {}, "required": []}

    result: ChatCompletionToolParam = {
        "type": "function",
        "function": function_def,
    }
    if use_cache:
        _GLOBAL_TOOL_SCHEMA_CACHE.set(cache_key, result)
    _record_schema_cache_event(
        prompt_context,
        context=context,
        tool=tool,
        llm_name=name,
        cache_key=cache_key,
        cache_hit=False,
        render_duration_ms=(time.perf_counter() - started) * 1000.0,
    )
    return copy.deepcopy(result)


async def render_tool_prompt(tool: BaseTool, context: ToolPromptContext | None = None) -> str:
    prompt_fn = getattr(tool, "get_prompt", None)
    if callable(prompt_fn):
        try:
            result = _call_prompt_renderer(prompt_fn, context)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, str) and result.strip():
                return result
        except Exception:
            logger.debug("Failed to render tool prompt for %s", tool.name, exc_info=True)
    return tool.schema.description or tool.description or ""


def _call_prompt_renderer(prompt_fn, context: ToolPromptContext | None):
    try:
        signature = inspect.signature(prompt_fn)
    except (TypeError, ValueError):
        return prompt_fn()
    positional = [
        p
        for p in signature.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    has_var_kwargs = any(
        p.kind == inspect.Parameter.VAR_KEYWORD
        for p in signature.parameters.values()
    )
    if has_var_kwargs or positional:
        return prompt_fn(context)
    return prompt_fn()


def _schema_cache_key(tool: BaseTool, llm_name: str, context: ToolPromptContext) -> str:
    payload = {
        "tool": tool.name,
        "llm_name": llm_name,
        "class": f"{tool.__class__.__module__}.{tool.__class__.__qualname__}",
        "description": tool.schema.description or "",
        "parameters": tool.schema.parameters or {},
        "backend": getattr(tool.schema.backend_type, "value", str(tool.schema.backend_type)),
        "prompt_context": context.cache_fingerprint(),
    }
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()


def _record_schema_cache_event(
    runtime_context: Any | None,
    *,
    context: ToolPromptContext,
    tool: BaseTool,
    llm_name: str,
    cache_key: str,
    cache_hit: bool,
    render_duration_ms: float,
) -> None:
    if runtime_context is None:
        return
    if getattr(runtime_context, "tool_schema_cache_telemetry_enabled", True) is False:
        return
    events = getattr(runtime_context, "tool_schema_cache_events", None)
    if not isinstance(events, list):
        return

    runtime_tools = getattr(runtime_context, "tools", None) or ()
    runtime_all_tools = getattr(runtime_context, "all_tools", None) or ()
    deferred_names = getattr(runtime_context, "deferred_tool_names", None) or ()
    discovered_names = getattr(runtime_context, "discovered_tool_names", None) or ()
    event = {
        "cache_hit": bool(cache_hit),
        "tool_name": tool.name,
        "llm_name": llm_name,
        "cache_key_fingerprint": cache_key,
        "prompt_context_fingerprint": context.cache_fingerprint(),
        "active_schema_count": len(runtime_tools) or len(context.tools),
        "all_tools_count": len(runtime_all_tools) or len(context.all_tools),
        "deferred_tools_count": len(deferred_names) or len(context.deferred_tools),
        "discovered_tools_count": len(discovered_names) or len(context.discovered_tools),
        "model": context.model,
        "backend": getattr(tool.schema.backend_type, "value", str(tool.schema.backend_type)),
        "backend_scope": list(context.backend_scope),
        "permission_mode": context.permission_mode,
        "profile": context.capability_profile,
        "render_duration_ms": max(0.0, float(render_duration_ms)),
    }
    events.append(event)


def _with_backend_label(tool: BaseTool, description: str) -> str:
    backend_type = getattr(tool.schema, "backend_type", None)
    if not backend_type or backend_type is BackendType.NOT_SET:
        return description
    labels = {
        "mcp": "MCP",
        "shell": "Shell",
        "gui": "GUI",
        "web": "Web",
        "meta": "Meta",
    }
    label = labels.get(backend_type.value, backend_type.value)
    return f"[{label}] {description}"


def _stable_tool_names(tools: Sequence[BaseTool]) -> tuple[str, ...]:
    return tuple(sorted(t.name for t in tools if getattr(t, "name", None)))


def _stable_mixed_tool_names(tools: Sequence[BaseTool] | Sequence[str]) -> tuple[str, ...]:
    names: list[str] = []
    for item in tools:
        if isinstance(item, str):
            names.append(item)
        else:
            name = getattr(item, "name", None)
            if name:
                names.append(str(name))
    return tuple(sorted(set(names)))


def _stable_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    except TypeError:
        return json.dumps(str(value), sort_keys=True)
