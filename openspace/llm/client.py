import litellm
import json
import asyncio
import time
from pathlib import Path
from typing import List, Sequence, Union, Dict, Optional
from dotenv import load_dotenv
from openai.types.chat import ChatCompletionToolParam

from openspace.grounding.core.types import ToolSchema, ToolResult, ToolStatus
from openspace.grounding.core.tool import BaseTool
from openspace.utils.logging import Logger

# Load .env from openspace package root (works regardless of CWD),
# then fall back to CWD/.env.  override=False (default) means first-loaded wins.
_PKG_ENV = Path(__file__).resolve().parent.parent / ".env"  # openspace/.env
if _PKG_ENV.is_file():
    load_dotenv(_PKG_ENV)
load_dotenv()  # also try CWD/.env for any remaining vars

# Disable LiteLLM verbose logging to prevent stdout blocking with large tool schemas
litellm.set_verbose = False
litellm.suppress_debug_info = True

logger = Logger.get_logger(__name__)


def _sanitize_schema(params: Dict) -> Dict:
    """Sanitize tool parameter schema to comply with Claude API requirements.
    
    Fixes common issues:
    - Empty object schemas (no properties, no required)
    - Missing required fields for Claude compatibility
    """
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
       
def _prepare_tools_for_llmclient(
    tools: List[BaseTool] | None,
    fmt: str = "openai",
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
            
            tool_param = _schema_to_openai(tool.schema)
            tool_param["function"]["name"] = llm_name

            # Tag the description with backend type so the LLM knows each
            # tool's origin (e.g. "[MCP] ...", "[Shell] ...").
            backend_type = getattr(tool.schema, "backend_type", None)
            if backend_type and backend_type.value not in ("not_set",):
                _BACKEND_LABELS = {
                    "mcp": "MCP",
                    "shell": "Shell",
                    "gui": "GUI",
                    "web": "Web",
                    "system": "System",
                }
                label = _BACKEND_LABELS.get(backend_type.value, backend_type.value)
                desc = tool_param["function"].get("description", "")
                tool_param["function"]["description"] = f"[{label}] {desc}"

            result.append(tool_param)
            
            tool_map[llm_name] = tool
            
            if llm_name != original_name:
                logger.info(f"[TOOL_RENAME] {original_name} -> {llm_name}")
        
        logger.info(f"[SCHEMA_SANITIZE] Prepared {len(result)} tools for LLM (from {len(tools)} total)")
        return result, tool_map
    
    tool_map = {tool.schema.name: tool for tool in tools}
    return [tool.schema for tool in tools], tool_map


def _infer_backend_from_tool_name(tool_name: str) -> Optional[str]:
    """Infer backend when tool_results would otherwise have no backend (name mismatch or unbound tools)."""
    if not tool_name or not isinstance(tool_name, str):
        return None
    name = tool_name.strip()
    # Dedup format: "server__toolname" -> use suffix
    if "__" in name:
        name = name.split("__", 1)[-1]
    shell_tools = {"shell_agent", "read_file", "write_file", "list_dir", "run_shell"}
    if name in shell_tools:
        return "shell"
    if name in ("gui_agent",) or "gui" in name.lower():
        return "gui"
    if "mcp" in name.lower() or ("." in name and "__" not in name):
        return "mcp"
    if name in ("deep_research_agent", "deep_research"):
        return "web"
    return None


DEFAULT_SUMMARIZE_THRESHOLD_CHARS = 200000  # ~50K tokens, lowered from 400K to prevent context overflow
MAX_TOOL_RESULT_CHARS = 200000  # Fallback truncation limit when summarization fails (~50K tokens)

async def _summarize_tool_result(
    content: str,
    tool_name: str,
    task: str = "",
    model: str = "openrouter/anthropic/claude-sonnet-4.5",
    timeout: float = 120.0
) -> str:
    """Use LLM to summarize large tool results."""
    try:
        from gdpval_bench.token_tracker import set_call_source, reset_call_source
        _src_tok = set_call_source("summarizer")
    except ImportError:
        _src_tok = None

    try:
        logger.info(f"Summarizing tool result from '{tool_name}': {len(content):,} chars")
        
        # Pre-truncate if content is too large for the model (leave room for prompt + output)
        # Assuming ~4 chars per token, 200K tokens limit, 8K output, ~500 tokens for prompt
        # Safe input limit: (200K - 8K - 0.5K) * 4 = ~766K chars, but be conservative at 400K
        max_input_chars = 200000
        if len(content) > max_input_chars:
            logger.warning(f"Pre-truncating content for summarization: {len(content):,} -> {max_input_chars:,} chars")
            content = content[:max_input_chars] + f"\n\n[TRUNCATED for summarization: original was {len(content):,} chars]"
        
        task_hint = f"\n\nUser's task: {task}\nSummarize with focus on information relevant to this task." if task else ""
        
        prompt = f"""Tool '{tool_name}' returned a large result ({len(content):,} chars). Summarize it concisely.{task_hint}

**Guidelines:**
- Structured data (coordinates, steps, etc.): Keep key summary (totals, start/end), omit repetitive details.
- Markup content (HTML, XML): Extract text and key data only, ignore tags/scripts.
- Long documents: Keep structure outline and essential sections.
- Lists/arrays: Summarize count and most relevant items.
- Always preserve: numbers, URLs, file paths, IDs, key identifiers.

Content:
{content}

Concise summary:"""
        
        response = await asyncio.wait_for(
            litellm.acompletion(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                timeout=timeout
            ),
            timeout=timeout + 5
        )
        
        summary = response.choices[0].message.content.strip()
        result = f"[SUMMARY of {len(content):,} chars]\n{summary}"
        
        logger.info(f"Tool result summarized: {len(content):,} -> {len(result):,} chars")
        return result
        
    except Exception as e:
        logger.warning(f"Summarization failed for '{tool_name}': {e}")
        return None
    finally:
        if _src_tok is not None:
            reset_call_source(_src_tok)


async def _tool_result_to_message_async(
    result: ToolResult, 
    *, 
    tool_call_id: str, 
    tool_name: str,
    task: str = "",
    summarize_threshold: int = DEFAULT_SUMMARIZE_THRESHOLD_CHARS,
    summarize_model: str = "openrouter/anthropic/claude-sonnet-4.5",
    enable_summarization: bool = True
) -> Dict:
    """Convert ToolResult to LLMClient usable message format with LLM summarization for large results.

    Args:
        result: Tool execution result
        tool_call_id: OpenAI tool_call ID
        tool_name: Tool name
        task: User's original task for context-aware summarization
        summarize_threshold: If content exceeds this, use LLM summarization
        summarize_model: Model to use for summarization
        enable_summarization: Whether to enable LLM summarization
        
    Returns:
        OpenAI ChatCompletion tool message (text only)
    """
    if result.is_error:
        text_content = f"[ERROR] {result.error or 'unknown error'}"
    else:
        text_content = (
            result.content
            if isinstance(result.content, str)
            else json.dumps(result.content, ensure_ascii=False, default=str)
        )
    
    original_len = len(text_content)
    
    # Use LLM summarization if content exceeds threshold
    if original_len > summarize_threshold and enable_summarization:
        summary = await _summarize_tool_result(text_content, tool_name, task, summarize_model)
        if summary:
            text_content = summary
        elif original_len > MAX_TOOL_RESULT_CHARS:
            # Fallback: truncate if summarization failed and content is too large
            truncate_msg = f"\n\n[TRUNCATED: Original content was {original_len:,} chars, showing first {MAX_TOOL_RESULT_CHARS:,}]"
            text_content = text_content[:MAX_TOOL_RESULT_CHARS - len(truncate_msg)] + truncate_msg
            logger.warning(f"Tool result truncated for '{tool_name}': {original_len:,} -> {len(text_content):,} chars (summarization failed)")
    
    return {
        "role": "tool",
        "name": tool_name,
        "content": text_content,
        "tool_call_id": tool_call_id,
    }

async def _execute_tool_call(
    tool: BaseTool,
    openai_tool_call: Dict,
) -> ToolResult:
    """Execute LLMClient returned tool_call

    Args:
        tool: BaseTool instance (must be obtained from GroundingClient and bound to runtime_info)
        openai_tool_call: LLMClient usable tool_call object, contains id, type, function etc. fields
    """
    if not tool.is_bound:
        raise ValueError(
            f"Tool '{tool.schema.name}' is not bound to runtime_info. "
            f"Please ensure tools are obtained from GroundingClient.list_tools() "
            f"with bind_runtime_info=True"
        )
    
    func = openai_tool_call["function"]
    arguments = func.get("arguments", "{}")
    if isinstance(arguments, str):
        arguments = json.loads(arguments or "{}")
    
    # Filter out parameters that are not in the tool's schema
    if isinstance(arguments, dict) and tool.schema.parameters:
        # Get valid parameter names from tool schema (JSON Schema format)
        schema_params = tool.schema.parameters
        valid_params = set()
        
        if isinstance(schema_params, dict) and "properties" in schema_params:
            valid_params = set(schema_params["properties"].keys())
        
        # Check for invalid parameters
        invalid_params = []
        for param_name in list(arguments.keys()):
            if param_name == "skip_visual_analysis":
                invalid_params.append(param_name)
                continue
            
            # Check if parameter is in the tool's schema
            if valid_params and param_name not in valid_params:
                invalid_params.append(param_name)
        
        # Remove invalid parameters
        for param in invalid_params:
            arguments.pop(param)
            logger.debug(
                f"Removed parameter '{param}' from {tool.schema.name} "
                f"(not in tool schema)"
            )

    return await tool.invoke(
        parameters=arguments,
        keep_session=True
    )


class LLMClient:
    """LLMClient class for single round call"""
    def __init__(
        self, 
        model: str = "openrouter/anthropic/claude-sonnet-4.5", 
        enable_thinking: bool = False,
        rate_limit_delay: float = 0.0,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        timeout: float = 120.0,
        summarize_threshold_chars: int = DEFAULT_SUMMARIZE_THRESHOLD_CHARS,
        enable_tool_result_summarization: bool = True,
        **litellm_kwargs
    ):
        """
        Args:
            model: LLM model identifier
            enable_thinking: Whether to enable extended thinking mode
            rate_limit_delay: Minimum delay between API calls in seconds (0 = no delay)
            max_retries: Maximum number of retries on rate limit errors
            retry_delay: Initial delay between retries in seconds (exponential backoff)
            timeout: Request timeout in seconds (default: 120s)
            summarize_threshold_chars: If tool result exceeds this threshold, use LLM to 
                                       summarize the result (default: 50000 chars ≈ 12.5K tokens)
            enable_tool_result_summarization: Whether to enable LLM-based summarization for 
                                              large tool results (default: True)
            **litellm_kwargs: Additional litellm parameters
        """
        self.model = model
        self.enable_thinking = enable_thinking
        self.rate_limit_delay = rate_limit_delay
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.timeout = timeout
        self.summarize_threshold_chars = summarize_threshold_chars
        self.enable_tool_result_summarization = enable_tool_result_summarization
        self.litellm_kwargs = litellm_kwargs
        self._logger = Logger.get_logger(__name__)
        self._last_call_time = 0.0
    
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
        if not cls._is_minimax_model(model):
            return messages

        minimized_system_history = cls._merge_consecutive_system_messages(messages)
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
    
    async def _call_with_retry(self, **completion_kwargs):
        """Call LLM with backoff retry on rate limit errors
        
        Timeout and retry strategy:
        - Single call timeout: self.timeout (default 120s)
        - Rate limit retry delays: 60s, 90s, 120s
        - Total max time: timeout * max_retries + sum(retry_delays)
        """
        last_exception = None
        
        for attempt in range(self.max_retries):
            try:
                # Add timeout to the completion call
                response = await asyncio.wait_for(
                    litellm.acompletion(**completion_kwargs),
                    timeout=self.timeout
                )
                return response
            except asyncio.TimeoutError:
                self._logger.error(
                    f"LLM call timed out after {self.timeout}s (attempt {attempt + 1}/{self.max_retries})"
                )
                last_exception = TimeoutError(f"LLM call timed out after {self.timeout}s")
                if attempt < self.max_retries - 1:
                    # Retry on timeout with shorter delay
                    self._logger.info(f"Retrying after {self.retry_delay}s delay...")
                    await asyncio.sleep(self.retry_delay)
                    continue
                else:
                    raise last_exception
            except Exception as e:
                last_exception = e
                error_str = str(e).lower()
                
                # Check if it's a retryable error
                is_rate_limit = any(
                    keyword in error_str 
                    for keyword in ['rate limit', 'rate_limit', 'too many requests', '429']
                )
                
                is_overloaded = any(
                    keyword in error_str
                    for keyword in ['overloaded', '500', '502', '503', '504', 'internal server error', 'service unavailable']
                )
                
                is_connection_error = any(
                    keyword in error_str
                    for keyword in ['cannot connect', 'connection refused', 'connection reset',
                                    'connectionerror', 'timeout', 'name resolution',
                                    'temporary failure', 'network unreachable']
                )
                
                if attempt < self.max_retries - 1 and (is_rate_limit or is_overloaded or is_connection_error):
                    if is_rate_limit:
                        backoff_delay = 60 + (attempt * 30)  # 60s, 90s, 120s
                        error_type = "Rate limit"
                    elif is_connection_error:
                        backoff_delay = min(10 * (2 ** attempt), 60)  # 10s, 20s, 40s, max 60s
                        error_type = "Connection"
                    else:
                        backoff_delay = min(5 * (2 ** attempt), 60)  # 5s, 10s, 20s, max 60s
                        error_type = "Server overload"
                    
                    self._logger.warning(
                        f"{error_type} error (attempt {attempt + 1}/{self.max_retries}), "
                        f"waiting {backoff_delay}s before retry..."
                    )
                    await asyncio.sleep(backoff_delay)
                    continue
                else:
                    # Not a retryable error, or max retries reached
                    if attempt >= self.max_retries - 1:
                        self._logger.error(f"Max retries ({self.max_retries}) reached, giving up")
                    raise
        
        raise last_exception
    
    async def complete(
        self,
        messages: List[Dict] | str, 
        tools: List[BaseTool] | None = None,
        execute_tools: bool = True,
        summary_prompt: Optional[str] = None,
        tool_result_callback: Optional[callable] = None,
        **kwargs
    ) -> Dict:
        """
        Single-round LLM call with optional tool execution.
        
        Args:
            messages: conversation history (List[Dict] for standard OpenAI format, or str for text format)
            tools: BaseTool instance list (must be obtained from GroundingClient and bound to runtime_info)
                if None or empty list, only perform conversation, no tools
            execute_tools: if LLM returns tool_calls, whether to automatically execute tools
            summary_prompt: Optional custom prompt for requesting iteration summary. 
                If provided, will request summary after tool execution.
                If None, no summary will be requested.
            tool_result_callback: Optional async callback to process tool results after execution.
                Signature: async def callback(result: ToolResult, tool_name: str, tool_call: Dict, backend: str) -> ToolResult
            **kwargs: additional parameters for litellm completion
        """
        # 1. Process messages
        if isinstance(messages, str):
            current_messages = [{"role": "user", "content": messages}]
            user_task = messages
        elif isinstance(messages, list):
            current_messages = messages.copy()
            # Extract first user message as task for context-aware summarization
            user_task = next(
                (m.get("content", "") for m in messages if m.get("role") == "user"),
                ""
            )
        else:
            raise ValueError("messages must be List[Dict] or str")
        
        # 2. prepare base litellm completion kwargs
        completion_kwargs = {
            "model": kwargs.get("model", self.model),
            **self.litellm_kwargs,
        }
        request_model = completion_kwargs["model"]
        
        # Add thinking/reasoning_effort only if explicitly enabled and not using tools
        enable_thinking = kwargs.get("enable_thinking", self.enable_thinking)
        
        # 3. if tools are provided, add them to the request
        llm_tools = None
        tool_map = {}  # llm_name -> BaseTool
        if tools:
            llm_tools, tool_map = _prepare_tools_for_llmclient(tools, fmt="openai")
            if llm_tools:
                completion_kwargs["tools"] = llm_tools
                completion_kwargs["tool_choice"] = kwargs.get("tool_choice", "auto")
                # Disable thinking when using tools to avoid format conflicts
                enable_thinking = False
                self._logger.debug(f"Prepared {len(llm_tools)} tools for LLM")
            else:
                self._logger.warning("Tools provided but none could be prepared for LLM")
        
        # Add thinking parameters if enabled
        if enable_thinking:
            completion_kwargs["reasoning_effort"] = kwargs.get("reasoning_effort", "medium")
        
        # 4. Normalize messages for providers with stricter role constraints.
        current_messages = self._normalize_messages_for_model(
            current_messages,
            request_model,
        )

        # 5. Apply rate limiting
        await self._rate_limit()
        
        # 6. Call LLM with retry (single round)
        completion_kwargs["messages"] = current_messages
        response = await self._call_with_retry(**completion_kwargs)
        
        if not response.choices:
            raise ValueError("LLM response has no choices")
        
        response_message = response.choices[0].message
        
        # 6. Build assistant message
        assistant_message = {
            "role": "assistant",
            "content": response_message.content or "",
        }

        for field_name in ("reasoning_details", "reasoning_content", "name"):
            field_value = getattr(response_message, field_name, None)
            if field_value:
                assistant_message[field_name] = self._serialize_response_field(field_value)
        
        tool_calls = getattr(response_message, 'tool_calls', None)
        if tool_calls:
            assistant_message["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments
                    }
                }
                for tc in tool_calls
            ]
        
        # Add assistant message to conversation
        current_messages.append(assistant_message)
        
        # 7. Execute tools if requested
        tool_results = []
        if execute_tools and tool_calls and tools:
            self._logger.info(f"Executing {len(tool_calls)} tool calls...")
            
            for tool_call in tool_calls:
                tool_name = tool_call.function.name
                
                # Resolve tool instance: key might differ from model response (e.g. API returns
                # "read_file" while we stored "server__read_file" for dedup), so fallback by schema.name
                tool_obj = tool_map.get(tool_name)
                if tool_obj is None and tool_name:
                    for _k, _t in tool_map.items():
                        if getattr(getattr(_t, "schema", None), "name", None) == tool_name:
                            tool_obj = _t
                            break
                
                backend = None
                server_name = None
                
                if tool_obj:
                    try:
                        # Prefer runtime_info if bound
                        if getattr(tool_obj, 'is_bound', False) and getattr(tool_obj, 'runtime_info', None):
                            backend = tool_obj.runtime_info.backend.value
                            server_name = tool_obj.runtime_info.server_name
                        else:
                            bt = getattr(tool_obj, 'backend_type', None)
                            bv = getattr(bt, 'value', None) if bt is not None else None
                            if bv and bv not in ("not_set",):
                                backend = bv
                    except Exception as e:
                        self._logger.warning(f"Failed to resolve backend for tool '{tool_name}': {e}")
                
                # Ensure backend is set for recording: API may return different tool name, or
                # runtime_info/backend_type can be missing or raise
                if backend is None and tool_name:
                    backend = _infer_backend_from_tool_name(tool_name)
                    if backend is None:
                        self._logger.warning(
                            f"Could not resolve backend for tool '{tool_name}', "
                            f"recording will be skipped"
                        )
                
                # Log tool execution
                try:
                    if isinstance(tool_call.function.arguments, str):
                        safe_args_str = tool_call.function.arguments.strip() or "{}"
                        args = json.loads(safe_args_str)
                    else:
                        args = tool_call.function.arguments
                    
                    args_str = json.dumps(args, ensure_ascii=False)[:200]
                    self._logger.info(f"Calling {tool_name} with args: {args_str}")
                except:
                    pass
                
                if tool_name not in tool_map:
                    result = ToolResult(
                        status=ToolStatus.ERROR,
                        error=f"Tool '{tool_name}' not found"
                    )
                else:
                    try:
                        result = await _execute_tool_call(
                            tool=tool_map[tool_name],
                            openai_tool_call={
                                "id": tool_call.id,
                                "type": "function",
                                "function": {
                                    "name": tool_call.function.name,
                                    "arguments": tool_call.function.arguments
                                }
                            }
                        )

                        # Apply tool result callback if provided
                        if tool_result_callback and not result.is_error:
                            try:
                                result = await tool_result_callback(
                                    result=result,
                                    tool_name=tool_name,
                                    tool_call=tool_call,
                                    backend=backend
                                )
                            except Exception as e:
                                self._logger.warning(f"Tool result callback failed for {tool_name}: {e}")
                    except Exception as e:
                        result = ToolResult(
                            status=ToolStatus.ERROR,
                            error=str(e)
                        )
                
                # Use async version with LLM summarization for large results
                tool_message = await _tool_result_to_message_async(
                    result, 
                    tool_call_id=tool_call.id, 
                    tool_name=tool_name,
                    task=user_task,
                    summarize_threshold=self.summarize_threshold_chars,
                    summarize_model=self.model,
                    enable_summarization=self.enable_tool_result_summarization
                )
                current_messages.append(tool_message)
                
                # Store result
                tool_results.append({
                    "tool_call": tool_call,
                    "result": result,
                    "message": tool_message,
                    "backend": backend,
                    "server_name": server_name,
                })
            
            self._logger.info(f"Tool execution completed, {len(tool_results)} tools executed")
        
        # 8. Request summary if provided and tools were executed
        iteration_summary = None
        
        if summary_prompt and tool_results:
            self._logger.debug("Requesting iteration summary from LLM")
            summary_message = {
                "role": "system",
                "content": summary_prompt
            }
            current_messages.append(summary_message)
            current_messages = self._normalize_messages_for_model(
                current_messages,
                request_model,
            )
            
            # Apply rate limiting before summary call
            await self._rate_limit()
            
            # Call LLM to generate summary (without tools)
            summary_kwargs = {
                **self.litellm_kwargs,
                "model": request_model,
                "messages": current_messages,
                "tools": [], 
                "tool_choice": "none",
            }
            
            summary_response = await self._call_with_retry(**summary_kwargs)
            
            if summary_response.choices:
                summary_message = summary_response.choices[0].message
                iteration_summary = summary_message.content or ""
                
                # Add summary response to messages
                current_messages.append({
                    "role": "assistant",
                    "content": iteration_summary
                })
                
                self._logger.debug(f"Generated iteration summary: {iteration_summary[:100]}...")
                
        # 9. Return single-round result        
        return {
            "message": assistant_message,
            "tool_results": tool_results,
            "messages": current_messages,
            "has_tool_calls": bool(tool_calls),
            "iteration_summary": iteration_summary
        }
    
    @staticmethod
    def format_messages_to_text(messages: List[Dict]) -> str:
        """Format conversation history to readable text (for logging/debugging)"""
        formatted = ""
        for msg in messages:
            role = msg.get("role", "unknown").upper()
            content = msg.get("content", "")
            formatted += f"[{role}]\n{content}\n\n"
        return formatted