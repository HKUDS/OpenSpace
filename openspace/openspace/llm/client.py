import copy
import json
import asyncio
import inspect
import os
import re
import time
from types import SimpleNamespace
from typing import Any, Callable, List, Sequence, Union, Dict, Optional, Mapping
from openai.types.chat import ChatCompletionToolParam

from openspace.grounding.core.types import ToolSchema, ToolResult, ToolStatus
from openspace.grounding.core.tool import BaseTool
from openspace.services.conversation.messages import (
    build_assistant_api_error_message,
    build_system_api_error_message,
    normalize_messages_for_api,
)
from openspace.services.conversation.content_blocks import normalize_multimodal_messages_for_model
from openspace.services.tooling.prompt import (
    ToolPromptContext,
    tool_to_openai_schema,
)
# strip_meta removed in step 7.2 — normalize_messages_for_api supersedes it.

from .types import ModelResponse, TokenUsage, token_usage_from_dict
from .effort import (
    EffortConfig,
    EffortLevel,
    build_effort_request_params,
    get_effort_config,
)
from .thinking import (
    ThinkingConfig,
    build_thinking_request_params,
    get_model_max_output_tokens,
    get_thinking_config,
)
from openspace.utils.logging import Logger

from .errors import (
    CannotRetryError,
    FallbackTriggeredError,
    OverloadedError,
    RetryContext,
    FLOOR_OUTPUT_TOKENS,
    MAX_OVERLOADED_RETRIES,
    get_default_max_retries,
    classify_api_error,
    get_retry_after_ms,
    get_retry_delay,
    is_model_not_available_error as _is_model_not_available,
    is_overloaded_error as _is_overloaded,
    is_prompt_too_long_error as _is_prompt_too_long,
    parse_max_tokens_context_overflow_error,
    should_retry as _should_retry,
    to_model_not_available_error,
    to_prompt_too_long_error,
)

# .env loading is centralized in host_detection.resolver.load_runtime_env().
# CLI/MCP entrypoints call it before reading startup env vars, and the
# resolver helpers also call it defensively.

logger = Logger.get_logger(__name__)


def _usage_has_counts(usage: TokenUsage) -> bool:
    return any(
        int(getattr(usage, attr, 0) or 0) > 0
        for attr in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "reasoning_tokens",
        )
    ) or float(getattr(usage, "cost", 0.0) or 0.0) > 0


def _estimate_missing_usage(
    *,
    model: str,
    input_messages: Sequence[Mapping[str, Any]],
    output_text: str,
) -> TokenUsage:
    try:
        from openspace.services.conversation.compact import estimate_message_tokens

        input_tokens = int(estimate_message_tokens(input_messages, model=model))
        output_tokens = int(
            estimate_message_tokens(
                [{"role": "assistant", "content": output_text}],
                model=model,
            )
        )
    except Exception:
        input_tokens = max(1, len(json.dumps(list(input_messages), ensure_ascii=False).encode("utf-8")) // 4)
        output_tokens = max(1, len(output_text.encode("utf-8")) // 4)

    output_tokens = max(1 if output_text else 0, output_tokens)
    return TokenUsage(
        input_tokens=max(0, input_tokens),
        output_tokens=output_tokens,
        total_tokens=max(0, input_tokens) + output_tokens,
    )


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _is_official_deepseek_model(model: str) -> bool:
    normalized = str(model or "").strip().lower()
    return normalized.startswith("deepseek/") or normalized.startswith("deepseek-")


def _supports_required_tool_choice(
    model: str,
    *,
    deepseek_thinking_disabled: bool = False,
) -> bool:
    """Return whether the provider accepts API-level required tool choice."""

    if _env_bool("OPENSPACE_ALLOW_DEEPSEEK_REQUIRED_TOOL_CHOICE", False):
        return True
    if _is_official_deepseek_model(model) and not deepseek_thinking_disabled:
        return False
    return True


def _deepseek_thinking_mode_from_env() -> str | None:
    raw = (
        os.environ.get("OPENSPACE_DEEPSEEK_THINKING")
        or os.environ.get("OPENSPACE_DEEPSEEK_THINKING_MODE")
    )
    if raw is None:
        return None
    normalized = raw.strip().lower().replace("_", "-")
    aliases = {
        "": None,
        "auto": None,
        "default": None,
        "enabled": "enabled",
        "enable": "enabled",
        "on": "enabled",
        "thinking": "enabled",
        "disabled": "disabled",
        "disable": "disabled",
        "off": "disabled",
        "non-thinking": "disabled",
        "nonthinking": "disabled",
    }
    if normalized not in aliases:
        logger.warning(
            "Ignoring invalid OPENSPACE_DEEPSEEK_THINKING value: %s",
            raw,
        )
    return aliases.get(normalized)


def _deepseek_request_params(model: str, *, tool_choice: str) -> dict[str, Any]:
    """Build DeepSeek-specific request controls when explicitly configured."""

    if not _is_official_deepseek_model(model):
        return {}

    mode = _deepseek_thinking_mode_from_env()
    if (
        mode is None
        and tool_choice == "required"
        and _env_bool(
            "OPENSPACE_DEEPSEEK_DISABLE_THINKING_ON_REQUIRED_TOOL_CHOICE",
            False,
        )
    ):
        mode = "disabled"

    params: dict[str, Any] = {}
    if mode in {"enabled", "disabled"}:
        params["extra_body"] = {"thinking": {"type": mode}}

    effort = os.environ.get("OPENSPACE_DEEPSEEK_REASONING_EFFORT")
    if effort and effort.strip() and mode != "disabled":
        params["reasoning_effort"] = effort.strip().lower()
    return params


def _deepseek_thinking_disabled(params: Mapping[str, Any]) -> bool:
    extra_body = params.get("extra_body")
    if not isinstance(extra_body, Mapping):
        return False
    thinking = extra_body.get("thinking")
    return (
        isinstance(thinking, Mapping)
        and str(thinking.get("type") or "").strip().lower() == "disabled"
    )


def _disable_reasoning_on_required_tool_choice(model: str) -> bool:
    """Return whether required-tool recovery calls should suppress reasoning."""

    if _env_bool("OPENSPACE_DISABLE_REASONING_ON_REQUIRED_TOOL_CHOICE", False):
        return True
    normalized = str(model or "").strip().lower()
    if normalized.startswith("openrouter/"):
        return _env_bool(
            "OPENSPACE_OPENROUTER_DISABLE_REASONING_ON_REQUIRED_TOOL_CHOICE",
            False,
        )
    return False


def _required_tool_choice_reasoning_disabled_params(model: str) -> dict[str, Any]:
    """Provider-specific params that actively disable reasoning for tool recovery."""

    normalized = str(model or "").strip().lower()
    if normalized.startswith("openrouter/"):
        return {"reasoning": {"effort": "none", "exclude": True}}
    return {}


def _decode_jsonish_string(value: str) -> str:
    """Decode the small escape subset models commonly use in malformed JSON."""

    return (
        value.replace("\\r\\n", "\n")
        .replace("\\n", "\n")
        .replace("\\r", "\r")
        .replace("\\t", "\t")
        .replace('\\"', '"')
        .replace("\\/", "/")
        .replace("\\\\", "\\")
    )


def _extract_jsonish_string_field(
    raw: str,
    field_names: Sequence[str],
    *,
    greedy: bool = False,
) -> str | None:
    """Extract a JSON string field from malformed model-emitted arguments."""

    for field_name in field_names:
        match = re.search(rf'"{re.escape(field_name)}"\s*:\s*"', raw)
        if not match:
            continue
        start = match.end()
        if greedy:
            end = raw.rfind('"')
            if end > start:
                return _decode_jsonish_string(raw[start:end])
            return None

        index = start
        escaped = False
        while index < len(raw):
            char = raw[index]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                return _decode_jsonish_string(raw[start:index])
            index += 1
    return None


def _salvage_malformed_write_arguments(
    raw_arguments: str,
    tool_name: str,
) -> dict[str, Any] | None:
    """Recover common huge ``write`` calls whose JSON string was not escaped."""

    if tool_name != "write":
        return None
    file_path = _extract_jsonish_string_field(
        raw_arguments,
        ("file_path", "path", "filename"),
    )
    content = _extract_jsonish_string_field(
        raw_arguments,
        ("content",),
        greedy=True,
    )
    if not file_path or content is None:
        return None
    return {"file_path": file_path, "content": content}


def _text_tool_call(
    *,
    name: str,
    arguments: Mapping[str, Any],
    index: int = 0,
) -> Dict[str, Any]:
    return {
        "id": f"call_text_{index}",
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(dict(arguments), ensure_ascii=False),
        },
    }


def _parse_text_tool_calls(
    content: str,
    tool_map: Mapping[str, BaseTool],
) -> list[Dict[str, Any]]:
    if not _env_bool("OPENSPACE_PARSE_TEXT_TOOL_CALLS") or not content:
        return []

    function_match = re.search(
        r"<function=([A-Za-z0-9_.-]+)>(.*?)</function>",
        content,
        flags=re.DOTALL,
    )
    if function_match:
        tool_name = function_match.group(1).strip()
        body = function_match.group(2)
        if tool_name in tool_map:
            arguments: dict[str, Any] = {}
            for param_match in re.finditer(
                r"<parameter=([A-Za-z0-9_.-]+)>(.*?)</parameter>",
                body,
                flags=re.DOTALL,
            ):
                arguments[param_match.group(1).strip()] = param_match.group(2).strip()
            if arguments:
                return [_text_tool_call(name=tool_name, arguments=arguments)]

    json_blocks = re.findall(
        r"```(?:json)?\s*(\{.*?\})\s*```",
        content,
        flags=re.DOTALL | re.IGNORECASE,
    )
    stripped = content.strip()
    if not json_blocks and stripped.startswith("{") and stripped.endswith("}"):
        json_blocks = [stripped]

    for block in json_blocks:
        try:
            payload = json.loads(block)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        command = payload.get("command")
        if isinstance(command, str) and command.strip() and "bash" in tool_map:
            arguments = {"command": command.strip()}
            description = payload.get("description")
            if isinstance(description, str) and description.strip():
                arguments["description"] = description.strip()
            return [_text_tool_call(name="bash", arguments=arguments)]
        name = payload.get("name") or payload.get("tool")
        arguments = payload.get("arguments") or payload.get("parameters")
        if isinstance(name, str) and name in tool_map and isinstance(arguments, dict):
            return [_text_tool_call(name=name, arguments=arguments)]

    return []


def _merge_completion_kwargs(
    target: Dict[str, Any],
    updates: Mapping[str, Any],
) -> None:
    """Merge LiteLLM kwargs, preserving nested ``extra_body`` values."""

    for key, value in updates.items():
        if (
            key == "extra_body"
            and isinstance(value, Mapping)
            and isinstance(target.get("extra_body"), Mapping)
        ):
            merged = dict(target["extra_body"])
            for nested_key, nested_value in value.items():
                if (
                    isinstance(nested_value, Mapping)
                    and isinstance(merged.get(nested_key), Mapping)
                ):
                    merged[nested_key] = {
                        **dict(merged[nested_key]),
                        **dict(nested_value),
                    }
                else:
                    merged[nested_key] = nested_value
            target[key] = merged
        else:
            target[key] = value


def _ensure_stream_usage_options(target: Dict[str, Any]) -> None:
    """Ask OpenAI-compatible streaming providers to include final usage stats."""

    stream_options = target.get("stream_options")
    if stream_options is None:
        target["stream_options"] = {"include_usage": True}
        return
    if isinstance(stream_options, Mapping):
        options = dict(stream_options)
        options.setdefault("include_usage", True)
        target["stream_options"] = options


def _serializable_effort_value(value: Any) -> str | int | None:
    if isinstance(value, EffortLevel):
        return value.value
    if isinstance(value, int):
        return value
    if value is None:
        return None
    return str(value)


def _find_tool_for_api_normalization(
    tool_map: Mapping[str, BaseTool],
    name: str,
) -> BaseTool | None:
    tool = tool_map.get(name)
    if tool is not None:
        return tool
    for candidate in tool_map.values():
        aliases = getattr(candidate, "aliases", ()) or ()
        if getattr(candidate, "name", None) == name or name in aliases:
            return candidate
    return None


def _normalize_tool_inputs_for_api(
    messages: Sequence[Mapping[str, Any]],
    tool_map: Mapping[str, BaseTool],
) -> list[dict[str, Any]]:
    if not tool_map:
        return [dict(message) for message in messages]

    from openspace.tool_runtime.pipeline.execution import normalize_tool_input_for_api

    normalized_messages: list[dict[str, Any]] = []
    for message in messages:
        copied_message = dict(message)
        tool_calls = copied_message.get("tool_calls")
        if copied_message.get("role") != "assistant" or not isinstance(tool_calls, list):
            normalized_messages.append(copied_message)
            continue

        normalized_tool_calls: list[Any] = []
        for tool_call in tool_calls:
            if not isinstance(tool_call, Mapping):
                normalized_tool_calls.append(tool_call)
                continue

            function = tool_call.get("function")
            if not isinstance(function, Mapping):
                normalized_tool_calls.append(dict(tool_call))
                continue

            tool_name = str(function.get("name") or "")
            tool = _find_tool_for_api_normalization(tool_map, tool_name)
            if tool is None:
                normalized_tool_calls.append(dict(tool_call))
                continue

            raw_arguments = function.get("arguments")
            arguments_were_string = isinstance(raw_arguments, str)
            if arguments_were_string:
                try:
                    parsed_arguments = json.loads(raw_arguments or "{}")
                except (TypeError, json.JSONDecodeError):
                    normalized_tool_calls.append(dict(tool_call))
                    continue
            else:
                parsed_arguments = raw_arguments

            if not isinstance(parsed_arguments, dict):
                normalized_tool_calls.append(dict(tool_call))
                continue

            normalized_arguments = normalize_tool_input_for_api(tool, parsed_arguments)
            copied_function = dict(function)
            copied_function["arguments"] = (
                json.dumps(normalized_arguments, ensure_ascii=False)
                if arguments_were_string
                else normalized_arguments
            )
            normalized_tool_calls.append({
                **dict(tool_call),
                "function": copied_function,
            })

        copied_message["tool_calls"] = normalized_tool_calls
        normalized_messages.append(copied_message)
    return normalized_messages

_litellm_module: Any | None = None


def _get_litellm() -> Any:
    global _litellm_module
    if _litellm_module is None:
        import litellm as imported_litellm

        # Disable LiteLLM verbose logging to prevent stdout blocking with large
        # tool schemas. Do this lazily so importing LLMClient does not trigger
        # LiteLLM/httpx proxy initialization in tests that never call a model.
        imported_litellm.set_verbose = False
        imported_litellm.suppress_debug_info = True
        _litellm_module = imported_litellm
    return _litellm_module


class _LiteLLMProxy:
    def __getattr__(self, name: str) -> Any:
        return getattr(_get_litellm(), name)


litellm = _LiteLLMProxy()


def _last_user_text(messages: Sequence[Mapping[str, Any]]) -> str | None:
    for msg in reversed(list(messages)):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, Mapping):
                    text = block.get("text") or block.get("content")
                    if isinstance(text, str):
                        parts.append(text)
                elif isinstance(block, str):
                    parts.append(block)
            return "\n".join(parts) if parts else None
    return None


