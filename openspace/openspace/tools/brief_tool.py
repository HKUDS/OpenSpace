"""BriefTool - turn/session response style control.

OpenSpace keeps normal assistant text as the primary user-visible channel, so
this tool toggles concise response style in the current runtime context without
changing permissions or user settings.
"""

from __future__ import annotations

from typing import Any, Literal, Mapping

from openspace.grounding.core.permissions.types import PermissionAllow
from openspace.grounding.core.tool.base import BaseTool
from openspace.grounding.core.types import BackendType, ToolResult, ToolSchema, ToolStatus


BRIEF_TOOL_NAME = "brief"
BRIEF_TOOL_ALIAS = "Brief"
BRIEF_MAX_RESULT_SIZE_CHARS = 100_000
ResponseStyleMode = Literal["brief", "normal"]

DESCRIPTION = "Toggle concise assistant responses for this session"
PROMPT = """Use this tool to switch the assistant's response style for future text.

Set enabled=true or mode='brief' when the user asks for shorter answers or when low-value explanation should be minimized. Set enabled=false or mode='normal' to restore the default response style.

This only changes assistant text style. It does not change permissions, tools, or user configuration files."""


def make_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "enabled": {
                "type": "boolean",
                "description": "True for brief mode, false to restore normal mode.",
            },
            "mode": {
                "type": "string",
                "enum": ["brief", "normal"],
                "description": "Explicit response style mode.",
            },
        },
        "additionalProperties": False,
    }


def resolve_response_style(input_data: Mapping[str, Any]) -> tuple[ResponseStyleMode | None, str | None]:
    has_enabled = "enabled" in input_data and input_data.get("enabled") is not None
    has_mode = "mode" in input_data and input_data.get("mode") is not None
    if has_enabled and has_mode:
        enabled_style = "brief" if bool(input_data.get("enabled")) else "normal"
        mode_style = str(input_data.get("mode")).strip().lower()
        if mode_style != enabled_style:
            return None, "enabled and mode disagree."
    if has_mode:
        mode = str(input_data.get("mode")).strip().lower()
        if mode not in {"brief", "normal"}:
            return None, "mode must be brief or normal."
        return mode, None  # type: ignore[return-value]
    if has_enabled:
        return ("brief" if bool(input_data.get("enabled")) else "normal"), None
    return "brief", None


class BriefTool(BaseTool):
    _name = BRIEF_TOOL_NAME
    _description = DESCRIPTION
    backend_type = BackendType.META
    aliases = [BRIEF_TOOL_ALIAS, "BriefTool"]
    _is_read_only = True
    _is_concurrency_safe = True
    should_defer = True
    search_hint = "concise response style"
    max_result_size_chars = BRIEF_MAX_RESULT_SIZE_CHARS

    def __init__(self) -> None:
        self._current_context: Any | None = None
        super().__init__(
            schema=ToolSchema(
                name=self._name,
                description=DESCRIPTION,
                parameters=make_input_schema(),
                backend_type=self.backend_type,
            )
        )

    def set_context(self, context: Any) -> None:
        self._current_context = context

    def get_prompt(self, context: Any = None) -> str:
        return PROMPT

    async def validate_input(self, input: dict[str, Any], context: Any = None) -> str | None:
        _, error = resolve_response_style(input)
        return error

    async def check_permissions(self, input: dict[str, Any], context: Any = None) -> PermissionAllow:
        return PermissionAllow(updated_input=dict(input))

    async def _arun(
        self,
        enabled: bool | None = None,
        mode: str | None = None,
    ) -> ToolResult:
        input_data: dict[str, Any] = {}
        if enabled is not None:
            input_data["enabled"] = enabled
        if mode is not None:
            input_data["mode"] = mode
        style, error = resolve_response_style(input_data)
        if error or style is None:
            return ToolResult(status=ToolStatus.ERROR, content=error or "Invalid response style.", error=error)

        context = self._current_context
        if context is not None:
            try:
                context.response_style = style
            except Exception:
                pass
        session_metadata = getattr(context, "session_metadata", None) if context is not None else None
        if isinstance(session_metadata, dict):
            session_metadata["response_style"] = style

        data = {
            "response_style": style,
            "enabled": style == "brief",
        }
        content = (
            "Brief response mode enabled."
            if style == "brief"
            else "Normal response mode restored."
        )
        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=content,
            metadata={"tool": self.name, "data": data},
        )


__all__ = [
    "BRIEF_TOOL_NAME",
    "BRIEF_MAX_RESULT_SIZE_CHARS",
    "BriefTool",
    "BRIEF_TOOL_ALIAS",
    "resolve_response_style",
]
