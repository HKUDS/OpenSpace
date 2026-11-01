from __future__ import annotations

from typing import Any, Mapping

from openspace.grounding.core.permissions.types import (
    DecisionReasonOther,
    PermissionAllow,
    PermissionAsk,
    PermissionDeny,
)
from openspace.grounding.core.tool.base import BaseTool
from openspace.grounding.core.types import BackendType, ToolResult, ToolSchema, ToolStatus
from openspace.services.runtime_support.plan_mode import (
    ENTER_PLAN_MODE_TOOL_NAME,
    EXIT_PLAN_MODE_TOOL_NAME,
    enter_plan_mode,
    exit_plan_mode,
    get_plan,
    get_plan_file_path,
    write_plan,
)


ENTER_PLAN_MODE_PROMPT = """Use this tool proactively when you're about to start a non-trivial implementation task. Getting user sign-off on your approach before writing code prevents wasted effort and ensures alignment. This tool transitions you into plan mode where you can explore the codebase and design an implementation approach for user approval.

In plan mode, you should:
1. Thoroughly explore the codebase to understand existing patterns
2. Identify similar features and architectural approaches
3. Consider multiple approaches and their trade-offs
4. Use AskUserQuestion if you need to clarify the approach
5. Design a concrete implementation strategy
6. When ready, use ExitPlanMode to present your plan for approval

Remember: DO NOT write or edit any files except the plan file."""


EXIT_PLAN_MODE_PROMPT = """Use this tool when you are in plan mode and have finished writing your plan to the plan file and are ready for user approval.

## How This Tool Works
- You should have already written your plan to the plan file specified in the plan mode system message
- This tool does NOT require the plan content as a parameter - it will read the plan from the file you wrote
- This tool simply signals that you're done planning and ready for the user to review and approve
- The user will see the contents of your plan file when they review it

IMPORTANT: Only use this tool when the task requires planning the implementation steps of a task that requires writing code. For research tasks where you're gathering information, searching files, reading files or trying to understand the codebase, do NOT use this tool."""


class EnterPlanModeTool(BaseTool):
    _name = ENTER_PLAN_MODE_TOOL_NAME
    _description = "Requests permission to enter plan mode for complex tasks requiring exploration and design"
    backend_type = BackendType.META
    _is_read_only = True
    _is_concurrency_safe = True
    should_defer = True
    search_hint = "switch to plan mode to design approach"
    max_result_size_chars = 100_000

    def __init__(self) -> None:
        super().__init__(
            schema=ToolSchema(
                name=self._name,
                description=self._description,
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                return_schema={
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                },
                backend_type=self.backend_type,
            )
        )
        self._current_context: Any | None = None

    def get_prompt(self, context: Any = None) -> str:
        return ENTER_PLAN_MODE_PROMPT

    def set_context(self, context: Any) -> None:
        self._current_context = context

    async def check_permissions(self, input: dict[str, Any], context: Any = None) -> PermissionAllow:
        return PermissionAllow(updated_input=dict(input))

    async def _arun(self, **kwargs: Any) -> ToolResult:
        context = self._current_context
        if context is None:
            return ToolResult(
                status=ToolStatus.ERROR,
                content="EnterPlanMode requires a tool execution context.",
                error="Missing ToolUseContext",
                metadata={"tool": self.name},
            )
        if getattr(context, "agent_id", None) not in (None, "", "primary"):
            return ToolResult(
                status=ToolStatus.ERROR,
                content="EnterPlanMode tool cannot be used in agent contexts",
                error="EnterPlanMode tool cannot be used in agent contexts",
                metadata={"tool": self.name},
            )
        state = enter_plan_mode(context)
        message = (
            "Entered plan mode. You should now focus on exploring the codebase "
            "and designing an implementation approach."
        )
        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=(
                f"{message}\n\n"
                "DO NOT write or edit any files except the plan file. "
                f"Write the plan to: {state['plan_file_path']}"
            ),
            metadata={
                "tool": self.name,
                "data": {
                    "message": message,
                    "planFilePath": state["plan_file_path"],
                    "previousMode": state["previous_mode"],
                },
            },
        )