def _sanitize_schema(params: Dict) -> Dict:
    """Sanitize tool parameter schema to comply with Claude API requirements."""
    if not params:
        return {"type": "object", "properties": {}, "required": []}
    
    # Deep copy to avoid modifying the original
    import copy
    sanitized = copy.deepcopy(params)
    
    # Anthropic API requires top-level type to be 'object'
    # If it's not an object, wrap the schema as a property of an object
    top_level_type = sanitized.get("type")
    if top_level_type and top_level_type != "object":
        # Wrap non-object schema as a single property called "value"
        logger.debug(f"[SCHEMA_SANITIZE] Wrapping non-object schema (type={top_level_type}) into object")
        wrapped = {
            "type": "object",
            "properties": {
                "value": sanitized  # The original schema becomes a property
            },
            "required": ["value"]  # Make it required
        }
        sanitized = wrapped
    
    # If type is object but missing properties/required, add them
    if sanitized.get("type") == "object":
        if "properties" not in sanitized:
            sanitized["properties"] = {}
        if "required" not in sanitized:
            sanitized["required"] = []
    
    # Remove non-standard fields that may cause issues (like 'title')
    sanitized.pop("title", None)
    
    # Recursively sanitize nested properties
    if "properties" in sanitized and isinstance(sanitized["properties"], dict):
        for prop_name, prop_schema in list(sanitized["properties"].items()):
            if isinstance(prop_schema, dict):
                # Remove title from nested properties
                prop_schema.pop("title", None)
    
    return sanitized


def _schema_to_openai(schema: ToolSchema) -> ChatCompletionToolParam:
    """Convert ToolSchema to OpenAI ChatCompletion tool format"""
    function_def = {
        "name": schema.name,
        "description": schema.description or "",
    }
    
    # Sanitize and add parameters
    if schema.parameters:
        sanitized = _sanitize_schema(schema.parameters)
        function_def["parameters"] = sanitized
        # Debug: verify sanitization worked
        if "title" in schema.parameters and "title" not in sanitized:
            logger.debug(f"Sanitized tool '{schema.name}': removed title")
    else:
        # Claude requires parameters field even if empty
        function_def["parameters"] = {"type": "object", "properties": {}, "required": []}
    
    return { 
        "type": "function",
        "function": function_def
    }
       
