from __future__ import annotations

import copy
import json
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from openspace.agents.base import BaseAgent
from openspace.grounding.core.types import BackendType, ToolResult
from openspace.platforms.screenshot import ScreenshotClient
from openspace.prompts import GroundingAgentPrompts
from openspace.utils.logging import Logger

if TYPE_CHECKING:
    from openspace.llm import LLMClient
    from openspace.grounding.core.grounding_client import GroundingClient
    from openspace.recording import RecordingManager
    from openspace.skill_engine import SkillRegistry

logger = Logger.get_logger(__name__)


class GroundingAgent(BaseAgent):
    def __init__(
        self,
        name: str = "GroundingAgent",
        backend_scope: Optional[List[str]] = None,
        llm_client: Optional[LLMClient] = None,
        grounding_client: Optional[GroundingClient] = None,
        recording_manager: Optional[RecordingManager] = None,
        system_prompt: Optional[str] = None,
        max_iterations: int = 15,
        visual_analysis_timeout: float = 30.0,
        tool_retrieval_llm: Optional[LLMClient] = None,
        visual_analysis_model: Optional[str] = None,
    ) -> None:
        """
        Initialize the Grounding Agent.
        
        Args:
            name: Agent name
            backend_scope: List of backends this agent can access (None = all available)
            llm_client: LLM client for reasoning
            grounding_client: GroundingClient for tool execution
            recording_manager: RecordingManager for recording execution
            system_prompt: Custom system prompt
            max_iterations: Maximum LLM reasoning iterations for self-correction
            visual_analysis_timeout: Timeout for visual analysis LLM calls in seconds
            tool_retrieval_llm: LLM client for tool retrieval filter (None = use llm_client)
            visual_analysis_model: Model name for visual analysis (None = use llm_client.model)
        """
        super().__init__(
            name=name,
            backend_scope=backend_scope or ["gui", "shell", "mcp", "web", "system"],
            llm_client=llm_client,
            grounding_client=grounding_client,
            recording_manager=recording_manager
        )
       
        self._system_prompt = system_prompt or self._default_system_prompt()
        self._max_iterations = max_iterations
        self._visual_analysis_timeout = visual_analysis_timeout
        self._tool_retrieval_llm = tool_retrieval_llm
        self._visual_analysis_model = visual_analysis_model
        
        # Skill context injection (set externally before process())
        self._skill_context: Optional[str] = None
        self._active_skill_ids: List[str] = []

        # Skill registry for mid-iteration retrieve_skill tool
        self._skill_registry: Optional["SkillRegistry"] = None

        # Tools from the last execution (available for post-execution analysis)
        self._last_tools: List = []
        
        logger.info(f"Grounding Agent initialized: {name}")
        logger.info(f"Backend scope: {self._backend_scope}")
        logger.info(f"Max iterations: {self._max_iterations}")
        logger.info(f"Visual analysis timeout: {self._visual_analysis_timeout}s")
        if tool_retrieval_llm:
            logger.info(f"Tool retrieval model: {tool_retrieval_llm.model}")
        if visual_analysis_model:
            logger.info(f"Visual analysis model: {visual_analysis_model}")

    def set_skill_context(
        self,
        context: str,
        skill_ids: Optional[List[str]] = None,
    ) -> None:
        """Inject skill guidance into the agent's system prompt.

        Called by ``OpenSpace.execute()`` before ``process()`` when skills
        are matched.  The context is a formatted string built by
        ``SkillRegistry.build_context_injection()``.

        Args:
            context: Formatted skill content for system prompt injection.
            skill_ids: skill_id values of injected skills.
        """
        self._skill_context = context if context else None
        self._active_skill_ids = skill_ids or []
        if self._skill_context:
            logger.info(f"Skill context set: {', '.join(self._active_skill_ids) or '(unnamed)'}")

    def clear_skill_context(self) -> None:
        """Remove skill guidance (used before fallback execution)."""
        if self._skill_context:
            logger.info(f"Skill context cleared (was: {', '.join(self._active_skill_ids)})")
        self._skill_context = None
        self._active_skill_ids = []

    @property
    def has_skill_context(self) -> bool:
        return self._skill_context is not None

    def set_skill_registry(self, registry: Optional["SkillRegistry"]) -> None:
        """Attach a SkillRegistry so the agent can offer ``retrieve_skill`` as a tool."""
        self._skill_registry = registry
        if registry:
            count = len(registry.list_skills())
            logger.info(f"Skill registry attached ({count} skill(s) available for mid-iteration retrieval)")

    _MAX_SINGLE_CONTENT_CHARS = 30_000
    _ITERATION_GUIDANCE_PREFIX = "[INTERNAL ORCHESTRATION NOTE]"

    @classmethod
    def _cap_message_content(cls, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Truncate oversized individual message contents in-place.

        Targets tool-result messages and assistant messages that can
        carry enormous file contents (read_file on large CSVs/scripts).
        System messages and the first user instruction are never touched.
        """
        cap = cls._MAX_SINGLE_CONTENT_CHARS
        trimmed = 0
        for msg in messages:
            content = msg.get("content")
            if not isinstance(content, str) or len(content) <= cap:
                continue
            if msg.get("role") == "system":
                continue
            original_len = len(content)
            msg["content"] = (
                content[: cap // 2]
                + f"\n\n... [truncated {original_len - cap:,} chars] ...\n\n"
                + content[-(cap // 2):]
            )
            trimmed += 1
        if trimmed:
            logger.info(f"Capped {trimmed} oversized message(s) to {cap:,} chars each")
        return messages

    def _truncate_messages(
        self, 
        messages: List[Dict[str, Any]], 
        keep_recent: int = 8,
        max_tokens_estimate: int = 120000
    ) -> List[Dict[str, Any]]:
        # First: cap any single oversized message to prevent one huge
        # tool-result from dominating the context window.
        messages = self._cap_message_content(messages)

        if len(messages) <= keep_recent + 2:  # +2 for system and initial user
            return messages
        
        total_text = json.dumps(messages, ensure_ascii=False)
        estimated_tokens = len(total_text) // 4
        
        if estimated_tokens < max_tokens_estimate:
            return messages
        
        logger.info(f"Truncating message history: {len(messages)} messages, "
                   f"~{estimated_tokens:,} tokens -> keeping recent {keep_recent} rounds")
        
        system_messages = []
        user_instruction = None
        conversation_messages = []
        
        for msg in messages:
            role = msg.get("role")
            if role == "system":
                system_messages.append(msg)
            elif role == "user" and user_instruction is None:
                user_instruction = msg
            else:
                conversation_messages.append(msg)
        
        recent_messages = conversation_messages[-(keep_recent * 2):] if conversation_messages else []
        
        truncated = system_messages.copy()
        if user_instruction:
            truncated.append(user_instruction)
        truncated.extend(recent_messages)
        
        logger.info(f"After truncation: {len(truncated)} messages, "
                   f"~{len(json.dumps(truncated, ensure_ascii=False))//4:,} tokens (estimated)")
        
        return truncated
    
    async def process(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process a task execution request with multi-round iteration control.
        """
        instruction = context.get("instruction", "")
        if not instruction:
            logger.error("Grounding Agent: No instruction provided")
            return {"error": "No instruction provided", "status": "error"}
        
        # Store current instruction for visual analysis context
        self._current_instruction = instruction
        
        logger.info(f"Grounding Agent: Processing instruction at step {self.step}")
        
        # Exist workspace files check
        workspace_info = await self._check_workspace_artifacts(context)
        if workspace_info["has_files"]:
            context["workspace_artifacts"] = workspace_info
            logger.info(f"Workspace has {len(workspace_info['files'])} existing files: {workspace_info['files']}")
        
        # Get available tools (auto-search with cap)
        tools = await self._get_available_tools(instruction)
        self._last_tools = tools  # expose for post-execution analysis
        
        # Get search debug info (similarity scores, LLM selections)
        search_debug_info = None
        if self.grounding_client:
            search_debug_info = self.grounding_client.get_last_search_debug_info()
        
        # Build retrieved tools list for return value
        retrieved_tools_list = []
        for tool in tools:
            tool_info = {
                "name": getattr(tool, "name", str(tool)),
                "description": getattr(tool, "description", ""),
            }
            # Prefer runtime_info.backend
            # over backend_type (may be NOT_SET for cached RemoteTools)
            runtime_info = getattr(tool, "_runtime_info", None)
            if runtime_info and hasattr(runtime_info, "backend"):
                tool_info["backend"] = runtime_info.backend.value if hasattr(runtime_info.backend, "value") else str(runtime_info.backend)
                tool_info["server_name"] = runtime_info.server_name
            elif hasattr(tool, "backend_type"):
                tool_info["backend"] = tool.backend_type.value if hasattr(tool.backend_type, "value") else str(tool.backend_type)
            
            # Add similarity score if available
            if search_debug_info and search_debug_info.get("tool_scores"):
                for score_info in search_debug_info["tool_scores"]:
                    if score_info["name"] == tool_info["name"]:
                        tool_info["similarity_score"] = score_info["score"]
                        break
            
            retrieved_tools_list.append(tool_info)
        
        # Record retrieved tools
        if self._recording_manager:
            from openspace.recording import RecordingManager
            await RecordingManager.record_retrieved_tools(
                task_instruction=instruction,
                tools=tools,
                search_debug_info=search_debug_info,
            )
        
        # Initialize iteration state
        max_iterations = context.get("max_iterations", self._max_iterations)
        current_iteration = 0
        all_tool_results = []
        iteration_contexts = []
        consecutive_empty_responses = 0  # Track consecutive empty LLM responses
        MAX_CONSECUTIVE_EMPTY = 5  # Exit after this many empty responses
        
        # Build initial messages
        messages = self.construct_messages(context)
        
        # Record initial conversation setup once (system prompts + user instruction + tool definitions)
        from openspace.recording import RecordingManager
        await RecordingManager.record_conversation_setup(
            setup_messages=copy.deepcopy(messages),
            tools=tools,
        )
        
        try:
            while current_iteration < max_iterations:
                current_iteration += 1
                logger.info(f"Grounding Agent: Iteration {current_iteration}/{max_iterations}")
                
                # Strip skill context after the first iteration to save prompt tokens.
                # Skills only need to guide the first LLM call; subsequent iterations
                # already have the plan and tool results in context.
                if current_iteration == 2 and self._skill_context:
                    skill_ctx = self._skill_context
                    messages = [
                        m for m in messages
                        if not (m.get("role") == "system" and m.get("content") == skill_ctx)
                    ]
                    logger.info("Skill context removed from messages after first iteration")

                # Cap oversized individual messages every iteration to prevent
                # a single huge tool result from ballooning all subsequent calls.
                if current_iteration >= 2:
                    messages = self._cap_message_content(messages)

                # Truncate message history to prevent context length issues
                # Start truncating after 5 iterations to keep context manageable
                if current_iteration >= 5:
                    messages = self._truncate_messages(
                        messages, 
                        keep_recent=8,
                        max_tokens_estimate=120000
                    )
                
                messages_input_snapshot = copy.deepcopy(messages)
                
                # [DISABLED] Iteration summary generation
                # Tool results (including visual analysis) are already in context,
                # LLM can make decisions directly without separate summary.
                # To re-enable, uncomment below and pass iteration_summary_prompt to complete()
                # iteration_summary_prompt = GroundingAgentPrompts.iteration_summary(
                #     instruction=instruction,
                #     iteration=current_iteration,
                #     max_iterations=max_iterations
                # ) if context.get("auto_execute", True) else None
                
                # Call LLMClient for single round
                # LLM will decide whether to call tools or finish with <COMPLETE>
                llm_response = await self._llm_client.complete(
                    messages=messages,
                    tools=tools if context.get("auto_execute", True) else None,
                    execute_tools=context.get("auto_execute", True),
                    summary_prompt=None,  # Disabled
                    tool_result_callback=self._visual_analysis_callback
                )
                
                # Update messages with LLM response
                messages = llm_response["messages"]
                
                # Collect tool results
                tool_results_this_iteration = llm_response.get("tool_results", [])
                if tool_results_this_iteration:
                    all_tool_results.extend(tool_results_this_iteration)

                # [DISABLED] Iteration summary logging
                # llm_summary = llm_response.get("iteration_summary")
                # if llm_summary:
                #     logger.info(f"Iteration {current_iteration} summary: {llm_summary[:150]}...")
                
                assistant_message = llm_response.get("message", {})
                assistant_content = assistant_message.get("content", "")
                
                has_tool_calls = llm_response.get('has_tool_calls', False)
                logger.info(f"Iteration {current_iteration} - Has tool calls: {has_tool_calls}, "
                          f"Tool results: {len(tool_results_this_iteration)}, "
                          f"Content length: {len(assistant_content)} chars")
                
                if len(assistant_content) > 0:
                    logger.info(f"Iteration {current_iteration} - Assistant content preview: {repr(assistant_content[:300])}")
                    consecutive_empty_responses = 0  # Reset counter on valid response
                else:
                    if not has_tool_calls:
                        consecutive_empty_responses += 1
                        logger.warning(f"Iteration {current_iteration} - NO tool calls and NO content "
                                     f"(empty response {consecutive_empty_responses}/{MAX_CONSECUTIVE_EMPTY})")
                        
                        if consecutive_empty_responses >= MAX_CONSECUTIVE_EMPTY:
                            logger.error(f"Exiting due to {MAX_CONSECUTIVE_EMPTY} consecutive empty LLM responses. "
                                       "This may indicate API issues, rate limiting, or context too long.")
                            break
                    else:
                        consecutive_empty_responses = 0  # Reset if we have tool calls
                
                # Snapshot messages after LLM call (accumulated context)
                messages_output_snapshot = copy.deepcopy(messages)
                
                # Delta messages: only the messages produced in this iteration
                # (avoids repeating system prompts / initial user instruction each time)
                delta_messages = messages[len(messages_input_snapshot):]
                
                # Response metadata (lightweight; full content lives in delta_messages)
                response_metadata = {
                    "has_tool_calls": has_tool_calls,
                    "tool_calls_count": len(tool_results_this_iteration),
                }
                iteration_context = {
                    "iteration": current_iteration,
                    "messages_input": messages_input_snapshot,
                    "messages_output": messages_output_snapshot,
                    "response_metadata": response_metadata,
                }
                iteration_contexts.append(iteration_context)
                
                # Real-time save to conversations.jsonl (delta only, no redundancy)
                await RecordingManager.record_iteration_context(
                    iteration=current_iteration,
                    delta_messages=copy.deepcopy(delta_messages),
                    response_metadata=response_metadata,
                )
                
                # Check for completion token in assistant content
                # [DISABLED] Also check in iteration summary when enabled
                # is_complete = (
                #     GroundingAgentPrompts.TASK_COMPLETE in assistant_content or
                #     (llm_summary and GroundingAgentPrompts.TASK_COMPLETE in llm_summary)
                # )
                is_complete = GroundingAgentPrompts.TASK_COMPLETE in assistant_content
                
                if is_complete:
                    # Task is complete - LLM generated completion token
                    logger.info(f"Task completed at iteration {current_iteration} (found {GroundingAgentPrompts.TASK_COMPLETE})")
                    break
                
                else:
                    # LLM didn't generate <COMPLETE>, continue to next iteration
                    if tool_results_this_iteration:
                        logger.debug(f"Task in progress, LLM called {len(tool_results_this_iteration)} tools")
                    else:
                        logger.debug(f"Task in progress, LLM did not generate <COMPLETE>")
                    
                    # Remove previous iteration guidance to avoid accumulation
                    messages = [
                        msg for msg in messages
                        if not (
                            isinstance(msg.get("content"), str)
                            and msg.get("content", "").startswith(self._ITERATION_GUIDANCE_PREFIX)
                        )
                    ]
                    
                    # MiniMax rejects system messages injected mid-conversation,
                    # so runtime guidance is sent as an internal user note.
                    guidance_msg = {
                        "role": "user",
                        "content": f"{self._ITERATION_GUIDANCE_PREFIX}\n"
                                   f"Iteration {current_iteration} complete. "
                                   f"Check if task is finished - if yes, output {GroundingAgentPrompts.TASK_COMPLETE}. "
                                   f"If not, continue with next action."
                    }
                    messages.append(guidance_msg)
                    
                    # [DISABLED] Full iteration feedback with summary
                    # self._remove_previous_guidance(messages)
                    # feedback_msg = self._build_iteration_feedback(
                    #     iteration=current_iteration,
                    #     llm_summary=llm_summary,
                    #     add_guidance=True
                    # )
                    # if feedback_msg:
                    #     messages.append(feedback_msg)
                    #     logger.debug(f"Added iteration {current_iteration} feedback with guidance")
                    
                    continue
            
            # Build final result
            result = await self._build_final_result(
                instruction=instruction,
                messages=messages,
                all_tool_results=all_tool_results,
                iterations=current_iteration,
                max_iterations=max_iterations,
                iteration_contexts=iteration_contexts,
                retrieved_tools_list=retrieved_tools_list,
                search_debug_info=search_debug_info,
            )
            
            # Record agent action to recording manager
            if self._recording_manager:
                await self._record_agent_execution(result, instruction)
            
            # Increment step
            self.increment_step()
            
            logger.info(f"Grounding Agent: Execution completed with status: {result.get('status')}")
            return result
            
        except Exception as e:
            logger.error(f"Grounding Agent: Execution failed: {e}")
            result = {
                "error": str(e),
                "status": "error",
                "instruction": instruction,
                "iteration": current_iteration
            }
            self.increment_step()
            return result
    
    def _default_system_prompt(self) -> str:
        """Default system prompt tailored to the agent's actual backend scope."""
        return GroundingAgentPrompts.build_system_prompt(self._backend_scope)

    def construct_messages(
        self,
        context: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        messages = [{"role": "system", "content": self._system_prompt}]
        
        # Get instruction from context
        instruction = context.get("instruction", "")
        if not instruction:
            raise ValueError("context must contain 'instruction' field")
        
        # Add workspace directory
        workspace_dir = context.get("workspace_dir")
        if workspace_dir:
            messages.append({
                "role": "system",
                "content": GroundingAgentPrompts.workspace_directory(workspace_dir)
            })
        
        # Add workspace artifacts information
        workspace_artifacts = context.get("workspace_artifacts")
        if workspace_artifacts and workspace_artifacts.get("has_files"):
            files = workspace_artifacts.get("files", [])
            matching_files = workspace_artifacts.get("matching_files", [])
            recent_files = workspace_artifacts.get("recent_files", [])
            
            if matching_files:
                artifact_msg = GroundingAgentPrompts.workspace_matching_files(matching_files)
            elif len(recent_files) >= 2:
                artifact_msg = GroundingAgentPrompts.workspace_recent_files(
                    total_files=len(files),
                    recent_files=recent_files
                )
            else:
                artifact_msg = GroundingAgentPrompts.workspace_file_list(files)
            
            messages.append({
                "role": "system",
                "content": artifact_msg
            })
        
        # Skill injection — only active (selected) skills, full content
        if self._skill_context:
            messages.append({
                "role": "system",
                "content": self._skill_context
            })
            logger.info(f"Injected active skill context ({len(self._active_skill_ids)} skill(s))")
        
        # User instruction
        messages.append({"role": "user", "content": instruction})
        
        return messages

    async def _get_available_tools(self, task_description: Optional[str]) -> List:
        """
        Retrieve tools for the current execution phase.

        Both skill-augmented and normal modes use the same
        ``get_tools_with_auto_search`` pipeline:
        - Non-MCP tools (shell, gui, web, system) are always included.
        - MCP tools are filtered by relevance only when their count
          exceeds ``max_tools``.

        When skills are active, the shell backend is guaranteed to be in
        scope (skills commonly reference ``shell_agent``).

        Falls back to returning all tools if anything fails.
        """
        grounding_client = self.grounding_client
        if not grounding_client:
            return []

        backends = [BackendType(name) for name in self._backend_scope]

        # Ensure shell backend is available when skills are active
        # (skills commonly reference shell_agent, read_file, etc.)
        if self.has_skill_context:
            shell_bt = BackendType.SHELL
            if shell_bt not in backends:
                backends = list(backends) + [shell_bt]
                logger.info("Added Shell backend to scope for skill file I/O")

        try:
            retrieval_llm = self._tool_retrieval_llm or self._llm_client
            tools = await grounding_client.get_tools_with_auto_search(
                task_description=task_description,
                backend=backends,
                use_cache=True,
                llm_callable=retrieval_llm,
            )
            logger.info(
                f"GroundingAgent selected {len(tools)} tools (auto-search) "
                f"from {len(backends)} backends"
                + (f" [skill-augmented]" if self.has_skill_context else "")
            )
        except Exception as e:
            logger.warning(f"Auto-search tools failed, falling back to full list: {e}")
            tools = await self._load_all_tools(grounding_client)

        # Append retrieve_skill tool when skill registry is available
        if self._skill_registry and self._skill_registry.list_skills():
            from openspace.skill_engine.retrieve_tool import RetrieveSkillTool
            retrieve_llm = self._tool_retrieval_llm or self._llm_client
            retrieve_tool = RetrieveSkillTool(
                self._skill_registry,
                backends=[b.value for b in backends],
                llm_client=retrieve_llm,
                skill_store=getattr(self, "_skill_store", None),
            )
            retrieve_tool.bind_runtime_info(
                backend=BackendType.SYSTEM,
                session_name="internal",
            )
            tools.append(retrieve_tool)
            logger.info("Added retrieve_skill tool for mid-iteration skill retrieval")

        return tools

    async def _load_all_tools(self, grounding_client: "GroundingClient") -> List:
        """Fallback: load all tools from all backends without search."""
        all_tools = []
        for backend_name in self._backend_scope:
            try:
                backend_type = BackendType(backend_name)
                tools = await grounding_client.list_tools(backend=backend_type)
                all_tools.extend(tools)
                logger.debug(f"Retrieved {len(tools)} tools from backend: {backend_name}")
            except Exception as e:
                logger.debug(f"Could not get tools from {backend_name}: {e}")

        logger.info(
            f"GroundingAgent fallback retrieved {len(all_tools)} tools "
            f"from {len(self._backend_scope)} backends"
        )
        return all_tools

    async def _visual_analysis_callback(
        self,
        result: ToolResult,
        tool_name: str,
        tool_call: Dict,
        backend: str
    ) -> ToolResult:
        """
        Callback for LLMClient to handle visual analysis after tool execution.
        """
        # 1. Check if LLM requested to skip visual analysis
        skip_visual_analysis = False
        try:
            arguments = tool_call.function.arguments
            if isinstance(arguments, str):
                args = json.loads(arguments.strip() or "{}")
            else:
                args = arguments
            
            if isinstance(args, dict) and args.get("skip_visual_analysis"):
                skip_visual_analysis = True
                logger.info(f"Visual analysis skipped for {tool_name} (meta-parameter set by LLM)")
        except Exception as e:
            logger.debug(f"Could not parse tool arguments: {e}")
        
        # 2. If skip requested, return original result
        if skip_visual_analysis:
            return result
        
        # 3. Check if this backend needs visual analysis
        if backend != "gui":
            return result
        
        # 4. Check if tool has visual data
        metadata = getattr(result, 'metadata', None)
        has_screenshots = metadata and (metadata.get("screenshot") or metadata.get("screenshots"))
        
        # 5. If no visual data, try to capture a screenshot
        if not has_screenshots:
            try:
                logger.info(f"No visual data from {tool_name}, capturing screenshot...")
                screenshot_client = ScreenshotClient()
                screenshot_bytes = await screenshot_client.capture()
                
                if screenshot_bytes:
                    # Add screenshot to result metadata
                    if metadata is None:
                        result.metadata = {}
                        metadata = result.metadata
                    metadata["screenshot"] = screenshot_bytes
                    has_screenshots = True
                    logger.info(f"Screenshot captured for visual analysis")
                else:
                    logger.warning("Failed to capture screenshot")
            except Exception as e:
                logger.warning(f"Error capturing screenshot: {e}")
        
        # 6. If still no screenshots, return original result
        if not has_screenshots:
            logger.debug(f"No visual data available for {tool_name}")
            return result
        
        # 7. Perform visual analysis
        return await self._enhance_result_with_visual_context(result, tool_name)
    
    async def _enhance_result_with_visual_context(
        self,
        result: ToolResult,
        tool_name: str
    ) -> ToolResult:
        """
        Enhance tool result with visual analysis for grounding agent workflows.
        """
        import asyncio
        import base64
        import litellm
        
        try:
            metadata = getattr(result, 'metadata', None)
            if not metadata:
                return result
            
            # Collect all screenshots
            screenshots_bytes = []
            
            # Check for multiple screenshots first
            if metadata.get("screenshots"):
                screenshots_list = metadata["screenshots"]
                if isinstance(screenshots_list, list):
                    screenshots_bytes = [s for s in screenshots_list if s]
            # Fall back to single screenshot
            elif metadata.get("screenshot"):
                screenshots_bytes = [metadata["screenshot"]]
            
            if not screenshots_bytes:
                return result
            
            # Select key screenshots if there are too many
            selected_screenshots = self._select_key_screenshots(screenshots_bytes, max_count=3)
            
            # Convert to base64
            visual_b64_list = []
            for visual_data in selected_screenshots:
                if isinstance(visual_data, bytes):
                    visual_b64_list.append(base64.b64encode(visual_data).decode('utf-8'))
                else:
                    visual_b64_list.append(visual_data)  # Already base64
            
            # Build prompt based on number of screenshots
            num_screenshots = len(visual_b64_list)
            
            prompt = GroundingAgentPrompts.visual_analysis(
                tool_name=tool_name,
                num_screenshots=num_screenshots,
                task_description=getattr(self, '_current_instruction', '')
            )

            # Build content with text prompt + all images
            content = [{"type": "text", "text": prompt}]
            for visual_b64 in visual_b64_list:
                content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{visual_b64}"
                    }
                })

            # Use dedicated visual analysis model if configured, otherwise use main LLM model
            visual_model = self._visual_analysis_model or (self._llm_client.model if self._llm_client else "openrouter/anthropic/claude-sonnet-4.5")
            response = await asyncio.wait_for(
                litellm.acompletion(
                    model=visual_model,
                    messages=[{
                        "role": "user",
                        "content": content
                    }],
                    timeout=self._visual_analysis_timeout
                ),
                timeout=self._visual_analysis_timeout + 5
            )
            
            analysis = response.choices[0].message.content.strip()
            
            # Inject visual analysis into content
            original_content = result.content or "(no text output)"
            enhanced_content = f"{original_content}\n\n**Visual content**: {analysis}"
            
            # Create enhanced result
            enhanced_result = ToolResult(
                status=result.status,
                content=enhanced_content,
                error=result.error,
                metadata={**metadata, "visual_analyzed": True, "visual_analysis": analysis},
                execution_time=result.execution_time
            )
            
            logger.info(f"Enhanced {tool_name} result with visual analysis ({num_screenshots} screenshot(s))")
            return enhanced_result
            
        except asyncio.TimeoutError:
            logger.warning(f"Visual analysis timed out for {tool_name}, returning original result")
            return result
        except Exception as e:
            logger.warning(f"Failed to analyze visual content for {tool_name}: {e}")
            return result
    
    def _select_key_screenshots(
        self, 
        screenshots: List[bytes], 
        max_count: int = 3
    ) -> List[bytes]:
        """
        Select key screenshots if there are too many.
        """
        if len(screenshots) <= max_count:
            return screenshots
        
        selected_indices = set()
        
        # Always include last (final state)
        selected_indices.add(len(screenshots) - 1)
        
        # If room, include first (initial state)
        if max_count >= 2:
            selected_indices.add(0)
        
        # Fill remaining slots with evenly spaced middle screenshots
        remaining_slots = max_count - len(selected_indices)
        if remaining_slots > 0:
            # Calculate spacing
            available_indices = [
                i for i in range(1, len(screenshots) - 1)
                if i not in selected_indices
            ]
            
            if available_indices:
                step = max(1, len(available_indices) // (remaining_slots + 1))
                for i in range(remaining_slots):
                    idx = min((i + 1) * step, len(available_indices) - 1)
                    if idx < len(available_indices):
                        selected_indices.add(available_indices[idx])
        
        # Return screenshots in original order
        selected = [screenshots[i] for i in sorted(selected_indices)]
        
        logger.debug(
            f"Selected {len(selected)} screenshots at indices {sorted(selected_indices)} "
            f"from total of {len(screenshots)}"
        )
        
        return selected

    def _get_workspace_path(self, context: Dict[str, Any]) -> Optional[str]:
        """
        Get workspace directory path from context.
        """
        return context.get("workspace_dir")
    
    def _scan_workspace_files(
        self,
        workspace_path: str,
        recent_threshold: int = 600 # seconds
    ) -> Dict[str, Any]:
        """
        Scan workspace directory and collect file information.
        
        Args:
            workspace_path: Path to workspace directory
            recent_threshold: Threshold in seconds for recent files
            
        Returns:
            Dictionary with file information:
                - files: List of all filenames
                - file_details: Dict mapping filename to file info (size, modified, age_seconds)
                - recent_files: List of recently modified filenames
        """
        import os
        import time
        
        result = {
            "files": [],
            "file_details": {},
            "recent_files": []
        }
        
        if not workspace_path or not os.path.exists(workspace_path):
            return result
        
        # Recording system files to exclude from workspace scanning
        excluded_files = {"metadata.json", "traj.jsonl"}
        
        try:
            current_time = time.time()
            
            for filename in os.listdir(workspace_path):
                filepath = os.path.join(workspace_path, filename)
                if os.path.isfile(filepath) and filename not in excluded_files:
                    result["files"].append(filename)
                    
                    # Get file stats
                    stat = os.stat(filepath)
                    file_info = {
                        "size": stat.st_size,
                        "modified": stat.st_mtime,
                        "age_seconds": current_time - stat.st_mtime
                    }
                    result["file_details"][filename] = file_info
                    
                    # Track recently created/modified files
                    if file_info["age_seconds"] < recent_threshold:
                        result["recent_files"].append(filename)
            
            result["files"] = sorted(result["files"])
        
        except Exception as e:
            logger.debug(f"Error scanning workspace files: {e}")
        
        return result
    
    async def _check_workspace_artifacts(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Check workspace directory for existing artifacts that might be relevant to the task.
        Enhanced to detect if task might already be completed.
        """
        import re
        
        workspace_info = {"has_files": False, "files": [], "file_details": {}, "recent_files": []}
        
        try:
            # Get workspace path
            workspace_path = self._get_workspace_path(context)
            
            # Scan workspace files
            scan_result = self._scan_workspace_files(workspace_path, recent_threshold=600)
            
            if scan_result["files"]:
                workspace_info["has_files"] = True
                workspace_info["files"] = scan_result["files"]
                workspace_info["file_details"] = scan_result["file_details"]
                workspace_info["recent_files"] = scan_result["recent_files"]
                
                logger.info(f"Grounding Agent: Found {len(scan_result['files'])} existing files in workspace "
                           f"({len(scan_result['recent_files'])} recent)")
                
                # Check if instruction mentions specific filenames
                instruction = context.get("instruction", "")
                if instruction:
                    # Look for potential file references in instruction
                    potential_outputs = []
                    # Match common file patterns: filename.ext, "filename", 'filename'
                    file_patterns = re.findall(r'["\']?([a-zA-Z0-9_\-]+\.[a-zA-Z0-9]+)["\']?', instruction)
                    for pattern in file_patterns:
                        if pattern in scan_result["files"]:
                            potential_outputs.append(pattern)
                    
                    if potential_outputs:
                        workspace_info["matching_files"] = potential_outputs
                        logger.info(f"Grounding Agent: Found {len(potential_outputs)} files matching task: {potential_outputs}")
        
        except Exception as e:
            logger.debug(f"Could not check workspace artifacts: {e}")
        
        return workspace_info
    
    def _build_iteration_feedback(
        self,
        iteration: int,
        llm_summary: Optional[str] = None,
        add_guidance: bool = True
    ) -> Optional[Dict[str, str]]:
        """
        Build feedback message to add to next iteration.
        """
        if not llm_summary:
            return None
        
        feedback_content = GroundingAgentPrompts.iteration_feedback(
            iteration=iteration,
            llm_summary=llm_summary,
            add_guidance=add_guidance
        )
        
        return {
            "role": "system",
            "content": feedback_content
        }
    
    def _remove_previous_guidance(self, messages: List[Dict[str, Any]]) -> None:
        """
        Remove guidance section from previous iteration feedback messages.
        """
        for msg in messages:
            if msg.get("role") == "system":
                content = msg.get("content", "")
                # Check if this is an iteration feedback message with guidance
                if "## Iteration" in content and "Summary" in content and "---" in content:
                    # Remove everything from "---" onwards (the guidance part)
                    summary_only = content.split("---")[0].strip()
                    msg["content"] = summary_only

    async def _generate_final_summary(
        self,
        instruction: str,
        messages: List[Dict],
        iterations: int
    ) -> tuple[str, bool, List[Dict]]:
        """
        Generate final summary across all iterations for reporting to upper layer.
        
        Returns:
            tuple[str, bool, List[Dict]]: (summary_text, success_flag, context_used)
                - summary_text: The generated summary or error message
                - success_flag: True if summary was generated successfully, False otherwise
                - context_used: The cleaned messages used for generating summary
        """
        final_summary_prompt = {
            "role": "user",
            "content": GroundingAgentPrompts.final_summary(
                instruction=instruction,
                iterations=iterations
            )
        }
        
        clean_messages = []
        for msg in messages:
            # Skip tool result messages
            if msg.get("role") == "tool":
                continue
            # Copy message and remove tool_calls if present
            clean_msg = msg.copy()
            if "tool_calls" in clean_msg:
                del clean_msg["tool_calls"]
            clean_messages.append(clean_msg)
        
        clean_messages.append(final_summary_prompt)
        
        # Save context for return
        context_for_return = copy.deepcopy(clean_messages)
        
        try:
            # Call LLMClient to generate final summary (without tools)
            summary_response = await self._llm_client.complete(
                messages=clean_messages,
                tools=None,
                execute_tools=False
            )
            
            final_summary = summary_response.get("message", {}).get("content", "")
            
            if final_summary:
                logger.info(f"Generated final summary: {final_summary[:200]}...")
                return final_summary, True, context_for_return
            else:
                logger.warning("LLM returned empty final summary")
                return f"Task completed after {iterations} iteration(s). Check execution history for details.", True, context_for_return
        
        except Exception as e:
            logger.error(f"Error generating final summary: {e}")
            return f"Task completed after {iterations} iteration(s), but failed to generate summary: {str(e)}", False, context_for_return
    

    async def _build_final_result(
        self,
        instruction: str,
        messages: List[Dict],
        all_tool_results: List[Dict],
        iterations: int,
        max_iterations: int,
        iteration_contexts: List[Dict] = None,
        retrieved_tools_list: List[Dict] = None,
        search_debug_info: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """
        Build final execution result.
        
        Args:
            instruction: Original instruction
            messages: Complete conversation history (including all iteration summaries)
            all_tool_results: All tool execution results
            iterations: Number of iterations performed
            max_iterations: Maximum allowed iterations
            iteration_contexts: Context snapshots for each iteration
            retrieved_tools_list: List of tools retrieved for this task
            search_debug_info: Debug info from tool search (similarity scores, LLM selections)
        """
        is_complete = self._check_task_completion(messages)
        
        tool_executions = self._format_tool_executions(all_tool_results)
        
        result = {
            "instruction": instruction,
            "step": self.step,
            "iterations": iterations,
            "tool_executions": tool_executions,
            "messages": messages,
            "iteration_contexts": iteration_contexts or [],
            "retrieved_tools_list": retrieved_tools_list or [],
            "search_debug_info": search_debug_info,
            "active_skills": list(self._active_skill_ids),
            "keep_session": True
        }
        
        if is_complete:
            logger.info("Task completed with <COMPLETE> marker")
            # Use LLM's own completion response directly (no extra LLM call needed)
            # LLM already generates a summary before outputting <COMPLETE>
            last_response = self._extract_last_assistant_message(messages)
            # Remove the <COMPLETE> token from response for cleaner output
            result["response"] = last_response.replace(GroundingAgentPrompts.TASK_COMPLETE, "").strip()
            result["status"] = "success"
            
            # [DISABLED] Extra LLM call to generate final summary
            # final_summary, summary_success, final_summary_context = await self._generate_final_summary(
            #     instruction=instruction,
            #     messages=messages,
            #     iterations=iterations
            # )
            # result["response"] = final_summary
            # result["final_summary_context"] = final_summary_context
        else:
            result["response"] = self._extract_last_assistant_message(messages)
            result["status"] = "incomplete"
            result["warning"] = (
                f"Task reached max iterations ({max_iterations}) without completion. "
                f"This may indicate the task needs more steps or clarification."
            )
        
        return result
    
    def _format_tool_executions(self, all_tool_results: List[Dict]) -> List[Dict]:
        executions = []
        for tr in all_tool_results:
            tool_result_obj = tr.get("result")
            tool_call = tr.get("tool_call")
            
            status = "unknown"
            if hasattr(tool_result_obj, 'status'):
                status_obj = tool_result_obj.status
                status = getattr(status_obj, 'value', status_obj)
            
            # Extract tool_name and arguments from tool_call object (litellm format)
            tool_name = "unknown"
            arguments = {}
            if tool_call is not None:
                if hasattr(tool_call, 'function'):
                    # tool_call is an object with .function attribute
                    tool_name = getattr(tool_call.function, 'name', 'unknown')
                    args_raw = getattr(tool_call.function, 'arguments', '{}')
                    if isinstance(args_raw, str):
                        try:
                            arguments = json.loads(args_raw) if args_raw.strip() else {}
                        except json.JSONDecodeError:
                            arguments = {}
                    else:
                        arguments = args_raw if isinstance(args_raw, dict) else {}
                elif isinstance(tool_call, dict):
                    # Fallback: tool_call is a dict
                    func = tool_call.get("function", {})
                    tool_name = func.get("name", "unknown")
                    args_raw = func.get("arguments", "{}")
                    if isinstance(args_raw, str):
                        try:
                            arguments = json.loads(args_raw) if args_raw.strip() else {}
                        except json.JSONDecodeError:
                            arguments = {}
                    else:
                        arguments = args_raw if isinstance(args_raw, dict) else {}
            
            executions.append({
                "tool_name": tool_name,
                "arguments": arguments,
                "backend": tr.get("backend"),
                "server_name": tr.get("server_name"),
                "status": status,
                "content": tool_result_obj.content if hasattr(tool_result_obj, 'content') else None,
                "error": tool_result_obj.error if hasattr(tool_result_obj, 'error') else None,
                "execution_time": tool_result_obj.execution_time if hasattr(tool_result_obj, 'execution_time') else None,
                "metadata": tool_result_obj.metadata if hasattr(tool_result_obj, 'metadata') else {},
            })
        return executions
    
    def _check_task_completion(self, messages: List[Dict]) -> bool:
        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                content = msg.get("content", "")
                return GroundingAgentPrompts.TASK_COMPLETE in content
        return False
    
    def _extract_last_assistant_message(self, messages: List[Dict]) -> str:
        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                return msg.get("content", "")
        return ""
    
    async def _record_agent_execution(
        self,
        result: Dict[str, Any],
        instruction: str
    ) -> None:
        """
        Record agent execution to recording manager.
        
        Args:
            result: Execution result
            instruction: Original instruction
        """
        if not self._recording_manager:
            return
        
        # Extract tool execution summary
        tool_summary = []
        if result.get("tool_executions"):
            for exec_info in result["tool_executions"]:
                tool_summary.append({
                    "tool": exec_info.get("tool_name", "unknown"),
                    "backend": exec_info.get("backend", "unknown"),
                    "status": exec_info.get("status", "unknown"),
                })
        
        await self._recording_manager.record_agent_action(
            agent_name=self.name,
            action_type="execute",
            input_data={"instruction": instruction},
            reasoning={
                "response": result.get("response", ""),
                "tools_selected": tool_summary,
            },
            output_data={
                "status": result.get("status", "unknown"),
                "iterations": result.get("iterations", 0),
                "num_tool_executions": len(result.get("tool_executions", [])),
            },
            metadata={
                "step": self.step,
                "instruction": instruction,
            }
        )