class ExitPlanModeTool(BaseTool):
    _name = EXIT_PLAN_MODE_TOOL_NAME
    _description = "Prompts the user to exit plan mode and start coding"
    backend_type = BackendType.META
    _is_read_only = False
    _is_concurrency_safe = True
    requires_user_interaction = True
    should_defer = True
    search_hint = "present plan for approval and start coding"
    max_result_size_chars = 100_000
    parameter_descriptions = {
        "allowedPrompts": (
            "Prompt-based permissions needed to implement the plan. These "
            "describe categories of actions rather than specific commands."
        ),
        "plan": (
            "Optional edited plan content provided by the user approval flow. "
            "Normally injected locally from the plan file."
        ),
        "planFilePath": "The plan file path injected locally before execution.",
    }

    def __init__(self) -> None:
        super().__init__(
            schema=ToolSchema(
                name=self._name,
                description=self._description,
                parameters={
                    "type": "object",
                    "properties": {
                        "allowedPrompts": {"type": "array", "items": {"type": "object"}},
                        "plan": {"type": "string"},
                        "planFilePath": {"type": "string"},
                    },
                    "additionalProperties": True,
                },
                return_schema={
                    "type": "object",
                    "properties": {
                        "plan": {"type": ["string", "null"]},
                        "isAgent": {"type": "boolean"},
                        "filePath": {"type": "string"},
                        "planWasEdited": {"type": "boolean"},
                    },
                    "required": ["plan", "isAgent"],
                },
                backend_type=self.backend_type,
            )
        )
        self._current_context: Any | None = None

    def get_prompt(self, context: Any = None) -> str:
        return EXIT_PLAN_MODE_PROMPT

    def set_context(self, context: Any) -> None:
        self._current_context = context

    async def validate_input(self, input_data: dict[str, Any], context: Any = None) -> str | None:
        context = context or self._current_context
        if str(getattr(context, "permission_mode", "") or "") != "plan":
            return (
                "You are not in plan mode. This tool is only for exiting plan "
                "mode after writing a plan. If your plan was already approved, "
                "continue with implementation."
            )
        return None

    async def check_permissions(self, input: dict[str, Any], context: Any = None):
        context = context or self._current_context
        if str(getattr(context, "permission_mode", "") or "") != "plan":
            return PermissionDeny(
                message="ExitPlanMode can only be used while in plan mode.",
                decision_reason=DecisionReasonOther(reason="not in plan mode"),
            )
        return PermissionAsk(
            message="Exit plan mode?",
            updated_input=dict(input),
            decision_reason=DecisionReasonOther(reason="Exit plan mode requires user approval"),
        )

    def is_user_interaction_complete(self, input_data: Mapping[str, Any]) -> bool:
        return True

    async def _arun(self, **kwargs: Any) -> ToolResult:
        context = self._current_context
        if context is None:
            return ToolResult(
                status=ToolStatus.ERROR,
                content="ExitPlanMode requires a tool execution context.",
                error="Missing ToolUseContext",
                metadata={"tool": self.name},
            )
        session_id = getattr(context, "session_id", None)
        agent_id = getattr(context, "agent_id", None)
        file_path = get_plan_file_path(session_id, agent_id)
        input_plan = kwargs.get("plan")
        plan_was_edited = isinstance(input_plan, str)
        if plan_was_edited:
            write_plan(input_plan, session_id, agent_id)
        plan = str(input_plan) if plan_was_edited else get_plan(session_id, agent_id)
        exit_plan_mode(context)
        is_agent = bool(agent_id and agent_id != "primary")
        if is_agent:
            content = (
                'User has approved the plan. There is nothing else needed from '
                'you now. Please respond with "ok"'
            )
        elif not plan or not plan.strip():
            content = "User has approved exiting plan mode. You can now proceed."
        else:
            label = "Approved Plan (edited by user)" if plan_was_edited else "Approved Plan"
            content = (
                "User has approved your plan. You can now start coding. Start "
                "with updating your todo list if applicable\n\n"
                f"Your plan has been saved to: {file_path}\n"
                "You can refer back to it if needed during implementation.\n\n"
                f"## {label}:\n{plan}"
            )
        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=content,
            metadata={
                "tool": self.name,
                "data": {
                    "plan": plan,
                    "isAgent": is_agent,
                    "filePath": str(file_path),
                    "planWasEdited": plan_was_edited or None,
                },
            },
        )


__all__ = [
    "EnterPlanModeTool",
    "ExitPlanModeTool",
    "ENTER_PLAN_MODE_TOOL_NAME",
    "EXIT_PLAN_MODE_TOOL_NAME",
]