async def _prepare_tools_for_llmclient(
    tools: List[BaseTool] | None,
    fmt: str = "openai",
    *,
    prompt_context: ToolPromptContext | Any | None = None,
    use_schema_cache: bool = True,
) -> tuple[Sequence[Union[ToolSchema, ChatCompletionToolParam]], Dict[str, BaseTool]]:
    """Convert BaseTool list to LLMClient usable format, with deduplication.
    
    Args:
        tools: BaseTool instance list (should be obtained from GroundingClient and bound to runtime_info)
                if None or empty list, return empty list
        fmt: output format, "openai" for OpenAI format
    """
    if not tools:
        return [], {}
    
    if fmt == "openai":
        result = []
        tool_map = {}  # llm_name -> BaseTool
        name_count = {}
        
        for tool in tools:
            name = tool.schema.name
            name_count[name] = name_count.get(name, 0) + 1
        

        seen_names = set()
        for tool in tools:
            original_name = tool.schema.name
            
            if name_count[original_name] > 1:
                server_name = "unknown"
                if tool.is_bound and tool.runtime_info and tool.runtime_info.server_name:
                    server_name = tool.runtime_info.server_name
                llm_name = f"{server_name}__{original_name}"
            else:
                llm_name = original_name
            
            if llm_name in seen_names:
                logger.warning(f"[TOOL_DEDUP] Skipping duplicate tool: {llm_name}")
                continue
            seen_names.add(llm_name)
            
            tool_param = await tool_to_openai_schema(
                tool,
                llm_name=llm_name,
                prompt_context=prompt_context,
                sanitize_schema=_sanitize_schema,
                use_cache=use_schema_cache,
            )

            result.append(tool_param)
            
            tool_map[llm_name] = tool
            
            if llm_name != original_name:
                logger.info(f"[TOOL_RENAME] {original_name} -> {llm_name}")
        
        logger.info(f"[SCHEMA_SANITIZE] Prepared {len(result)} tools for LLM (from {len(tools)} total)")
        return result, tool_map
    
    tool_map = {tool.schema.name: tool for tool in tools}
    return [tool.schema for tool in tools], tool_map



# ═══════════════════════════════════════════════════════════════════════
# Legacy helper functions removed in step 7.2.
#
# The following module-level functions were deleted because they are
# superseded by the new tool execution pipeline (steps 5.1 / 5.2):
#
#   _infer_backend_from_tool_name   → removed with the old LLM+tool wrapper
#   _resolve_tool_call_target       → find_tool_by_name() in tool_execution.py
#   _summarize_tool_result          → OpenSpace uses persist-to-disk, not LLM summary
#   _tool_result_to_message_async   → pipeline step 7 (build_tool_result_message)
#   _execute_tool_call              → run_tool_use() in tool_execution.py
#
# Old callers should use:
#   - call_model() for pure LLM calls
#   - run_tool_use() / run_tools() for tool execution
#   - enforce_tool_result_budget() for large result handling
# ═══════════════════════════════════════════════════════════════════════


