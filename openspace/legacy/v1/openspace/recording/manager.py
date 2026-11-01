import datetime
import json
import ast
import types
from typing import Any, Dict, List, Optional
from pathlib import Path

from openspace.utils.logging import Logger
from .recorder import TrajectoryRecorder
from .action_recorder import ActionRecorder

logger = Logger.get_logger(__name__)


class RecordingManager:
    # Global instance management (singleton pattern)
    _global_instance: Optional['RecordingManager'] = None
    
    def __init__(
        self,
        enabled: bool = True,
        task_id: str = "",
        log_dir: str = "./logs/recordings",
        backends: Optional[List[str]] = None,
        enable_screenshot: bool = True,
        enable_video: bool = False,
        enable_conversation_log: bool = True,
        auto_save_interval: int = 10,
        server_url: Optional[str] = None,
        agent_name: str = "GroundingAgent",
    ):
        """
        Initialize automatic recording manager
        
        Args:
            enabled: whether to enable recording
            task_id: task ID (for naming recording directory)
            log_dir: log directory path
            backends: list of backends to record (None = all)
                    (optional: "mcp", "gui", "shell", "system", "web")
            enable_screenshot: whether to enable screenshot (through platform.ScreenshotClient)
            enable_video: whether to enable video recording (through platform.RecordingClient)
            enable_conversation_log: whether to save LLM conversations to conversations.jsonl (default: True)
            auto_save_interval: automatic save interval (steps)
            server_url: local server address (None = read from config/environment variables)
            agent_name: name of the agent performing the recording (default: "GroundingAgent")
        """
        self.enabled = enabled
        self.task_id = task_id
        self.log_dir = log_dir
        self.backends = set(backends) if backends else {"mcp", "gui", "shell", "system", "web"}
        self.enable_screenshot = enable_screenshot
        self.enable_video = enable_video
        self.enable_conversation_log = enable_conversation_log
        self.auto_save_interval = auto_save_interval
        self.server_url = server_url
        self.agent_name = agent_name
        
        # internal state
        self._recorder: Optional[TrajectoryRecorder] = None
        self._action_recorder: Optional[ActionRecorder] = None
        self._is_started = False
        self._step_counter = 0
        
        # registered LLM clients
        self._registered_llm_clients = []
        self._original_methods = {}
        
        # video/screenshot clients (internal management)
        self._recording_client = None
        self._screenshot_client = None
        
        # Register as global instance
        RecordingManager._global_instance = self

    @classmethod
    def is_recording(cls) -> bool:
        """
        Check if there is an active recording session
        
        Returns:
            bool: True if recording is active
        """
        return cls._global_instance is not None and cls._global_instance._is_started
    
    @classmethod
    async def record_retrieved_tools(
        cls,
        task_instruction: str,
        tools: List[Any],
        search_debug_info: Optional[Dict[str, Any]] = None,
    ):
        """
        Record the tools retrieved for a task
        
        Args:
            task_instruction: The task instruction used for retrieval
            tools: List of retrieved tools
            search_debug_info: Debug info from search (similarity scores, LLM selections)
        """
        instance = cls._global_instance
        if not instance or not instance._is_started or not instance._recorder:
            return
        
        # Extract tool info
        tool_info = []
        for tool in tools:
            info = {
                "name": getattr(tool, "name", str(tool)),
            }
            # Prefer runtime_info.backend
            # over backend_type (may be NOT_SET for cached RemoteTools)
            runtime_info = getattr(tool, "_runtime_info", None)
            if runtime_info and hasattr(runtime_info, "backend"):
                info["backend"] = runtime_info.backend.value if hasattr(runtime_info.backend, "value") else str(runtime_info.backend)
                info["server_name"] = runtime_info.server_name
            elif hasattr(tool, "backend_type"):
                info["backend"] = tool.backend_type.value if hasattr(tool.backend_type, "value") else str(tool.backend_type)
            tool_info.append(info)
        
        # Build metadata
        metadata = {
            "instruction": task_instruction[:500],  # Truncate long instructions
            "count": len(tools),
            "tools": tool_info,
        }
        
        # Add search debug info if available
        if search_debug_info:
            metadata["search_debug"] = {
                "search_mode": search_debug_info.get("search_mode", ""),
                "total_candidates": search_debug_info.get("total_candidates", 0),
                "mcp_count": search_debug_info.get("mcp_count", 0),
                "non_mcp_count": search_debug_info.get("non_mcp_count", 0),
                "llm_filter": search_debug_info.get("llm_filter", {}),
                "tool_scores": search_debug_info.get("tool_scores", []),
            }
        
        # Save to metadata
        await instance._recorder.add_metadata("retrieved_tools", metadata)
        
        logger.info(f"Recorded {len(tools)} retrieved tools (with search debug info: {search_debug_info is not None})")
    
    @classmethod
    async def record_skill_selection(
        cls,
        selection_record: Dict[str, Any],
    ):
        """
        Record skill selection decision to metadata.json.
        
        This captures the pre-execution skill matching conversation:
        - Which skills were available
        - The LLM prompt and response (or keyword fallback)
        - Which skills were selected
        
        Args:
            selection_record: Structured record from SkillRegistry.select_skills_with_llm()
                Keys: method, task, available_skills, prompt, llm_response, selected, error
        """
        instance = cls._global_instance
        if not instance or not instance._is_started or not instance._recorder:
            return
        
        # Save to metadata alongside retrieved_tools
        await instance._recorder.add_metadata("skill_selection", selection_record)
        
        selected = selection_record.get("selected", [])
        method = selection_record.get("method", "unknown")
        logger.info(
            f"Recorded skill selection: {len(selected)} selected via {method} "
            f"(from {len(selection_record.get('available_skills', []))} available)"
        )
    
    @staticmethod
    def _truncate_messages(
        messages: List[Dict[str, Any]],
        max_content_length: int = 5000,
    ) -> List[Dict[str, Any]]:
        """Truncate message content to avoid huge log files."""
        result = []
        for msg in messages:
            new_msg = {"role": msg.get("role", "unknown")}
            content = msg.get("content", "")

            if isinstance(content, str):
                if len(content) > max_content_length:
                    new_msg["content"] = content[:max_content_length] + f"... [truncated, total {len(content)} chars]"
                else:
                    new_msg["content"] = content
            elif isinstance(content, list):
                # Handle multi-part content (e.g., with images)
                new_content = []
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "image":
                            new_content.append({"type": "image", "note": "[image data omitted]"})
                        elif item.get("type") == "text":
                            text = item.get("text", "")
                            if len(text) > max_content_length:
                                new_content.append({
                                    "type": "text",
                                    "text": text[:max_content_length] + f"... [truncated, total {len(text)} chars]"
                                })
                            else:
                                new_content.append(item)
                        else:
                            new_content.append(item)
                    else:
                        new_content.append(item)
                new_msg["content"] = new_content
            else:
                new_msg["content"] = str(content)[:max_content_length]

            if "tool_calls" in msg:
                new_msg["tool_calls"] = msg["tool_calls"]

            result.append(new_msg)
        return result

    @classmethod
    async def record_conversation_setup(
        cls,
        setup_messages: List[Dict[str, Any]],
        tools: Optional[List] = None,
        max_content_length: int = 5000,
        agent_name: str = "GroundingAgent",
        extra: Optional[Dict[str, Any]] = None,
    ):
        """
        Record initial conversation context to conversations.jsonl (called once before iterations).

        Writes a ``type: "setup"`` line containing all system messages, the user
        instruction, **and** the tool schemas exposed to the LLM so the log
        gives a complete picture of what the model sees.

        Args:
            setup_messages: The initial messages list (system prompts + user instruction).
            tools: BaseTool list passed to the LLM (optional).  Each tool's
                   name, backend, and description are recorded.
            max_content_length: Max length for message content truncation.
            agent_name: Agent/phase identifier. Used to distinguish conversations
                from different pipeline stages during replay.
                Common values: "GroundingAgent", "ExecutionAnalyzer",
                "SkillEvolver", "SkillEvolver.confirm", "SkillEvolver.retry".
            extra: Optional dict of additional context (e.g. evolution_type,
                trigger, target_skills) merged into the record.
        """
        instance = cls._global_instance
        if not instance or not instance._is_started or not instance._recorder:
            return
        if not getattr(instance, 'enable_conversation_log', True):
            return

        record: Dict[str, Any] = {
            "type": "setup",
            "agent_name": agent_name,
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "messages": cls._truncate_messages(setup_messages, max_content_length),
        }
        if extra:
            record["extra"] = extra

        # Record tool definitions so the log shows what the LLM can call.
        # Description includes the [Backend] tag that the LLM actually sees.
        if tools:
            _BACKEND_LABELS = {
                "mcp": "MCP", "shell": "Shell", "gui": "GUI",
                "web": "Web", "system": "System",
            }
            tool_defs = []
            for t in tools:
                schema = getattr(t, "schema", None)
                if schema:
                    backend_val = getattr(schema, "backend_type", None)
                    backend_str = (
                        backend_val.value
                        if hasattr(backend_val, "value")
                        else str(backend_val) if backend_val else None
                    )
                    entry: Dict[str, Any] = {
                        "name": schema.name,
                        "backend": backend_str,
                    }
                    if schema.description:
                        desc = schema.description
                        # Mirror the [Backend] tag that _prepare_tools_for_llmclient
                        # adds so the recording matches what the LLM sees.
                        if backend_str and backend_str not in ("not_set",):
                            label = _BACKEND_LABELS.get(backend_str, backend_str)
                            desc = f"[{label}] {desc}"
                        if len(desc) > 200:
                            desc = desc[:200] + "..."
                        entry["description"] = desc
                else:
                    entry = {"name": getattr(t, "name", str(t))}
                tool_defs.append(entry)
            record["tools"] = tool_defs

        conv_file = instance._recorder.trajectory_dir / "conversations.jsonl"
        try:
            with open(conv_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False))
                f.write("\n")
        except Exception as e:
            logger.debug(f"Failed to write conversation setup: {e}")

    @classmethod
    async def record_iteration_context(
        cls,
        iteration: int,
        delta_messages: List[Dict[str, Any]],
        response_metadata: Dict[str, Any],
        max_content_length: int = 5000,
        agent_name: str = "GroundingAgent",
        extra: Optional[Dict[str, Any]] = None,
    ):
        """
        Record a single iteration's delta messages to conversations.jsonl.

        Only the messages produced during this iteration are stored (assistant
        response, tool results, inter-iteration guidance), avoiding repetition
        of system prompts and initial user instruction.  The initial context is
        stored once via ``record_conversation_setup``.  The full conversation
        can be reconstructed by concatenating the setup with all deltas in order.

        Args:
            iteration: Iteration number (1-based).
            delta_messages: Messages added during this iteration (assistant + tool results).
            response_metadata: Lightweight metadata about the LLM response
                (has_tool_calls, tool_calls_count).
            max_content_length: Max length for message content truncation.
            agent_name: Agent/phase identifier (must match the corresponding
                ``record_conversation_setup`` call).
            extra: Optional dict of additional context merged into the record.
        """
        instance = cls._global_instance
        if not instance or not instance._is_started or not instance._recorder:
            return
        if not getattr(instance, 'enable_conversation_log', True):
            return

        record = {
            "type": "iteration",
            "agent_name": agent_name,
            "iteration": iteration,
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "response_metadata": response_metadata,
            "delta_messages": cls._truncate_messages(delta_messages, max_content_length),
        }
        if extra:
            record["extra"] = extra

        # Append to conversations.jsonl (real-time)
        conv_file = instance._recorder.trajectory_dir / "conversations.jsonl"
        try:
            with open(conv_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False))
                f.write("\n")
        except Exception as e:
            logger.debug(f"Failed to write conversation log: {e}")
    
    @classmethod
    async def record_tool_execution(
        cls,
        tool_name: str,
        backend: str,
        parameters: Dict[str, Any],
        result: Any,
        server_name: Optional[str] = None,
        is_success: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """
        Record tool execution (internal method, called by BaseTool automatically)
        
        Args:
            tool_name: Name of the tool
            backend: Backend type (gui, shell, mcp, etc.)
            parameters: Tool parameters
            result: Tool execution result (content or error message)
            server_name: Server name for MCP backend
            is_success: Whether the tool execution was successful (default: True for backward compatibility)
            metadata: Tool result metadata (e.g. intermediate_steps for GUI)
        """
        if not cls._global_instance or not cls._global_instance._is_started:
            return
        
        instance = cls._global_instance
        
        # Infer backend if not_set or not in allowed backends
        if backend == "not_set" or backend not in instance.backends:
            inferred = cls._infer_backend_from_tool_name(tool_name)
            if inferred and inferred in instance.backends:
                backend = inferred
            elif backend not in instance.backends:
                logger.debug(
                    f"Backend '{backend}' not in recording backends {instance.backends}, "
                    f"skipping recording for tool '{tool_name}'"
                )
                return
        
        # Create mock tool_call and result objects for compatibility with existing _record_* methods
        class MockFunctionCall:
            def __init__(self, name, arguments):
                self.name = name
                self.arguments = arguments
        
        class MockToolCall:
            def __init__(self, name, arguments):
                self.function = MockFunctionCall(name, arguments)
        
        class MockResult:
            def __init__(self, content, is_success=True, metadata=None):
                self.content = content
                self.is_success = is_success
                self.is_error = not is_success
                self.error = content if not is_success else None
                self.metadata = metadata or {}
        
        tool_call = MockToolCall(tool_name, parameters)
        mock_result = MockResult(result, is_success=is_success, metadata=metadata)
        
        try:
            if backend == "mcp":
                server = server_name or "unknown"
                await instance._record_mcp(tool_call, mock_result, server)
            elif backend == "gui":
                await instance._record_gui(tool_call, mock_result)
            elif backend == "shell":
                await instance._record_shell(tool_call, mock_result)
            elif backend == "system":
                await instance._record_system(tool_call, mock_result)
            elif backend == "web":
                await instance._record_web(tool_call, mock_result)
            else:
                logger.warning(f"No recording handler for backend '{backend}', tool '{tool_name}'")
                return
            
            instance._step_counter += 1
        except Exception as e:
            logger.warning(f"Failed to record tool execution for {tool_name}: {e}")
    
    @staticmethod
    def _parse_arguments(arg_data):
        """Safely parse tool_call.function.arguments which may be JSON string.

        Handles:
        1. Proper JSON strings with true/false/null
        2. Python literal strings (produced by OpenAI) using ast.literal_eval
        3. Already-dict objects (returned by SDK)
        """
        if not isinstance(arg_data, str):
            return arg_data or {}

        # First, try JSON
        try:
            return json.loads(arg_data)
        except json.JSONDecodeError:
            pass

        # Fallback to Python literal
        try:
            return ast.literal_eval(arg_data)
        except Exception:
            logger.debug("Failed to parse arguments, returning raw string")
            return {"raw": arg_data}
    
    async def start(self, task_id: Optional[str] = None):
        """Start automatic recording
        Args:
            task_id: If provided, override the current task_id for this recording session. This allows
                     external callers (e.g. Coordinator) to specify a meaningful task identifier without
                     having to recreate the RecordingManager instance.
        """
        # Allow dynamic update of task_id before recording actually starts
        if task_id:
            self.task_id = task_id
        if not self.enabled or self._is_started:
            return
        
        try:
            # check server availability (only when video or screenshot is enabled)
            if self.enable_video or self.enable_screenshot:
                await self._check_server_availability()
            
            self._recorder = TrajectoryRecorder(
                task_name=self.task_id,
                log_dir=self.log_dir,
                enable_screenshot=self.enable_screenshot,
                enable_video=self.enable_video,
                server_url=self.server_url,
            )
            
            # create action recorder for agent decision tracking
            self._action_recorder = ActionRecorder(
                trajectory_dir=Path(self._recorder.get_trajectory_dir())
            )
            
            
            # create video client (internal management)
            if self.enable_video:
                from openspace.platforms import RecordingClient
                self._recording_client = RecordingClient(base_url=self.server_url)
                success = await self._recording_client.start_recording()
                if success:
                    logger.info("Video recording started")
                else:
                    logger.warning("Video recording failed to start")
            
            # create screenshot client (internal management)
            if self.enable_screenshot:
                from openspace.platforms import ScreenshotClient
                self._screenshot_client = ScreenshotClient(base_url=self.server_url)
                logger.debug("Screenshot client ready")
            
            # save initial metadata
            await self._recorder.add_metadata("task_id", self.task_id)
            await self._recorder.add_metadata("backends", list(self.backends))
            await self._recorder.add_metadata("start_time", datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))

            # Capture and save initial screenshot if enabled
            if self.enable_screenshot and self._screenshot_client:
                try:
                    init_shot = await self._screenshot_client.capture()
                    if init_shot:
                        await self._recorder.save_init_screenshot(init_shot)
                        logger.debug("Initial screenshot saved")
                except Exception as e:
                    logger.debug(f"Failed to capture initial screenshot: {e}")
            
            self._is_started = True
            logger.info(f"Recording started: {self._recorder.get_trajectory_dir()}")
            
        except Exception as e:
            logger.error(f"Recording failed to start: {e}")
            raise
    
    async def _check_server_availability(self):
        """Check if local server is available"""
        try:
            from openspace.platforms import SystemInfoClient

            # Use context manager to ensure aiohttp session is closed, avoiding warning of unclosed session
            async with SystemInfoClient(base_url=self.server_url) as client:
                info = await client.get_system_info()

            if info:
                logger.info(f"Server connected ({info.get('platform', 'unknown')})")
            else:
                logger.warning("Server not responding, video/screenshot functionality unavailable")
        
        except Exception:
            logger.warning("Cannot connect to server, video/screenshot functionality unavailable")
    
    async def save_execution_outcome(
        self,
        status: str,
        iterations: int,
        execution_time: float = 0,
    ) -> None:
        """Persist task-level execution outcome into metadata.json.

        Should be called **before** ``stop()`` so the data is included in the
        finalized recording.  The saved dict has the structure::

            {"status": "success"|"incomplete"|"error",
             "iterations": int,
             "execution_time": float}
        """
        if self._recorder:
            await self._recorder.add_metadata("execution_outcome", {
                "status": status,
                "iterations": iterations,
                "execution_time": round(execution_time, 2),
            })

    async def stop(self):
        """Stop automatic recording"""
        if not self.enabled or not self._is_started:
            return
        
        try:
            # stop video recording and save
            if self._recording_client:
                try:
                    video_path = None
                    if self._recorder:
                        video_path = str(Path(self._recorder.get_trajectory_dir()) / "screen_recording.mp4")
                    
                    video_bytes = await self._recording_client.end_recording(dest=video_path)
                    if video_bytes and video_path:
                        video_size_mb = len(video_bytes) / (1024 * 1024)
                        logger.info(f"Video recording saved: {video_path} ({video_size_mb:.2f} MB)")
                except Exception as e:
                    logger.warning(f"Video recording failed to save: {e}")

            # close RecordingClient session, avoid unclosed session warning
            try:
                if self._recording_client:
                    await self._recording_client.close()
            except Exception as e:
                logger.debug(f"Failed to close RecordingClient session: {e}")
            
            # close screenshot client
            if self._screenshot_client:
                try:
                    await self._screenshot_client.close()
                except Exception as e:
                    logger.debug(f"Screenshot client failed to close: {e}")
                finally:
                    self._screenshot_client = None
            
            # finalize trajectory recording
            if self._recorder:
                # save final metadata
                await self._recorder.add_metadata("end_time", datetime.datetime.now().isoformat())
                await self._recorder.add_metadata("total_steps", self._step_counter)
                
                # generate summary
                await self.generate_summary()
                
                # finalize recording
                await self._recorder.finalize()
                
                logger.info(f"Recording completed: {self._recorder.get_trajectory_dir()}")
            
            # Restore original methods for registered LLM clients
            for client in self._registered_llm_clients:
                client_id = id(client)
                if client_id in self._original_methods:
                    try:
                        original_method = self._original_methods[client_id]
                        client.complete = original_method
                    except Exception as e:
                        logger.debug(f"Failed to restore original method for LLM client: {e}")
            self._registered_llm_clients.clear()
            self._original_methods.clear()
            
            self._is_started = False
            self._recorder = None
            self._action_recorder = None
            
        except Exception as e:
            logger.error(f"Recording failed to stop: {e}")
    
    def register_to_llm(self, llm_client):
        """Register LLM client: wrap complete() to record tool results (Path B, aligned with AnyTool)."""
        if not self.enabled:
            return
        if id(llm_client) in self._original_methods:
            return
        original_complete = llm_client.complete
        self._original_methods[id(llm_client)] = original_complete
        
        async def wrapped_complete(self_client, *args, **kwargs):
            response = await original_complete(*args, **kwargs)
            if response.get("tool_results"):
                await self._auto_record_tool_results(response["tool_results"])
            return response
        
        llm_client.complete = types.MethodType(wrapped_complete, llm_client)
        self._registered_llm_clients.append(llm_client)
    
    @staticmethod
    def _infer_backend_from_tool_name(tool_name: str) -> Optional[str]:
        """Infer backend from tool name when tool_results lack backend."""
        if not tool_name or not isinstance(tool_name, str):
            return None
        name = tool_name.strip()
        # Use rsplit to handle server names that themselves contain "__".
        if "__" in name:
            name = name.rsplit("__", 1)[-1]
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
    
    async def _auto_record_tool_results(self, tool_results: List[Dict]):
        """Record tool execution results from LLM complete() (Path B, aligned with AnyTool)."""
        if not self._recorder or not self._is_started:
            return
        for tool_result in tool_results:
            tool_call = tool_result.get("tool_call")
            result = tool_result.get("result")
            backend = tool_result.get("backend")
            server_name = tool_result.get("server_name")
            
            if not tool_call or not result:
                continue
            if not backend:
                _name = getattr(getattr(tool_call, "function", None), "name", None) or str(tool_result.get("tool_call", ""))
                backend = self._infer_backend_from_tool_name(_name)
                if not backend:
                    logger.warning(f"Tool result missing 'backend', cannot infer for '{_name}', skipping")
                    continue
            
            result_metadata = result.metadata if hasattr(result, 'metadata') else None
            await RecordingManager.record_tool_execution(
                tool_name=tool_call.function.name,
                backend=backend,
                parameters=self._parse_arguments(tool_call.function.arguments),
                result=result.content if hasattr(result, 'content') else str(result),
                server_name=server_name,
                is_success=result.is_success if hasattr(result, 'is_success') else True,
                metadata=result_metadata,
            )
    
    async def _record_mcp(self, tool_call, result, server: str):
        tool_name = tool_call.function.name
        parameters = self._parse_arguments(tool_call.function.arguments)
        
        command = f"{server}.{tool_name}"
        result_str = str(result.content) if result.is_success else str(result.error)
        result_brief = result_str[:200] + "..." if len(result_str) > 200 else result_str
        
        is_actual_success = result.is_success and not result_str.startswith("ERROR:")
        
        step_info = await self._recorder.record_step(
            backend="mcp",
            tool=tool_name,
            command=command,
            result={
                "status": "success" if is_actual_success else "error",
                "output": result_brief,
            },
            parameters=parameters,
            extra={
                "server": server,
            },
            auto_screenshot=self.enable_screenshot
        )
        
        # Add agent_name to step_info
        step_info["agent_name"] = self.agent_name
    
    async def _record_gui(self, tool_call, result):
        tool_name = tool_call.function.name
        parameters = self._parse_arguments(tool_call.function.arguments)
        
        # Extract actual pyautogui command (from action_history)
        command = "gui_agent"
        if result.is_success and hasattr(result, 'metadata') and result.metadata:
            action_history = result.metadata.get("action_history", [])
            if action_history:
                # Get last successful execution action
                for action in reversed(action_history):
                    planned_action = action.get("planned_action", {})
                    execution_result = action.get("execution_result", {})
                    
                    if planned_action.get("action_type") == "PYAUTOGUI_COMMAND":
                        cmd = planned_action.get("command", "")
                        if cmd and execution_result.get("status") == "success":
                            command = cmd
                            break
                    elif execution_result.get("status") == "success":
                        action_type = planned_action.get("action_type", "")
                        if action_type and action_type not in ["WAIT", "DONE", "FAIL"]:
                            params = planned_action.get("parameters", {})
                            if params:
                                param_str = ", ".join([f"{k}={v}" for k, v in list(params.items())[:2]])
                                command = f"{action_type}({param_str})"
                            else:
                                command = action_type
                            break
        
        result_str = str(result.content) if result.is_success else str(result.error)
        
        is_actual_success = result.is_success
        if result.is_success:
            first_200_chars = result_str[:200] if result_str else ""
            critical_failure_patterns = ["Task failed", "CRITICAL ERROR:", "FATAL:"]
            has_critical_failure = any(pattern in first_200_chars for pattern in critical_failure_patterns)
            is_actual_success = not has_critical_failure
        
        # Extract intermediate_steps from metadata for embedding in traj.jsonl
        extra = {}
        if hasattr(result, 'metadata') and result.metadata:
            intermediate_steps = result.metadata.get("intermediate_steps")
            if intermediate_steps:
                extra["intermediate_steps"] = intermediate_steps
        
        step_info = await self._recorder.record_step(
            backend="gui",
            tool="gui_agent",
            command=command,
            result={
                "status": "success" if is_actual_success else "error",
                "output": result_str,
            },
            parameters=parameters,
            auto_screenshot=self.enable_screenshot,
            extra=extra if extra else None,
        )
        
        step_info["agent_name"] = self.agent_name
    
    async def _record_shell(self, tool_call, result):
        tool_name = tool_call.function.name
        parameters = self._parse_arguments(tool_call.function.arguments)
        
        task = parameters.get("task", tool_name)
        exit_code = 0 if result.is_success else 1
        
        stdout = str(result.content) if result.is_success else ""
        stderr = str(result.error) if result.is_error else ""
        
        command = task  
        if hasattr(result, 'metadata') and result.metadata:
            code_history = result.metadata.get("code_history", [])
            if code_history:
                # Try to find the last successful execution
                found_success = False
                for code_info in reversed(code_history):
                    if code_info.get("status") == "success":
                        lang = code_info.get("lang", "bash")
                        code = code_info.get("code", "")
                        # String format code block: ```lang\ncode\n```
                        command = f"```{lang}\n{code}\n```"
                        found_success = True
                        break
                
                # If no successful execution found, use last code block
                if not found_success and code_history:
                    last_code = code_history[-1]
                    lang = last_code.get("lang", "bash")
                    code = last_code.get("code", "")
                    command = f"```{lang}\n{code}\n```"
        
        stdout_brief = stdout[:200] + "..." if len(stdout) > 200 else stdout
        stderr_brief = stderr[:200] + "..." if len(stderr) > 200 else stderr
        
        is_actual_success = result.is_success
        if result.is_success:
            first_500_chars = stdout[:500] if stdout else ""
            critical_failure_patterns = [
                "Task failed after",
                "[TASK_FAILED:",
                "EXECUTION ERROR",
                "timed out",
            ]
            has_critical_failure = any(pattern in first_500_chars for pattern in critical_failure_patterns)
            is_actual_success = not has_critical_failure
        
        step_info = await self._recorder.record_step(
            backend="shell",
            tool="shell_agent",
            command=command,
            result={
                "status": "success" if is_actual_success else "error",
                "exit_code": exit_code,
                "stdout": stdout_brief,
                "stderr": stderr_brief,
            },
            auto_screenshot=self.enable_screenshot
        )
        
        step_info["agent_name"] = self.agent_name
    
    async def _record_system(self, tool_call, result):
        tool_name = tool_call.function.name
        parameters = self._parse_arguments(tool_call.function.arguments)
        
        command = tool_name
        if parameters:
            key_params = []
            for key in ['path', 'file', 'directory', 'name', 'provider', 'backend']:
                if key in parameters and parameters[key]:
                    key_params.append(f"{parameters[key]}")
            if key_params:
                command = f"{tool_name}({', '.join(key_params[:2])})"
        
        result_str = str(result.content) if result.is_success else str(result.error)
        result_brief = result_str[:200] + "..." if len(result_str) > 200 else result_str
        
        is_actual_success = result.is_success
        if result.is_success and result_str:
            is_actual_success = not result_str.startswith("ERROR:")
        
        step_info = await self._recorder.record_step(
            backend="system",
            tool=tool_name,
            command=command,
            result={
                "status": "success" if is_actual_success else "error",
                "output": result_brief,
            },
            auto_screenshot=self.enable_screenshot
        )
        
        step_info["agent_name"] = self.agent_name
    
    async def _record_web(self, tool_call, result):
        tool_name = tool_call.function.name
        parameters = self._parse_arguments(tool_call.function.arguments)
        
        query = parameters.get("query", "")
        command = query if query else "deep_research"
        
        result_str = str(result.content) if result.is_success else str(result.error)
        
        is_actual_success = result.is_success
        if result.is_success and result_str:
            is_actual_success = not result_str.startswith("ERROR:")
        
        step_info = await self._recorder.record_step(
            backend="web",
            tool="deep_research_agent",
            command=command,
            result={
                "status": "success" if is_actual_success else "error",
                "output": result_str,  # Full output preserved for training/replay
            },
            auto_screenshot=self.enable_screenshot
        )
        
        # Add agent_name to step_info
        step_info["agent_name"] = self.agent_name
    
    async def add_metadata(self, key: str, value: Any):
        if self._recorder:
            await self._recorder.add_metadata(key, value)
    
    async def save_plan(self, plan: Dict[str, Any], agent_name: str = "GroundingAgent"):
        """
        Save agent plan to recording directory.
        This integrates planning information with execution trajectory.
        
        Args:
            plan: The plan data (usually containing task_updates or plan steps)
            agent_name: Name of the agent creating the plan
        """
        if not self._recorder or not self._is_started:
            logger.warning("Cannot save plan: recording not started")
            return
        
        try:
            plan_dir = Path(self._recorder.get_trajectory_dir()) / "plans"
            plan_dir.mkdir(exist_ok=True)
            
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            plan_data = {
                "version": timestamp,
                "created_at": datetime.datetime.now().isoformat(),
                "created_by": agent_name,
                "plan": plan
            }
            
            # Save versioned plan
            plan_file = plan_dir / f"plan_{timestamp}.json"
            with open(plan_file, 'w', encoding='utf-8') as f:
                json.dump(plan_data, f, indent=2, ensure_ascii=False)
            
            # Save current plan (latest)
            current_plan_file = plan_dir / "current_plan.json"
            with open(current_plan_file, 'w', encoding='utf-8') as f:
                json.dump(plan_data, f, indent=2, ensure_ascii=False)
            
            logger.debug(f"Saved plan to recording: {plan_file.name}")
        except Exception as e:
            logger.error(f"Failed to save plan: {e}")
    
    async def log_decision(
        self, 
        agent_name: str, 
        decision: str, 
        context: Optional[Dict[str, Any]] = None
    ):
        """
        Log agent decision with optional context.
        This provides insight into agent reasoning process.
        
        Args:
            agent_name: Name of the agent making the decision
            decision: Description of the decision
            context: Additional context information
        """
        if not self._recorder or not self._is_started:
            logger.warning("Cannot log decision: recording not started")
            return
        
        try:
            traj_dir = Path(self._recorder.get_trajectory_dir())
            log_file = traj_dir / "decisions.log"
            
            timestamp = datetime.datetime.now().isoformat()
            log_entry = f"[{timestamp}] {agent_name}: {decision}"
            if context:
                log_entry += f"\n  Context: {json.dumps(context, ensure_ascii=False)}"
            log_entry += "\n"
            
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(log_entry)
            
            logger.debug(f"Logged decision from {agent_name}")
        except Exception as e:
            logger.error(f"Failed to log decision: {e}")
    
    async def record_agent_action(
        self,
        agent_name: str,
        action_type: str,
        input_data: Optional[Dict[str, Any]] = None,
        reasoning: Optional[Dict[str, Any]] = None,
        output_data: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        related_tool_steps: Optional[list] = None,
        correlation_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Record an agent's action and decision-making process.
        
        Args:
            agent_name: Name of the agent performing the action
            action_type: Type of action (plan | execute | evaluate | monitor)
            input_data: Input data the agent received (simplified)
            reasoning: Agent's reasoning process (structured)
            output_data: Agent's output/decision (structured)
            metadata: Additional metadata (LLM model, tokens, duration, etc.)
            related_tool_steps: List of tool execution step numbers related to this action
            correlation_id: Optional correlation ID to link related events
            
        Returns:
            The recorded action info, or None if recording not started
        """
        if not self._action_recorder or not self._is_started:
            logger.debug("Cannot record agent action: recording not started")
            return None
        
        try:
            action_info = await self._action_recorder.record_action(
                agent_name=agent_name,
                action_type=action_type,
                input_data=input_data,
                reasoning=reasoning,
                output_data=output_data,
                metadata=metadata,
                related_tool_steps=related_tool_steps,
                correlation_id=correlation_id,
            )
            
            logger.debug(f"Recorded agent action: {agent_name} - {action_type}")
            return action_info
            
        except Exception as e:
            logger.error(f"Failed to record agent action: {e}")
            return None
    
    async def generate_summary(self) -> Dict[str, Any]:
        """
        Generate a comprehensive summary of the recording session.
        """
        if not self._recorder or not self._is_started:
            logger.warning("Cannot generate summary: recording not started")
            return {}
        
        try:
            from .action_recorder import load_agent_actions, analyze_agent_actions
            from .utils import load_trajectory_from_jsonl, analyze_trajectory
            
            traj_dir = self._recorder.get_trajectory_dir()
            
            # Load all recorded data
            trajectory = load_trajectory_from_jsonl(f"{traj_dir}/traj.jsonl")
            agent_actions = load_agent_actions(traj_dir)
            
            # Analyze data
            traj_stats = analyze_trajectory(trajectory)
            action_stats = analyze_agent_actions(agent_actions)
            
            # Build summary
            summary = {
                "task_id": self.task_id,
                "start_time": self._recorder.metadata.get("start_time", ""),
                "end_time": self._recorder.metadata.get("end_time", ""),
                "trajectory": {
                    "total_steps": traj_stats.get("total_steps", 0),
                    "success_count": traj_stats.get("success_count", 0),
                    "success_rate": traj_stats.get("success_rate", 0),
                    "by_backend": traj_stats.get("backends", {}),
                    "by_tool": traj_stats.get("tools", {}),
                },
                "agent_actions": {
                    "total_actions": action_stats.get("total_actions", 0),
                    "by_agent": action_stats.get("by_agent", {}),
                    "by_type": action_stats.get("by_type", {}),
                }
            }
            
            # Save summary to file
            summary_file = Path(traj_dir) / "summary.json"
            with open(summary_file, 'w', encoding='utf-8') as f:
                json.dump(summary, f, indent=2, ensure_ascii=False)
            
            logger.info(f"Generated summary: {summary_file}")
            return summary
            
        except Exception as e:
            logger.error(f"Failed to generate summary: {e}")
            return {}
    
    async def __aenter__(self):
        await self.start()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop()
        return False
    
    @property
    def recording_status(self) -> bool:
        return self._is_started
    
    @property
    def trajectory_dir(self) -> Optional[str]:
        if self._recorder:
            return str(self._recorder.get_trajectory_dir())
        return None
    
    @property
    def recording_client(self):
        return self._recording_client
    
    @property
    def screenshot_client(self):
        return self._screenshot_client
    
    @property
    def step_count(self) -> int:
        """Get current step count"""
        return self._step_counter


__all__ = [
    'RecordingManager',
]