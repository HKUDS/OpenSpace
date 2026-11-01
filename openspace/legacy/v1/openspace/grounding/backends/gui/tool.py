import base64
from typing import Any, Dict
from openspace.grounding.core.tool.base import BaseTool
from openspace.grounding.core.types import BackendType, ToolResult, ToolStatus
from .transport.connector import GUIConnector
from .transport.actions import ACTION_SPACE, KEYBOARD_KEYS
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


class GUIAgentTool(BaseTool):
    """
    LLM-powered GUI Agent Tool.
    
    This tool acts as an intelligent agent that:
    - Takes a task description as input
    - Observes the desktop via screenshot
    - Uses LLM/VLM to understand and plan actions
    - Outputs action space commands
    - Executes actions through the connector
    """
    
    _name = "gui_agent"
    _description = """Vision-based GUI automation agent for tasks requiring graphical interface interaction.
    
    Use this tool when the task involves:
    - Operating desktop applications with graphical interfaces (browsers, editors, design tools, etc.)
    - Tasks that require visual understanding of UI elements, layouts, or content
    - Multi-step workflows that need click, drag, type, or other GUI interactions
    - Scenarios where programmatic APIs or command-line tools are unavailable or insufficient
    
    The agent observes screen state through screenshots, uses vision-language models to understand
    the interface, plans appropriate actions, and executes GUI operations autonomously.
    
    IMPORTANT - max_steps Parameter Guidelines:
    - Simple tasks (1-2 actions): 15-20 steps
    - Medium tasks (3-5 actions): 25-35 steps  
    - Complex tasks (6+ actions, like web navigation): 35-50 steps
    - When uncertain, prefer larger values (35+) to avoid premature termination
    - Default is 25, but increase for multi-step workflows
    
    Input: 
    - task_description: Natural language task description
    - max_steps: Maximum actions (default 25, increase for complex tasks)
    
    Output: Task execution results with action history and completion status
    """
    
    backend_type = BackendType.GUI
    
    def __init__(self, connector: GUIConnector, llm_client=None, recording_manager=None, **kwargs):
        """
        Initialize GUI Agent Tool.
        
        Args:
            connector: GUI connector for communication with desktop_env
            llm_client: LLM/VLM client for vision-based planning (optional)
            recording_manager: RecordingManager for recording intermediate steps (optional)
            **kwargs: Additional arguments for BaseTool
        """
        super().__init__(**kwargs)
        self.connector = connector
        self.llm_client = llm_client  # Will be injected later
        self.recording_manager = recording_manager  # For recording intermediate steps
        self.action_history = []  # Track executed actions
    
    async def _arun(
        self,
        task_description: str,
        max_steps: int = 50,
    ) -> ToolResult:
        """
        Execute a GUI automation task using LLM planning.
        
        This is the main entry point that:
        1. Gets current screenshot
        2. Uses LLM to plan next action based on task and screenshot
        3. Executes the planned action
        4. Repeats until task is complete or max_steps reached
        
        Args:
            task_description: Natural language description of the task
            max_steps: Maximum number of actions to execute (default 25)
                Recommended values based on task complexity:
                - Simple (1-2 actions): 15-20
                - Medium (3-5 actions): 25-35
                - Complex (6+ actions, web navigation, multi-app): 35-50
                When in doubt, use higher values to avoid premature termination
        
        Returns:
            ToolResult with task execution status
        """
        if not task_description:
            return ToolResult(
                status=ToolStatus.ERROR,
                error="task_description is required"
            )
        
        logger.info(f"Starting GUI task: {task_description}")
        self.action_history = []
        
        # Execute task with LLM planning loop
        try:
            result = await self._execute_task_with_planning(
                task_description=task_description,
                max_steps=max_steps,
            )
            return result
        
        except Exception as e:
            logger.error(f"Task execution failed: {e}")
            return ToolResult(
                status=ToolStatus.ERROR,
                error=str(e),
                metadata={
                    "task_description": task_description,
                    "actions_executed": len(self.action_history),
                    "action_history": self.action_history,
                }
            )
    
    async def _execute_task_with_planning(
        self,
        task_description: str,
        max_steps: int,
    ) -> ToolResult:
        """
        Execute task with LLM-based planning loop.
        
        Planning loop:
        1. Observe: Get screenshot
        2. Plan: LLM decides next action
        3. Execute: Perform the action
        4. Verify: Check if task is complete
        5. Repeat until done or max_steps
        
        Args:
            task_description: Task to complete
            max_steps: Maximum planning iterations
        
        Returns:
            ToolResult with execution details
        """
        # Collect all screenshots for visual analysis
        all_screenshots = []
        # Collect intermediate steps
        intermediate_steps = []
        
        for step in range(max_steps):
            logger.info(f"Planning step {step + 1}/{max_steps}")
            
            # Step 1: Observe current state
            screenshot = await self.connector.get_screenshot()
            if not screenshot:
                return ToolResult(
                    status=ToolStatus.ERROR,
                    error="Failed to get screenshot for planning",
                    metadata={"step": step, "action_history": self.action_history}
                )
            
            # Collect screenshot for visual analysis
            all_screenshots.append(screenshot)
            
            # Step 2: Plan next action using LLM
            planned_action = await self._plan_next_action(
                task_description=task_description,
                screenshot=screenshot,
                action_history=self.action_history,
            )
            
            # Check if task is complete
            if planned_action["action_type"] == "DONE":
                logger.info("Task marked as complete by LLM")
                reasoning = planned_action.get("reasoning", "Task completed successfully")
                
                intermediate_steps.append({
                    "step_number": step + 1,
                    "action": "DONE",
                    "reasoning": reasoning,
                    "status": "done",
                })
                
                return ToolResult(
                    status=ToolStatus.SUCCESS,
                    content=f"Task completed: {task_description}\n\nFinal state: {reasoning}",
                    metadata={
                        "steps_taken": step + 1,
                        "action_history": self.action_history,
                        "screenshots": all_screenshots,
                        "intermediate_steps": intermediate_steps,
                        "final_reasoning": reasoning,
                    }
                )
            
            # Check if task failed
            if planned_action["action_type"] == "FAIL":
                logger.warning("Task marked as failed by LLM")
                reason = planned_action.get("reason", "Task cannot be completed")
                
                intermediate_steps.append({
                    "step_number": step + 1,
                    "action": "FAIL",
                    "reasoning": planned_action.get("reasoning", ""),
                    "status": "failed",
                })
                
                return ToolResult(
                    status=ToolStatus.ERROR,
                    error=reason,
                    metadata={
                        "steps_taken": step + 1,
                        "action_history": self.action_history,
                        "screenshots": all_screenshots,
                        "intermediate_steps": intermediate_steps,
                    }
                )
            
            # Check if action is WAIT (screenshot observation, continue to next step)
            if planned_action["action_type"] == "WAIT":
                logger.info("Screenshot observation step, continuing planning loop")
                intermediate_steps.append({
                    "step_number": step + 1,
                    "action": "WAIT",
                    "reasoning": planned_action.get("reasoning", ""),
                    "status": "observation",
                })
                continue
            
            # Step 3: Execute the planned action
            execution_result = await self._execute_planned_action(planned_action)
            
            # Record action in history
            self.action_history.append({
                "step": step + 1,
                "planned_action": planned_action,
                "execution_result": execution_result,
            })
            
            intermediate_steps.append({
                "step_number": step + 1,
                "action": planned_action.get("action_type", "unknown"),
                "reasoning": planned_action.get("reasoning", ""),
                "status": execution_result.get("status", "unknown"),
            })
            
            # Check execution result
            if execution_result.get("status") != "success":
                logger.warning(f"Action execution failed: {execution_result.get('error')}")
                # Continue to next iteration for retry planning
        
        # Max steps reached
        return ToolResult(
            status=ToolStatus.ERROR,
            error=f"Task incomplete after {max_steps} steps",
            metadata={
                "task_description": task_description,
                "steps_taken": max_steps,
                "action_history": self.action_history,
                "screenshots": all_screenshots,
                "intermediate_steps": intermediate_steps,
            }
        )
    
    async def _plan_next_action(
        self,
        task_description: str,
        screenshot: bytes,
        action_history: list,
    ) -> Dict[str, Any]:
        """
        Use LLM/VLM to plan the next action.
        
        This method sends:
        - Task description
        - Current screenshot (vision input)
        - Action history (context)
        - Available ACTION_SPACE
        
        And gets back a structured action plan.
        
        Args:
            task_description: The task to accomplish
            screenshot: Current desktop screenshot (PNG/JPEG bytes)
            action_history: Previously executed actions
        
        Returns:
            Dict with action_type and parameters
        """
        if self.llm_client is None:
            # Fallback: Simple heuristic or manual mode
            logger.warning("No LLM client configured, using fallback mode")
            return {
                "action_type": "FAIL",
                "reason": "LLM client not configured"
            }
        
        # Check if using Anthropic client
        try:
            from .anthropic_client import AnthropicGUIClient
            is_anthropic = isinstance(self.llm_client, AnthropicGUIClient)
        except ImportError:
            is_anthropic = False
        
        if is_anthropic:
            # Use Anthropic client
            try:
                reasoning, commands = await self.llm_client.plan_action(
                    task_description=task_description,
                    screenshot=screenshot,
                    action_history=action_history,
                )
                
                if commands == ["FAIL"]:
                    return {
                        "action_type": "FAIL",
                        "reason": "Anthropic planning failed"
                    }
                
                if commands == ["DONE"]:
                    return {
                        "action_type": "DONE",
                        "reasoning": reasoning
                    }
                
                if commands == ["SCREENSHOT"]:
                    # Screenshot is automatically handled by system
                    # Continue to next planning step
                    logger.info("LLM requested screenshot (observation step)")
                    return {
                        "action_type": "WAIT",
                        "reasoning": reasoning or "Observing screen state"
                    }
                
                # If no commands but has reasoning, task is complete
                # (Anthropic returns text-only when task is done)
                if not commands and reasoning:
                    logger.info("LLM returned text-only response, interpreting as task completion")
                    return {
                        "action_type": "DONE",
                        "reasoning": reasoning
                    }
                
                # No commands and no reasoning = error
                if not commands:
                    return {
                        "action_type": "FAIL",
                        "reason": "No commands generated and no completion message"
                    }
                
                # Return first command (Anthropic returns pyautogui commands directly)
                return {
                    "action_type": "PYAUTOGUI_COMMAND",
                    "command": commands[0],
                    "reasoning": reasoning
                }
                
            except Exception as e:
                logger.error(f"Anthropic planning failed: {e}")
                return {
                    "action_type": "FAIL",
                    "reason": f"Planning error: {str(e)}"
                }
        
        # Generic LLM client (for future integration with other LLMs)
        # Encode screenshot to base64 for LLM
        screenshot_b64 = base64.b64encode(screenshot).decode('utf-8')
        
        # Prepare prompt for LLM
        prompt = self._build_planning_prompt(
            task_description=task_description,
            action_history=action_history,
        )
        
        # Call LLM with vision input
        try:
            response = await self.llm_client.plan_action(
                prompt=prompt,
                image_base64=screenshot_b64,
                action_space=ACTION_SPACE,
                keyboard_keys=KEYBOARD_KEYS,
            )
            
            # Parse LLM response to action dict
            action = self._parse_llm_response(response)
            
            logger.info(f"LLM planned action: {action['action_type']}")
            return action
        
        except Exception as e:
            logger.error(f"LLM planning failed: {e}")
            return {
                "action_type": "FAIL",
                "reason": f"Planning error: {str(e)}"
            }
    
    def _build_planning_prompt(
        self,
        task_description: str,
        action_history: list,
    ) -> str:
        """
        Build prompt for LLM planning.
        
        Args:
            task_description: The task to accomplish
            action_history: Previously executed actions
        
        Returns:
            Formatted prompt string
        """
        prompt = f"""You are a GUI automation agent. Your task is to complete the following:

Task: {task_description}

You can observe the current desktop state through the provided screenshot.
You must plan the next action to take from the available ACTION_SPACE.

Available actions:
- Mouse: MOVE_TO, CLICK, RIGHT_CLICK, DOUBLE_CLICK, DRAG_TO, SCROLL
- Keyboard: TYPING, PRESS, KEY_DOWN, KEY_UP, HOTKEY
- Control: WAIT, DONE, FAIL

"""
        
        if action_history:
            prompt += f"\nPrevious actions taken ({len(action_history)}):\n"
            for i, action in enumerate(action_history[-5:], 1):  # Last 5 actions
                prompt += f"{i}. {action['planned_action']['action_type']}"
                if 'parameters' in action['planned_action']:
                    prompt += f" - {action['planned_action']['parameters']}"
                prompt += "\n"
        
        prompt += """
Based on the screenshot and task, output the next action in JSON format:
{
    "action_type": "ACTION_TYPE",
    "parameters": {...},
    "reasoning": "Why this action is needed"
}

If the task is complete, output: {"action_type": "DONE"}
If the task cannot be completed, output: {"action_type": "FAIL", "reason": "explanation"}
"""
        
        return prompt
    
    def _parse_llm_response(self, response: str) -> Dict[str, Any]:
        """
        Parse LLM response to extract action.
        
        Args:
            response: LLM response (should be JSON)
        
        Returns:
            Action dict with action_type and parameters
        """
        import json
        
        try:
            # Try to parse as JSON
            action = json.loads(response)
            
            # Validate action
            if "action_type" not in action:
                raise ValueError("Missing action_type in LLM response")
            
            return action
        
        except json.JSONDecodeError:
            logger.error(f"Failed to parse LLM response as JSON: {response[:200]}")
            return {
                "action_type": "FAIL",
                "reason": "Invalid LLM response format"
            }
    
    async def _execute_planned_action(
        self,
        action: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Execute a planned action through the connector.
        
        Args:
            action: Action dict with action_type and parameters
        
        Returns:
            Execution result dict
        """
        action_type = action["action_type"]
        
        # Handle Anthropic's direct pyautogui commands
        if action_type == "PYAUTOGUI_COMMAND":
            command = action.get("command", "")
            logger.info(f"Executing pyautogui command: {command}")
            
            try:
                result = await self.connector.execute_python_command(command)
                return {
                    "status": "success" if result else "error",
                    "action_type": action_type,
                    "command": command,
                    "result": result
                }
            except Exception as e:
                logger.error(f"Command execution error: {e}")
                return {
                    "status": "error",
                    "action_type": action_type,
                    "error": str(e)
                }
        
        # Handle standard action space commands
        parameters = action.get("parameters", {})
        logger.info(f"Executing action: {action_type}")
        
        try:
            result = await self.connector.execute_action(action_type, parameters)
            return result
        
        except Exception as e:
            logger.error(f"Action execution error: {e}")
            return {
                "status": "error",
                "action_type": action_type,
                "error": str(e)
            }
    
    # Helper methods for direct action execution
    
    async def execute_action(
        self,
        action_type: str,
        parameters: Dict[str, Any]
    ) -> ToolResult:
        """
        Direct action execution (bypass LLM planning).
        
        Args:
            action_type: Action type from ACTION_SPACE
            parameters: Action parameters
        
        Returns:
            ToolResult with execution status
        """
        result = await self.connector.execute_action(action_type, parameters)
        
        if result.get("status") == "success":
            return ToolResult(
                status=ToolStatus.SUCCESS,
                content=f"Executed {action_type}",
                metadata=result
            )
        else:
            return ToolResult(
                status=ToolStatus.ERROR,
                error=result.get("error", "Unknown error"),
                metadata=result
            )
    
    async def get_screenshot(self) -> ToolResult:
        """Get current desktop screenshot."""
        screenshot = await self.connector.get_screenshot()
        if screenshot:
            return ToolResult(
                status=ToolStatus.SUCCESS,
                content=screenshot,
                metadata={"type": "screenshot", "size": len(screenshot)}
            )
        else:
            return ToolResult(
                status=ToolStatus.ERROR,
                error="Failed to capture screenshot"
            )
    
    async def _record_intermediate_step(
        self,
        step_number: int,
        planned_action: Dict[str, Any],
        execution_result: Dict[str, Any],
        screenshot: bytes,
        task_description: str,
    ):
        """
        Record an intermediate step of GUI agent execution.
        
        This method records each planning-action cycle to the recording system,
        providing detailed traces of GUI agent's decision-making process.
        
        Args:
            step_number: Step number in the execution sequence
            planned_action: Action planned by LLM
            execution_result: Result of executing the action
            screenshot: Screenshot before executing the action
            task_description: Overall task description
        """
        # Try to get recording_manager dynamically if not set at initialization
        recording_manager = self.recording_manager
        if not recording_manager and hasattr(self, '_runtime_info') and self._runtime_info:
            # Try to get from grounding_client
            grounding_client = self._runtime_info.grounding_client
            if grounding_client and hasattr(grounding_client, 'recording_manager'):
                recording_manager = grounding_client.recording_manager
                logger.debug(f"Step {step_number}: Dynamically retrieved recording_manager from grounding_client")
        
        if not recording_manager:
            logger.debug(f"Step {step_number}: No recording_manager available, skipping intermediate step recording")
            return
        
        # Check if recording is active
        try:
            from openspace.recording.manager import RecordingManager
            if not RecordingManager.is_recording():
                logger.debug(f"Step {step_number}: RecordingManager not started")
                return
        except Exception as e:
            logger.debug(f"Step {step_number}: Failed to check recording status: {e}")
            return
        
        # Check if recorder is initialized
        if not hasattr(recording_manager, '_recorder') or not recording_manager._recorder:
            logger.warning(f"Step {step_number}: recording_manager._recorder not initialized")
            return
        
        # Build command string for display
        action_type = planned_action.get("action_type", "unknown")
        command = self._format_action_command(planned_action)
        
        # Build result summary
        status = execution_result.get("status", "unknown")
        is_success = status in ("success", "done", "observation")
        
        # Build result content
        if status == "done":
            result_content = f"Task completed at step {step_number}"
        elif status == "failed":
            result_content = execution_result.get("message", "Task failed")
        elif status == "observation":
            result_content = execution_result.get("message", "Screenshot observation")
        else:
            result_content = execution_result.get("result", execution_result.get("message", str(execution_result)))
        
        # Build parameters for recording
        parameters = {
            "task_description": task_description,
            "step_number": step_number,
            "action_type": action_type,
            "planned_action": planned_action,
        }
        
        # Record to trajectory recorder (handles screenshot saving)
        try:
            await recording_manager._recorder.record_step(
                backend="gui",
                tool="gui_agent_step",
                command=command,
                result={
                    "status": "success" if is_success else "error",
                    "output": str(result_content)[:200],
                },
                parameters=parameters,
                screenshot=screenshot,
                extra={
                    "gui_step_number": step_number,
                    "reasoning": planned_action.get("reasoning", ""),
                }
            )
            
            logger.info(f"✓ Recorded GUI intermediate step {step_number}: {command}")
        
        except Exception as e:
            logger.error(f"✗ Failed to record intermediate step {step_number}: {e}", exc_info=True)
    
    def _format_action_command(self, planned_action: Dict[str, Any]) -> str:
        """
        Format planned action into a human-readable command string.
        
        Args:
            planned_action: Action dictionary from LLM planning
            
        Returns:
            Formatted command string
        """
        action_type = planned_action.get("action_type", "unknown")
        
        # Handle special action types
        if action_type == "DONE":
            return "DONE (task completed)"
        elif action_type == "FAIL":
            reason = planned_action.get("reason", "unknown")
            return f"FAIL ({reason})"
        elif action_type == "WAIT":
            return "WAIT (screenshot observation)"
        
        # Handle PyAutoGUI commands
        elif action_type == "PYAUTOGUI_COMMAND":
            command = planned_action.get("command", "")
            # Truncate long commands
            if len(command) > 100:
                return command[:100] + "..."
            return command
        
        # Handle standard action space commands
        else:
            parameters = planned_action.get("parameters", {})
            if parameters:
                # Format first 2 parameters
                param_items = list(parameters.items())[:2]
                param_str = ", ".join([f"{k}={v}" for k, v in param_items])
                return f"{action_type}({param_str})"
            else:
                return action_type