class LLMClient:
    """LLMClient class for single round call"""
    def __init__(
        self,
        model: str = "openrouter/anthropic/claude-sonnet-4.5",
        enable_thinking: bool = False,
        rate_limit_delay: float = 0.0,
        max_retries: int | None = None,
        retry_delay: float = 1.0,
        timeout: float = 120.0,
        fallback_model: Optional[str] = None,
        **litellm_kwargs,
    ):
        """
        Args:
            model: LLM model identifier.
            enable_thinking: Whether to enable extended thinking mode.
            rate_limit_delay: Minimum delay between API calls (0 = no delay).
            max_retries: Maximum retry attempts on rate-limit / transient errors.
            retry_delay: Initial retry delay in seconds (exponential backoff).
            timeout: Request timeout in seconds (default: 120 s).
            fallback_model: Consecutive-overloaded fallback model (``None`` = no fallback).
            **litellm_kwargs: Additional litellm parameters.
        """
        self.model = model
        self.enable_thinking = enable_thinking
        self.rate_limit_delay = rate_limit_delay
        self.max_retries = max_retries if max_retries is not None else get_default_max_retries()
        self.retry_delay = retry_delay
        self.timeout = timeout
        self.fallback_model = fallback_model
        self.litellm_kwargs = litellm_kwargs
        self._logger = Logger.get_logger(__name__)
        self._last_call_time = 0.0
        self._event_callback: Optional[Callable[..., Any]] = None
        self._usage_callback: Optional[Callable[[str, TokenUsage], Any]] = None
    
    def set_event_callback(self, callback: Optional[Callable[..., Any]]) -> None:
        """Set callback for streaming events. Signature: async callback(event_type: str, data: dict)"""
        self._event_callback = callback

    def set_usage_callback(
        self,
        callback: Optional[Callable[[str, TokenUsage], Any]],
    ) -> None:
        """Set callback invoked after each model usage update.

        Signature: ``callback(model, usage: TokenUsage)``.
        """
        self._usage_callback = callback

    async def _emit_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """Emit an event via callback if set. Failures are silently swallowed."""
        if self._event_callback is None:
            return
        try:
            await self._event_callback(event_type, data)
        except Exception:
            pass

    async def _invoke_callback(self, callback: Optional[Callable[..., Any]], *args, **kwargs) -> Any:
        """Invoke a sync or async callback and return its result."""
        if callback is None:
            return None
        result = callback(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    async def _invoke_usage_callback(self, model: str, usage: TokenUsage) -> None:
        if self._usage_callback is None:
            return
        await self._invoke_callback(self._usage_callback, model, usage)

    async def _emit_text_tokens(self, text: str, chunk_size: int = 64) -> None:
        """Emit coarse-grained text chunks when true provider token streaming is unavailable."""
        if not text:
            return
        for idx in range(0, len(text), chunk_size):
            await self._emit_event("llm_token", {"token": text[idx: idx + chunk_size]})

    @staticmethod
    def _response_field(value: Any, name: str, default: Any = None) -> Any:
        if isinstance(value, Mapping):
            return value.get(name, default)
        return getattr(value, name, default)

    @staticmethod
    def _stringify_stream_tool_arguments(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, (Mapping, list, tuple)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    @classmethod
    def _merge_stream_tool_call_deltas(
        cls,
        tool_calls_by_index: dict[int, dict[str, Any]],
        delta_tool_calls: Any,
    ) -> None:
        if not delta_tool_calls:
            return

        for fallback_index, delta_tool_call in enumerate(delta_tool_calls):
            raw_index = cls._response_field(
                delta_tool_call, "index", fallback_index,
            )
            try:
                index = int(raw_index)
            except (TypeError, ValueError):
                index = fallback_index

            accumulated = tool_calls_by_index.setdefault(
                index,
                {
                    "id": "",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                },
            )

            tool_call_id = cls._response_field(delta_tool_call, "id", None)
            if tool_call_id:
                accumulated["id"] = str(tool_call_id)

            tool_call_type = cls._response_field(delta_tool_call, "type", None)
            if tool_call_type:
                accumulated["type"] = str(tool_call_type)

            function_delta = cls._response_field(delta_tool_call, "function", None)
            if not function_delta:
                continue

            name_delta = cls._response_field(function_delta, "name", None)
            if name_delta:
                accumulated["function"]["name"] = str(name_delta)

            arguments_delta = cls._response_field(function_delta, "arguments", None)
            if arguments_delta:
                accumulated["function"]["arguments"] += (
                    cls._stringify_stream_tool_arguments(arguments_delta)
                )

    @classmethod
    def _stream_tool_call_objects(
        cls,
        tool_calls_by_index: Mapping[int, Mapping[str, Any]],
    ) -> list[Any]:
        tool_calls: list[Any] = []
        for index in sorted(tool_calls_by_index):
            tool_call = tool_calls_by_index[index]
            function = tool_call.get("function") or {}
            tool_calls.append(
                SimpleNamespace(
                    id=str(tool_call.get("id") or f"call_{index}"),
                    type=str(tool_call.get("type") or "function"),
                    function=SimpleNamespace(
                        name=str(function.get("name") or ""),
                        arguments=str(function.get("arguments") or ""),
                    ),
                )
            )
        return tool_calls

    @classmethod
    def _stream_reasoning_delta(cls, delta: Any) -> Any:
        reasoning = (
            cls._response_field(delta, "reasoning_content", None)
            or cls._response_field(delta, "reasoning", None)
        )
        if reasoning:
            return reasoning

        provider_fields = cls._response_field(
            delta, "provider_specific_fields", None,
        )
        if provider_fields:
            return cls._response_field(provider_fields, "reasoning", None)
        return None

    async def _consume_streaming_completion(
        self,
        stream: Any,
        *,
        abort_event: Optional[asyncio.Event] = None,
        emit_events: bool = True,
    ) -> Any:
        content_parts: list[str] = []
        reasoning_parts: list[Any] = []
        tool_calls_by_index: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        raw_usage: Any = None
        response_model: str | None = None
        response_id: str | None = None
        saw_choice = False

        async for chunk in stream:
            if abort_event is not None and abort_event.is_set():
                raise asyncio.CancelledError("Request was aborted")

            response_model = response_model or self._response_field(
                chunk, "model", None,
            )
            response_id = response_id or self._response_field(chunk, "id", None)
            chunk_usage = self._response_field(chunk, "usage", None)
            if chunk_usage is not None:
                raw_usage = chunk_usage

            for choice in self._response_field(chunk, "choices", []) or []:
                saw_choice = True
                choice_finish_reason = self._response_field(
                    choice, "finish_reason", None,
                )
                if choice_finish_reason is not None:
                    finish_reason = choice_finish_reason

                delta = self._response_field(choice, "delta", None)
                if delta is None:
                    delta = self._response_field(choice, "message", None)
                if delta is None:
                    continue

                content_delta = self._response_field(delta, "content", None)
                if isinstance(content_delta, str) and content_delta:
                    content_parts.append(content_delta)
                    if emit_events:
                        await self._emit_event(
                            "llm_token",
                            {"token": content_delta},
                        )

                reasoning_delta = self._stream_reasoning_delta(delta)
                if reasoning_delta:
                    reasoning_parts.append(reasoning_delta)

                self._merge_stream_tool_call_deltas(
                    tool_calls_by_index,
                    self._response_field(delta, "tool_calls", None),
                )

        message = SimpleNamespace(
            content="".join(content_parts),
            tool_calls=self._stream_tool_call_objects(tool_calls_by_index) or None,
        )
        if reasoning_parts:
            setattr(message, "reasoning_content", "".join(map(str, reasoning_parts)))

        response = SimpleNamespace(
            id=response_id,
            model=response_model,
            choices=(
                [
                    SimpleNamespace(
                        message=message,
                        finish_reason=finish_reason,
                    )
                ]
                if saw_choice
                else []
            ),
            usage=raw_usage,
        )
        setattr(response, "_openspace_streamed", True)
        return response

    # Permission checks are handled by the run_tool_use() pipeline, which
    # resolves PermissionEngine decisions from the ToolUseContext. Legacy
    # permission_check PreToolUse adapters may still be registered explicitly by
    # external callers, but are not part of the default hook set.

    @staticmethod
    def _merge_consecutive_system_messages(messages: List[Dict]) -> List[Dict]:
        """Merge consecutive system messages into one.

        Providers like MiniMax reject requests that contain multiple consecutive
        messages with the same role (error 2013 "invalid chat setting").
        Merging is safe for all providers — it simply concatenates the content.
        """
        if not messages:
            return messages
        merged: List[Dict] = []
        for msg in messages:
            if (
                merged
                and msg.get("role") == "system"
                and merged[-1].get("role") == "system"
            ):
                merged[-1] = {
                    "role": "system",
                    "content": merged[-1].get("content", "") + "\n\n" + msg.get("content", ""),
                }
            else:
                merged.append(msg.copy())
        return merged

    @staticmethod
    def _is_minimax_model(model: str) -> bool:
        return isinstance(model, str) and "minimax" in model.lower()

    @classmethod
    def _rewrite_nonleading_system_messages_for_minimax(
        cls,
        messages: List[Dict],
    ) -> List[Dict]:
        """Rewrite non-leading system messages into internal user notes for MiniMax."""
        rewritten: List[Dict] = []
        rewritten_count = 0

        for msg in messages:
            msg_copy = msg.copy()
            if msg_copy.get("role") == "system" and rewritten:
                content = msg_copy.get("content", "")
                if isinstance(content, str):
                    msg_copy["content"] = (
                        "[INTERNAL ORCHESTRATION NOTE]\n"
                        "This note was originally injected as a system message by the "
                        "agent runtime. Treat it as workflow guidance, not as a new "
                        "end-user request.\n\n"
                        f"{content}"
                    )
                msg_copy["role"] = "user"
                rewritten_count += 1
            rewritten.append(msg_copy)

        if rewritten_count:
            logger.info(
                "Rewrote %d non-leading system message(s) for MiniMax compatibility",
                rewritten_count,
            )

        return rewritten

    @classmethod
    def _normalize_messages_for_model(cls, messages: List[Dict], model: str) -> List[Dict]:
        """Normalize message history only when a provider requires it."""
        normalized = normalize_multimodal_messages_for_model(messages, model)
        if not cls._is_minimax_model(model):
            return normalized

        minimized_system_history = cls._merge_consecutive_system_messages(normalized)
        return cls._rewrite_nonleading_system_messages_for_minimax(
            minimized_system_history
        )

    @staticmethod
    def _serialize_response_field(value):
        """Convert provider response fields into plain Python containers."""
        if hasattr(value, "model_dump"):
            return value.model_dump(exclude_none=True)
        if isinstance(value, list):
            return [LLMClient._serialize_response_field(item) for item in value]
        if isinstance(value, tuple):
            return [LLMClient._serialize_response_field(item) for item in value]
        if isinstance(value, dict):
            return {
                key: LLMClient._serialize_response_field(item)
                for key, item in value.items()
            }
        return value

    async def _rate_limit(self):
        """Apply rate limiting by adding delay between API calls"""
        if self.rate_limit_delay > 0:
            current_time = time.time()
            time_since_last_call = current_time - self._last_call_time
            
            if time_since_last_call < self.rate_limit_delay:
                sleep_time = self.rate_limit_delay - time_since_last_call
                self._logger.debug(f"Rate limiting: waiting {sleep_time:.2f}s before next API call")
                await asyncio.sleep(sleep_time)
            
            self._last_call_time = time.time()

    async def _call_with_retry(
        self,
        *,
        abort_event: Optional[asyncio.Event] = None,
        fallback_model: Optional[str] = None,
        thinking_budget_tokens: int = 0,
        emit_events: bool = True,
        **completion_kwargs,
    ):
        """Call LLM with OpenSpace retry logic."""
        request_model = completion_kwargs.get("model", self.model)
        retry_context = RetryContext(
            model=request_model,
            thinking_budget_tokens=max(0, int(thinking_budget_tokens or 0)),
        )
        consecutive_overloaded = 0
        last_error: BaseException | None = None
        max_retries = self.max_retries

        for attempt in range(1, max_retries + 2):  # 1..max_retries+1 inclusive
            if abort_event is not None and abort_event.is_set():
                raise asyncio.CancelledError("Request was aborted")

            try:
                response = await asyncio.wait_for(
                    litellm.acompletion(**completion_kwargs),
                    timeout=self.timeout,
                )
                if completion_kwargs.get("stream") and hasattr(response, "__aiter__"):
                    response = await asyncio.wait_for(
                        self._consume_streaming_completion(
                            response,
                            abort_event=abort_event,
                            emit_events=emit_events,
                        ),
                        timeout=self.timeout,
                    )
                return response

            except asyncio.CancelledError:
                raise  # never swallow cancellation

            except asyncio.TimeoutError as e:
                last_error = TimeoutError(
                    f"LLM call timed out after {self.timeout}s"
                )
                self._logger.error(
                    "LLM call timed out after %ss (attempt %d/%d)",
                    self.timeout, attempt, max_retries + 1,
                )
                # Timeout is retryable
                if attempt > max_retries:
                    raise CannotRetryError(last_error, retry_context) from e
                delay_ms = get_retry_delay(attempt)
                self._logger.info("Retrying after %d ms...", delay_ms)
                await asyncio.sleep(delay_ms / 1000)
                continue

            except Exception as e:
                last_error = e
                self._logger.debug(
                    "API error (attempt %d/%d): %s",
                    attempt, max_retries + 1, str(e)[:300],
                )

                # Non-retryable: prompt too long
                if _is_prompt_too_long(e):
                    raise to_prompt_too_long_error(
                        e, model=request_model,
                    ) from e

                # Non-retryable: model / auth not available
                if _is_model_not_available(e):
                    raise to_model_not_available_error(
                        e, model=request_model,
                    ) from e

                # Track consecutive overloaded
                if _is_overloaded(e):   
                    consecutive_overloaded += 1
                    if consecutive_overloaded >= MAX_OVERLOADED_RETRIES:
                        if fallback_model:
                            raise FallbackTriggeredError(
                                request_model, fallback_model,
                            ) from e
                        # No fallback → wrap as CannotRetryError
                        raise CannotRetryError(
                            OverloadedError(
                                str(e),
                                model=request_model,
                                original_error=e,
                            ),
                            retry_context,
                        ) from e
                else:
                    consecutive_overloaded = 0

                if attempt > max_retries:
                    raise CannotRetryError(e, retry_context) from e
                if not _should_retry(e):
                    raise CannotRetryError(e, retry_context) from e

                # max_tokens overflow
                overflow = parse_max_tokens_context_overflow_error(e)
                if overflow is not None:
                    input_tokens = overflow["input_tokens"]
                    context_limit = overflow["context_limit"]
                    safety_buffer = 1000
                    available = max(0, context_limit - input_tokens - safety_buffer)
                    if available < FLOOR_OUTPUT_TOKENS:
                        self._logger.error(
                            "Available context %d < FLOOR_OUTPUT_TOKENS %d",
                            available, FLOOR_OUTPUT_TOKENS,
                        )
                        raise CannotRetryError(e, retry_context) from e
                    min_required = retry_context.thinking_budget_tokens + 1
                    adjusted = max(FLOOR_OUTPUT_TOKENS, available, min_required)
                    retry_context.max_tokens_override = adjusted
                    self._logger.info(
                        "max_tokens overflow: adjusting to %d (input=%d, limit=%d)",
                        adjusted, input_tokens, context_limit,
                    )
                    if "max_tokens" in completion_kwargs:
                        completion_kwargs["max_tokens"] = adjusted
                    continue

                # Compute delay
                error_tag = classify_api_error(e)
                retry_after = get_retry_after_ms(e)
                if retry_after is not None:
                    delay_ms = retry_after
                else:
                    delay_ms = get_retry_delay(attempt)
                if error_tag == "rate_limit" and self.rate_limit_delay > 0:
                    delay_ms = max(delay_ms, int(self.rate_limit_delay * 1000))
                self._logger.warning(
                    "%s error (attempt %d/%d), waiting %d ms before retry...",
                    error_tag, attempt, max_retries + 1, delay_ms,
                )

                retry_system_msg = build_system_api_error_message(
                    error_msg=str(e)[:500],
                    retry_in_ms=delay_ms,
                    retry_attempt=attempt,
                    max_retries=max_retries,
                )
                if emit_events:
                    await self._emit_event("llm_retry", {
                        "attempt": attempt,
                        "max_retries": max_retries,
                        "delay_ms": delay_ms,
                        "error_type": error_tag,
                        "error_message": str(e)[:500],
                        "system_message": retry_system_msg,
                    })

                await asyncio.sleep(delay_ms / 1000)

        # Should not reach here, but safety net
        raise CannotRetryError(last_error, retry_context)
    
    
    @staticmethod
    def _safe_parse_tool_arguments(
        raw_arguments: str | None,
        tool_name: str,
    ) -> dict:
        """Parse tool call arguments JSON with fallback to empty dict."""
        if not raw_arguments:
            return {}
        try:
            parsed = json.loads(raw_arguments)
            if isinstance(parsed, dict):
                return parsed
            logger.warning(
                "[TOOL_JSON] Tool '%s' arguments parsed to %s, using {}",
                tool_name,
                type(parsed).__name__,
            )
            return {}
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning(
                "[TOOL_JSON] Failed to parse tool '%s' arguments "
                "(len=%d): %s. Falling back to {}.",
                tool_name,
                len(raw_arguments),
                str(exc)[:200],
            )
            return {}

    @staticmethod
    def _sanitize_tool_arguments_for_history(
        raw_arguments: str | None,
        tool_name: str,
    ) -> tuple[str, dict[str, Any], bool]:
        """Return API-safe tool arguments and whether the model JSON was invalid."""
        if not raw_arguments:
            return "{}", {}, False
        try:
            parsed = json.loads(raw_arguments)
            if isinstance(parsed, dict):
                return raw_arguments, parsed, False
            logger.warning(
                "[TOOL_JSON] Tool '%s' arguments parsed to %s; "
                "sanitizing history payload.",
                tool_name,
                type(parsed).__name__,
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning(
                "[TOOL_JSON] Failed to parse tool '%s' arguments "
                "(len=%d): %s. Sanitizing history payload.",
                tool_name,
                len(raw_arguments),
                str(exc)[:200],
            )

        salvaged = _salvage_malformed_write_arguments(raw_arguments, tool_name)
        if salvaged is not None:
            logger.warning(
                "[TOOL_JSON] Recovered malformed '%s' arguments "
                "(len=%d) for history/execution.",
                tool_name,
                len(raw_arguments),
            )
            return json.dumps(salvaged, ensure_ascii=False), salvaged, True

        fallback = {
            "__openspace_tool_argument_error": (
                "The model emitted malformed tool-call JSON. Retry this tool "
                "call with compact, valid JSON arguments."
            ),
            "raw_argument_chars": len(raw_arguments),
        }
        return json.dumps(fallback, ensure_ascii=False), fallback, True

    @staticmethod
    def _get_error_message_if_refusal(
        stop_reason: str | None,
        model: str,
    ) -> dict | None:
        """Build an error assistant message when the model refuses."""
        if stop_reason not in ("refusal", "content_filter"):
            return None
        content = (
            "API Error: The model is unable to respond to this request, "
            "which appears to violate the provider's Usage Policy. "
            "Try rephrasing the request or attempting a different approach."
        )
        return build_assistant_api_error_message(
            content, error_details=f"stop_reason={stop_reason}, model={model}"
        )

    @staticmethod
    def is_api_error_message(message: Dict[str, Any] | None) -> bool:
        if not isinstance(message, dict):
            return False
        meta = message.get("_meta")
        return isinstance(meta, dict) and bool(meta.get("is_api_error_message"))

    @classmethod
    def get_model_response_followup_messages(
        cls,
        model_response: ModelResponse,
    ) -> list[dict[str, Any]]:
        assistant_index = next(
            (
                index
                for index, message in enumerate(model_response.messages)
                if message is model_response.assistant_message
            ),
            -1,
        )
        if assistant_index < 0:
            assistant_role = model_response.assistant_message.get("role")
            assistant_content = model_response.assistant_message.get("content")
            assistant_tool_calls = model_response.assistant_message.get("tool_calls")
            for index in range(len(model_response.messages) - 1, -1, -1):
                message = model_response.messages[index]
                if not isinstance(message, dict):
                    continue
                if message.get("role") != assistant_role:
                    continue
                if message.get("content") != assistant_content:
                    continue
                if message.get("tool_calls") != assistant_tool_calls:
                    continue
                assistant_index = index
                break
        if assistant_index < 0:
            return []
        return [
            copy.deepcopy(message)
            for message in model_response.messages[assistant_index + 1 :]
            if isinstance(message, dict)
        ]

    @classmethod
    def model_response_has_api_error(
        cls,
        model_response: ModelResponse,
    ) -> bool:
        if cls.is_api_error_message(model_response.assistant_message):
            return True
        return any(
            cls.is_api_error_message(message)
            for message in cls.get_model_response_followup_messages(model_response)
        )

    async def call_model(
        self,
        messages: List[Dict],
        tools: List[BaseTool] | None = None,
        **kwargs,
    ) -> "ModelResponse":
        """Single-round LLM call — returns ModelResponse, never executes tools.

        Args:
            messages: Conversation history (OpenAI format dicts).
                ``_meta`` fields are stripped before sending to API.
            tools: Optional BaseTool list.  Schemas are converted to
                OpenAI format and returned in ``ModelResponse.tool_map``.
            **kwargs: Overrides forwarded to LiteLLM or used internally:
                - ``model``: override self.model for this call
                - ``abort_event``: ``asyncio.Event`` to cancel the call
                - ``fallback_model``: override self.fallback_model
                - ``enable_thinking``: override self.enable_thinking
                - ``thinking_config``: explicit ``ThinkingConfig`` or mapping
                - ``reasoning_effort``: provider effort/depth; ``None`` means auto
                - ``effort_config``: pre-resolved ``EffortConfig``
                - ``tool_choice``: tool selection mode (default ``"auto"``)
                - ``streaming``: enable provider token streaming (default ``True``)
                - ``emit_events``: stream LLM UI events (default ``True``)
                - Any other key is forwarded to ``litellm.acompletion``

        Returns:
            ``ModelResponse`` with ``assistant_message``, ``tool_calls``,
            ``tool_map``, ``stop_reason``, ``usage``, ``messages``.

        Raises:
            PromptTooLongError: Prompt exceeds context window (Layer 1).
            ModelNotAvailableError: Model/credentials unavailable (Layer 1).
            CannotRetryError: All retries exhausted (Layer 1).
            FallbackTriggeredError: Consecutive overloaded → switch model.
            ValueError: API returned empty response.
        """
        # Extract call_model-specific kwargs
        request_model: str = kwargs.pop("model", self.model)
        abort_event = kwargs.pop("abort_event", None)
        fallback_model: str | None = kwargs.pop(
            "fallback_model", self.fallback_model,
        )
        enable_thinking: bool = kwargs.pop(
            "enable_thinking", self.enable_thinking,
        )
        tool_choice: str = kwargs.pop("tool_choice", "auto")
        reasoning_effort: str | int | None = kwargs.pop("reasoning_effort", None)
        explicit_effort_config = kwargs.pop("effort_config", None)
        explicit_thinking_config = kwargs.pop("thinking_config", None)
        strip_thinking_keep_recent_override = kwargs.pop(
            "strip_thinking_keep_recent",
            None,
        )
        tool_prompt_context = kwargs.pop("tool_prompt_context", None)
        use_tool_schema_cache: bool = bool(kwargs.pop("use_tool_schema_cache", True))
        explicit_stream = kwargs.pop("stream", None)
        explicit_streaming = kwargs.pop("streaming", None)
        if explicit_streaming is None and "streaming" in self.litellm_kwargs:
            explicit_streaming = self.litellm_kwargs.get("streaming")
        if explicit_stream is None and "stream" in self.litellm_kwargs:
            explicit_stream = self.litellm_kwargs.get("stream")
        streaming = bool(
            explicit_streaming
            if explicit_streaming is not None
            else True if explicit_stream is None else explicit_stream
        )
        emit_events = bool(kwargs.pop("emit_events", True))
        raw_max_tokens = kwargs.pop("max_tokens", None)
        if raw_max_tokens is None:
            raw_max_tokens = self.litellm_kwargs.get("max_tokens")
        try:
            max_tokens = int(raw_max_tokens) if raw_max_tokens is not None else None
        except (TypeError, ValueError):
            max_tokens = None
        if max_tokens is None or max_tokens <= 0:
            max_tokens = get_model_max_output_tokens(request_model)

        # Prepare tools
        llm_tools: list = []
        tool_map: Dict[str, BaseTool] = {}
        if tools:
            llm_tools, tool_map = await _prepare_tools_for_llmclient(
                tools, fmt="openai",
                prompt_context=tool_prompt_context,
                use_schema_cache=use_tool_schema_cache,
            )

        disable_reasoning_for_required_tool = (
            bool(llm_tools)
            and tool_choice == "required"
            and _disable_reasoning_on_required_tool_choice(request_model)
        )
        if disable_reasoning_for_required_tool:
            enable_thinking = False
            explicit_effort_config = None
            explicit_thinking_config = ThinkingConfig.disabled(
                source="required_tool_choice"
            )

        if isinstance(explicit_effort_config, EffortConfig):
            effort_config = explicit_effort_config
        else:
            effort_config = get_effort_config(request_model, reasoning_effort)
        effective_effort = _serializable_effort_value(effort_config.applied_value)

        if (
            explicit_thinking_config is None
            and effort_config.thinking_budget_tokens is not None
        ):
            explicit_thinking_config = ThinkingConfig.enabled(
                effort_config.thinking_budget_tokens,
                source=effort_config.source,
            )

        thinking_config = get_thinking_config(
            request_model,
            effective_effort,
            user_request=_last_user_text(messages),
            max_output_tokens=max_tokens,
            enable_thinking=enable_thinking,
            explicit=explicit_thinking_config,
            has_tools=bool(llm_tools),
        )
        thinking_params, thinking_budget_tokens = build_thinking_request_params(
            thinking_config,
            request_model,
            effort=effective_effort,
            max_output_tokens=max_tokens,
        )
        effort_params = build_effort_request_params(effort_config, request_model)
        if disable_reasoning_for_required_tool:
            thinking_params = {}
            thinking_budget_tokens = 0
            effort_params = {}

        # Build litellm completion kwargs
        completion_kwargs: Dict[str, Any] = {
            "model": request_model,
            **self.litellm_kwargs,
            "max_tokens": max_tokens,
        }
        completion_kwargs.pop("streaming", None)
        if streaming:
            completion_kwargs["stream"] = True
        else:
            completion_kwargs.pop("stream", None)
        deepseek_params = _deepseek_request_params(
            request_model,
            tool_choice=tool_choice,
        )
        effective_tool_choice = tool_choice
        if (
            llm_tools
            and tool_choice == "required"
            and not _supports_required_tool_choice(
                request_model,
                deepseek_thinking_disabled=_deepseek_thinking_disabled(
                    deepseek_params
                ),
            )
        ):
            effective_tool_choice = "auto"
            logger.info(
                "Downgrading tool_choice=required to auto for model %s because "
                "the provider does not support required tool choice.",
                request_model,
            )
        if llm_tools:
            completion_kwargs["tools"] = llm_tools
            completion_kwargs["tool_choice"] = effective_tool_choice
        logger.info(
            "LLM request controls: model=%s tools=%s tool_choice=%s requested_tool_choice=%s "
            "disable_reasoning_for_required_tool=%s max_tokens=%s",
            request_model,
            len(llm_tools),
            effective_tool_choice if llm_tools else "none",
            tool_choice,
            disable_reasoning_for_required_tool,
            max_tokens,
        )
        _merge_completion_kwargs(completion_kwargs, thinking_params)
        _merge_completion_kwargs(completion_kwargs, effort_params)
        _merge_completion_kwargs(completion_kwargs, deepseek_params)

        # Forward remaining kwargs (e.g. max_tokens, temperature)
        _merge_completion_kwargs(completion_kwargs, kwargs)
        if streaming:
            _ensure_stream_usage_options(completion_kwargs)
        if disable_reasoning_for_required_tool:
            completion_kwargs.pop("reasoning", None)
            completion_kwargs.pop("reasoning_effort", None)
            completion_kwargs.pop("thinking", None)
            _merge_completion_kwargs(
                completion_kwargs,
                _required_tool_choice_reasoning_disabled_params(request_model),
            )
        if "thinking" in thinking_params:
            # Anthropic requires temperature=1/default whenever thinking is enabled.
            completion_kwargs.pop("temperature", None)

        # Normalize messages
        # strip_meta → drop compact boundaries → ensure tool_result
        # pairing → merge consecutive same-role → model-specific fixups.
        strip_thinking_keep_recent = (
            int(strip_thinking_keep_recent_override)
            if strip_thinking_keep_recent_override is not None
            else (0 if thinking_config.type == "disabled" else 1)
        )
        api_messages = normalize_messages_for_api(
            messages,
            strip_thinking_keep_recent=strip_thinking_keep_recent,
        )
        api_messages = _normalize_tool_inputs_for_api(api_messages, tool_map)
        api_messages = self._normalize_messages_for_model(
            api_messages, request_model,
        )
        completion_kwargs["messages"] = api_messages

        # Rate limit
        await self._rate_limit()

        # Emit llm_start event
        if emit_events:
            await self._emit_event("llm_start", {
                "model": request_model,
                "tool_count": len(llm_tools),
                "enable_thinking": thinking_config.type != "disabled",
                "thinking_type": thinking_config.type,
                "thinking_budget_tokens": thinking_budget_tokens,
                "thinking_source": thinking_config.source,
                "effort": effective_effort,
                "display_effort": effort_config.level.value,
                "effort_source": effort_config.source,
                "streaming": streaming,
            })

        # Call LLM via _call_with_retry
        # Exceptions (PromptTooLongError, ModelNotAvailableError,
        # CannotRetryError, FallbackTriggeredError) propagate to caller.
        response = await self._call_with_retry(
            abort_event=abort_event,
            fallback_model=fallback_model,
            thinking_budget_tokens=thinking_budget_tokens,
            emit_events=emit_events,
            **completion_kwargs,
        )

        # Validate response
        if not response.choices:
            raise ValueError("LLM response has no choices")

        choice = response.choices[0]
        response_message = choice.message

        # Parse usage → TokenUsage
        raw_usage = getattr(response, "usage", None)
        if raw_usage is not None:
            if hasattr(raw_usage, "model_dump"):
                usage = token_usage_from_dict(raw_usage.model_dump())
            elif isinstance(raw_usage, Mapping):
                usage = token_usage_from_dict(dict(raw_usage))
            elif hasattr(raw_usage, "__dict__"):
                usage = token_usage_from_dict(vars(raw_usage))
            else:
                usage = TokenUsage()
        else:
            usage = TokenUsage()

        # Parse content & stop_reason
        content_text: str = response_message.content or ""
        stop_reason: str | None = getattr(choice, "finish_reason", None)
        if not _usage_has_counts(usage) and content_text:
            usage = _estimate_missing_usage(
                model=request_model,
                input_messages=api_messages,
                output_text=content_text,
            )

        # Emit tokens & llm_complete
        streamed = bool(getattr(response, "_openspace_streamed", False))
        if emit_events and not streamed:
            await self._emit_text_tokens(content_text)
        if emit_events:
            await self._emit_event("llm_complete", {
                "model": request_model,
                "usage": {
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                },
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "content_length": len(content_text),
                "stop_reason": stop_reason,
                "streaming": streamed or streaming,
            })
        await self._invoke_usage_callback(request_model, usage)

        # Build assistant_message (OpenAI format)
        assistant_message: Dict[str, Any] = {
            "role": "assistant",
            "content": content_text,
        }

        # Serialize thinking/reasoning fields.
        reasoning_content = getattr(response_message, "reasoning_content", None)
        if not reasoning_content:
            psf = getattr(response_message, "provider_specific_fields", None)
            if psf and isinstance(psf, dict):
                reasoning_content = psf.get("reasoning")
        if reasoning_content:
            assistant_message["reasoning_content"] = self._serialize_response_field(
                reasoning_content,
            )

        msg_name = getattr(response_message, "name", None)
        if msg_name:
            assistant_message["name"] = msg_name

        # Keep OpenAI-compatible tool call shape. Arguments remain provider
        # wire values here; tool_runtime.pipeline.execution owns parse/fallback.
        tool_calls_raw = getattr(response_message, "tool_calls", None)
        parsed_tool_calls: list[Dict[str, Any]] = []
        malformed_tool_arguments: list[dict[str, Any]] = []

        if tool_calls_raw:
            for tc in tool_calls_raw:
                tc_name = tc.function.name
                (
                    safe_arguments,
                    _parsed_arguments,
                    malformed_arguments,
                ) = self._sanitize_tool_arguments_for_history(
                    tc.function.arguments,
                    tc_name,
                )
                if malformed_arguments:
                    malformed_tool_arguments.append({
                        "id": tc.id,
                        "tool": tc_name,
                        "raw_argument_chars": len(tc.function.arguments or ""),
                    })
                parsed_tool_calls.append({
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc_name,
                        "arguments": safe_arguments,
                    },
                })

        if not parsed_tool_calls:
            parsed_tool_calls = _parse_text_tool_calls(content_text, tool_map)
            if parsed_tool_calls:
                logger.info(
                    "Parsed %d text tool call(s) from assistant content",
                    len(parsed_tool_calls),
                )

        if parsed_tool_calls:
            assistant_message["tool_calls"] = parsed_tool_calls

        # Attach _meta to assistant_message
        assistant_message["_meta"] = {
            "usage": {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_creation_input_tokens": usage.cache_creation_input_tokens,
                "cache_read_input_tokens": usage.cache_read_input_tokens,
                "reasoning_tokens": usage.reasoning_tokens,
                "total_tokens": usage.total_tokens,
                "cost": usage.cost,
                "web_search_requests": usage.web_search_requests,
            },
            "model": request_model,
            "stop_reason": stop_reason,
            "timestamp": time.time(),
            "thinking_type": thinking_config.type,
            "thinking_budget_tokens": thinking_budget_tokens,
            "thinking_source": thinking_config.source,
            "effort": effective_effort,
            "display_effort": effort_config.level.value,
            "effort_source": effort_config.source,
        }
        if malformed_tool_arguments:
            assistant_message["_meta"][
                "malformed_tool_arguments"
            ] = malformed_tool_arguments

        # Build output messages list
        # messages = normalized input + assistant response (+ optional
        # error messages).  Caller should ONLY append assistant_message
        # to its own list — see ModelResponse.messages docstring.
        output_messages: list[Dict] = list(api_messages)
        output_messages.append(assistant_message)

        # Implementation: getErrorMessageIfRefusal (errors.ts L1184-1207)
        refusal_msg = self._get_error_message_if_refusal(
            stop_reason, request_model,
        )
        if refusal_msg is not None:
            output_messages.append(refusal_msg)

        if stop_reason == "length":
            output_messages.append(build_assistant_api_error_message(
                "Response was cut off because it reached the maximum "
                "output token limit. The model may not have completed "
                "its response.",
                error_details=f"stop_reason=length, model={request_model}",
            ))

        # Return ModelResponse
        return ModelResponse(
            assistant_message=assistant_message,
            tool_calls=parsed_tool_calls,
            tool_map=tool_map,
            stop_reason=stop_reason,
            usage=usage,
            messages=output_messages,
            effective_model=request_model,
        )

    async def call_model_with_fallback(
        self,
        messages: List[Dict],
        tools: List[BaseTool] | None = None,
        **kwargs,
    ) -> "ModelResponse":
        """Variant of :meth:`call_model` that transparently applies the
        consecutive-overloaded fallback switch.

        ``call_model`` surfaces :class:`FallbackTriggeredError` to the caller so
        the main agent loop can update its own bookkeeping (event emission,
        ``stop_reason_final``, TUI sync).  Auxiliary call sites
        (``BaseAgent.get_llm_response``, ``ExecutionAnalyzer``,
        ``SkillEvolver``, compaction, etc.) don't have that bookkeeping and
        previously treated the error as a hard failure — meaning the same
        provider overload that survives under the main loop would abort the
        auxiliary path.

        This helper unifies fallback semantics for those callers:

          1. Call :meth:`call_model` with the provided arguments.
          2. If it raises :class:`FallbackTriggeredError` and the fallback
             model differs from the currently effective model, retry once using
             a request-local ``model=<fallback>`` override.
          3. If the fallback model is missing, equal to the current model, or
             itself raises :class:`FallbackTriggeredError`, the error is
             propagated — callers can still surface a terminal failure.

        The shared ``self.model`` and ``self.fallback_model`` defaults are not
        mutated.  Callers that run multiple auxiliary iterations should carry
        ``ModelResponse.effective_model`` forward as their local model.

        All other exceptions (``CannotRetryError``, ``PromptTooLongError``,
        ``ModelNotAvailableError``, abort, …) propagate unchanged.
        """
        try:
            response = await self.call_model(messages=messages, tools=tools, **kwargs)
            response.effective_model = str(kwargs.get("model", self.model) or "")
            return response
        except FallbackTriggeredError as fb_err:
            fallback_model = str(fb_err.fallback_model or "").strip()
            current_model = str(kwargs.get("model", self.model) or "")
            if not fallback_model or fallback_model == current_model:
                raise

            self._logger.warning(
                "Auxiliary fallback switch: %s -> %s",
                fb_err.original_model, fallback_model,
            )
            retry_kwargs = dict(kwargs)
            retry_kwargs["model"] = fallback_model
            response = await self.call_model(
                messages=messages, tools=tools, **retry_kwargs,
            )
            response.effective_model = fallback_model
            return response

    @staticmethod
    def _find_seed_tool_use_context(
        tools: Sequence[BaseTool] | None,
    ) -> Any | None:
        """Return the first tool-attached ToolUseContext-like object, if any."""
        if not tools:
            return None

        for tool in tools:
            context = getattr(tool, "_current_context", None)
            if context is not None:
                return context
        return None

    @classmethod
    def _infer_auxiliary_cwd(
        cls,
        tools: Sequence[BaseTool] | None,
        seed_context: Any | None = None,
    ) -> str:
        """Best-effort cwd for auxiliary tool execution."""
        seed_cwd = getattr(seed_context, "cwd", None)
        if isinstance(seed_cwd, str) and seed_cwd.strip():
            return seed_cwd

        if tools:
            for tool in tools:
                direct_cwd = getattr(tool, "_default_working_dir", None)
                if isinstance(direct_cwd, str) and direct_cwd.strip():
                    return direct_cwd

                session = getattr(tool, "_session", None)
                session_cwd = getattr(session, "default_working_dir", None)
                if isinstance(session_cwd, str) and session_cwd.strip():
                    return session_cwd

                tool_cwd = getattr(tool, "default_working_dir", None)
                if isinstance(tool_cwd, str) and tool_cwd.strip():
                    return tool_cwd

        return os.getcwd()

    @staticmethod
    def _load_auxiliary_permission_context(
        cwd: str,
        mode: str | None,
    ) -> Any:
        """Load workspace permission context for auxiliary callers.

        Mirrors ``GroundingAgent._resolve_permission_context``: aggregates
        ``.openspace/settings*.json`` sources (userSettings, projectSettings,
        localSettings) plus runtime stores into
        a real :class:`ToolPermissionContext`.  Loader failures are surfaced
        instead of replacing the real workspace state with an empty default.
        """
        try:
            from openspace.grounding.core.permissions.loader import (
                load_tool_permission_context,
            )

            return load_tool_permission_context(cwd, mode)
        except Exception as exc:
            logger.error(
                "Failed to load tool permission context for %s (mode=%s)",
                cwd,
                mode,
                exc_info=True,
            )
            raise RuntimeError(
                "Failed to load tool permission settings for this workspace"
            ) from exc

    @staticmethod
    def _build_default_hook_registry() -> Any:
        """Create the same built-in hook registry used by the main agent loop."""
        from openspace.services.tooling.hooks import HookRegistry, setup_default_hooks

        registry = HookRegistry()
        setup_default_hooks(registry)
        return registry

    def build_auxiliary_tool_use_context(
        self,
        *,
        tools: Sequence[BaseTool],
        messages: list[dict[str, Any]],
        model: str | None = None,
        cwd: str | None = None,
        agent_id: str | None = None,
        agent_type: str | None = None,
        max_result_size_chars: int | None = None,
        abort_event: asyncio.Event | None = None,
        read_file_state: dict[str, Any] | None = None,
        tool_results_token_count: int | None = None,
        permission_engine: Any | None = None,
        permission_mode: str | None = None,
        permission_context: Any | None = None,
        hook_registry: Any | None = None,
        tui_available: bool | None = None,
        is_async_agent: bool | None = None,
        event_sink: Any | None = None,
        recording_manager: Any | None = None,
        quality_manager: Any | None = None,
        parent_task_id: str | None = None,
        task_description: str | None = None,
        current_iteration: int | None = None,
        max_iterations: int | None = None,
    ) -> Any:
        """Build a ToolUseContext for non-primary loops that execute tools.

        Preference order:
        1. Explicit arguments from the caller.
        2. Context already attached to tools from a prior main-loop execution.
        3. Reasonable runtime defaults (real cwd, workspace-loaded permission
           context via :func:`load_tool_permission_context`, event sink from
           the LLM client).

        ``tui_available`` deliberately **defaults to ``False`` (fail-closed)**
        when no explicit value and no seed context attribute is available.
        Mirrors ``GroundingAgent._resolve_tui_available``: the presence of a
        generic event callback (``set_event_callback``) or a runtime event
        sink on a sub-agent is *not* enough — those are one-way streaming
        hooks, not an interactive TUI that can reply to
        ``tool_permission_ask``. Entering the ask flow without a real
        responder otherwise hangs auxiliary callers on the permission ask
        timeout.
        """
        from openspace.grounding.core.tool.base import DEFAULT_MAX_RESULT_SIZE_CHARS
        from openspace.services.tooling.context import (
            ToolUseContext,
            clone_skill_invocation_scopes,
        )

        seed_context = self._find_seed_tool_use_context(tools)
        resolved_cwd = cwd or self._infer_auxiliary_cwd(tools, seed_context)

        resolved_permission_context = (
            permission_context
            or getattr(seed_context, "permission_context", None)
        )
        # Track whether the caller/seed actually supplied a mode.  When they
        # didn't we must leave ``mode`` unset so the workspace settings loader
        # can honour ``permissions.defaultMode`` from ``.openspace/settings.json``
        # instead of being overridden by a hard-coded fallback.
        explicit_permission_mode = (
            permission_mode
            or getattr(seed_context, "permission_mode", None)
            or getattr(resolved_permission_context, "mode", None)
        )
        if resolved_permission_context is None:
            # Auxiliary loops must honour the same workspace permission cascade
            # as the primary agent loop.
            resolved_permission_context = self._load_auxiliary_permission_context(
                resolved_cwd,
                explicit_permission_mode,
            )

        resolved_permission_mode = (
            explicit_permission_mode
            or getattr(resolved_permission_context, "mode", None)
            or "default"
        )

        resolved_max_result_size = max_result_size_chars
        if resolved_max_result_size is None:
            resolved_max_result_size = getattr(
                seed_context,
                "max_result_size_chars",
                DEFAULT_MAX_RESULT_SIZE_CHARS,
            )

        resolved_read_file_state = read_file_state
        if resolved_read_file_state is None:
            seeded_state = getattr(seed_context, "read_file_state", None)
            resolved_read_file_state = dict(seeded_state or {})

        resolved_tool_results_tokens = tool_results_token_count
        if resolved_tool_results_tokens is None:
            resolved_tool_results_tokens = int(
                getattr(seed_context, "tool_results_token_count", 0) or 0
            )

        resolved_hook_registry = (
            hook_registry
            or getattr(seed_context, "hook_registry", None)
            or self._build_default_hook_registry()
        )

        resolved_event_sink = (
            event_sink
            or getattr(seed_context, "event_sink", None)
            or self._event_callback
        )
        # TUI availability must mirror ``GroundingAgent._resolve_tui_available``:
        # an *interactive* sink that can round-trip ``tool_permission_ask`` ->
        # ``tool_permission_response`` is required before we offer the ask
        # flow. ``_event_callback`` (set via :meth:`set_event_callback`) and
        # per-agent ``_runtime_event_sink`` wiring are *unidirectional*
        # streaming hooks — they propagate events outward but cannot resolve
        # a pending ask. Treating their presence as evidence of a TUI makes
        # auxiliary callers (BaseAgent, ExecutionAnalyzer, SkillEvolver, ...)
        # enter the ask flow, emit
        # ``tool_permission_ask``, and then hang on the 300s ask timeout
        # because no responder exists. Main loop is fail-closed here; the
        # auxiliary paths must match that semantics. Callers with a real TUI
        # (e.g. the public application facade) should pass ``tui_available=True`` explicitly.
        # or surface it via the seed ``ToolUseContext``.
        if tui_available is not None:
            resolved_tui_available = bool(tui_available)
        elif seed_context is not None and hasattr(seed_context, "tui_available"):
            resolved_tui_available = bool(getattr(seed_context, "tui_available"))
        else:
            resolved_tui_available = False

        return ToolUseContext(
            tools=list(tools),
            all_tools=list(tools),
            model=str(
                model
                or getattr(seed_context, "model", None)
                or getattr(self, "model", "unknown")
            ),
            llm_client=self,
            cwd=resolved_cwd,
            original_cwd=str(
                getattr(seed_context, "original_cwd", None) or resolved_cwd
            ),
            agent_id=agent_id or getattr(seed_context, "agent_id", None) or "auxiliary",
            agent_type=agent_type if agent_type is not None else getattr(seed_context, "agent_type", None),
            max_result_size_chars=int(resolved_max_result_size),
            abort_event=abort_event or getattr(seed_context, "abort_event", None),
            messages=messages,
            read_file_state=resolved_read_file_state,
            tool_results_token_count=resolved_tool_results_tokens,
            permission_engine=permission_engine or getattr(seed_context, "permission_engine", None),
            permission_mode=resolved_permission_mode,
            permission_context=resolved_permission_context,
            hook_registry=resolved_hook_registry,
            tui_available=resolved_tui_available,
            is_async_agent=(
                is_async_agent
                if is_async_agent is not None
                else bool(getattr(seed_context, "is_async_agent", False))
            ),
            event_sink=resolved_event_sink,
            recording_manager=(
                recording_manager
                or getattr(seed_context, "recording_manager", None)
            ),
            quality_manager=(
                quality_manager
                or getattr(seed_context, "quality_manager", None)
            ),
            parent_task_id=(
                parent_task_id
                if parent_task_id is not None
                else getattr(seed_context, "parent_task_id", None)
            ),
            task_id=getattr(seed_context, "task_id", None),
            task_description=(
                task_description
                if task_description is not None
                else str(getattr(seed_context, "task_description", "") or "")
            ),
            current_iteration=(
                int(current_iteration)
                if current_iteration is not None
                else int(getattr(seed_context, "current_iteration", 0) or 0)
            ),
            max_iterations=(
                int(max_iterations)
                if max_iterations is not None
                else int(getattr(seed_context, "max_iterations", 0) or 0)
            ),
            session_id=getattr(seed_context, "session_id", None),
            session_dir=getattr(seed_context, "session_dir", None),
            tool_results_dir=getattr(seed_context, "tool_results_dir", None),
            session_storage=getattr(seed_context, "session_storage", None),
            file_history=getattr(seed_context, "file_history", None),
            skill_registry=getattr(seed_context, "skill_registry", None),
            skill_store=getattr(seed_context, "skill_store", None),
            sent_skill_names_by_agent={
                str(agent): set(names or ())
                for agent, names in (
                    getattr(seed_context, "sent_skill_names_by_agent", {}) or {}
                ).items()
            },
            discovered_skill_names=set(
                getattr(seed_context, "discovered_skill_names", set()) or set()
            ),
            invoked_skills_by_agent={
                str(agent): list(records or [])
                for agent, records in (
                    getattr(seed_context, "invoked_skills_by_agent", {}) or {}
                ).items()
            },
            skill_listing_suppressed_once=bool(
                getattr(seed_context, "skill_listing_suppressed_once", False)
            ),
            active_skill_scopes=clone_skill_invocation_scopes(
                getattr(seed_context, "active_skill_scopes", {}) or {}
            ),
            skill_model_override=getattr(seed_context, "skill_model_override", None),
            skill_effort_override=getattr(seed_context, "skill_effort_override", None),
            dynamic_skill_path_triggers=set(
                getattr(seed_context, "dynamic_skill_path_triggers", set()) or set()
            ),
            sent_dynamic_skill_keys=set(
                getattr(seed_context, "sent_dynamic_skill_keys", set()) or set()
            ),
            path_activated_skill_names=set(
                getattr(seed_context, "path_activated_skill_names", set()) or set()
            ),
            skills_disabled=bool(getattr(seed_context, "skills_disabled", False)),
        )

    @staticmethod
    def collect_tool_results(
        tool_calls: Sequence[dict[str, Any]],
        tool_map: dict[str, BaseTool],
        tool_messages: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Build structured tool result entries from ``run_tools()`` output."""
        from openspace.tool_runtime.pipeline.execution import find_tool_by_name

        results: list[dict[str, Any]] = []
        tool_messages_by_id: dict[str, dict[str, Any]] = {}
        for message in tool_messages:
            if message.get("role") != "tool":
                continue
            tool_call_id = str(message.get("tool_call_id") or "")
            if tool_call_id:
                tool_messages_by_id[tool_call_id] = message

        for tool_call in tool_calls:
            func = tool_call.get("function", {})
            tool_name = func.get("name", "")
            tool_call_id = str(tool_call.get("id", ""))
            tool_message = tool_messages_by_id.get(tool_call_id)
            if tool_message is None:
                continue

            tool_obj = tool_map.get(tool_name)
            if tool_obj is None:
                tool_obj = find_tool_by_name(list(tool_map.values()), tool_name)

            backend = None
            server_name = None
            if tool_obj is not None:
                runtime_info = getattr(tool_obj, "runtime_info", None)
                if runtime_info is not None:
                    backend = getattr(getattr(runtime_info, "backend", None), "value", None)
                    server_name = getattr(runtime_info, "server_name", None)

            meta = tool_message.get("_meta", {})
            status_raw = str(meta.get("status") or "success").strip().lower()
            error_statuses = {"error", "denied", "cancelled"}
            status = (
                ToolStatus.ERROR
                if status_raw in error_statuses
                else ToolStatus.SUCCESS
            )

            content = tool_message.get("content")
            error = None
            if status == ToolStatus.ERROR and isinstance(content, str):
                error = content.removeprefix("Error: ").strip() or content

            tool_result_kwargs: dict[str, Any] = {
                "status": status,
                "content": content,
                "error": error,
                "execution_time": meta.get("execution_time"),
            }
            metadata = meta.get("tool_result_metadata")
            if isinstance(metadata, dict):
                metadata = dict(metadata)
            else:
                metadata = {}
            if status_raw not in {ToolStatus.SUCCESS.value, ToolStatus.ERROR.value}:
                metadata["raw_status"] = status_raw
            if metadata:
                tool_result_kwargs["metadata"] = metadata

            tool_result = ToolResult(**tool_result_kwargs)

            results.append({
                "tool_call": tool_call,
                "result": tool_result,
                "message": tool_message,
                "backend": backend,
                "server_name": server_name,
            })

        return results

    @staticmethod
    def format_messages_to_text(messages: List[Dict]) -> str:
        """Format conversation history to readable text (for logging/debugging)"""
        formatted = ""
        for msg in messages:
            role = msg.get("role", "unknown").upper()
            content = msg.get("content", "")
            formatted += f"[{role}]\n{content}\n\n"
        return formatted
