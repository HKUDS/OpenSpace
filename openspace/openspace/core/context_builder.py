"""
Context builder — assembles system prompt, user context, and messages for LLM calls.
"""
from __future__ import annotations

from typing import Any, Optional

from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


class ContextBuilder:
    """Builds the full context dict (system prompt + user context + messages)
    that gets sent to the LLM for each query."""

    def __init__(
        self,
        base_system_prompt: str = "",
        skill_instructions: str = "",
    ) -> None:
        self._base_system_prompt = base_system_prompt
        self._skill_instructions = skill_instructions

    async def build(
        self,
        query: str,
        session_state: dict[str, Any],
    ) -> dict[str, Any]:
        system_prompt = self._build_system_prompt(session_state)
        user_context = self._build_user_context(session_state)

        messages: list[dict[str, Any]] = []
        history = session_state.get("messages", [])
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": query})

        return {
            "system_prompt": system_prompt,
            "user_context": user_context,
            "messages": messages,
        }

    # ── System prompt ─────────────────────────────────────────────

    def _build_system_prompt(self, session_state: dict[str, Any]) -> str:
        parts: list[str] = []

        if self._base_system_prompt:
            parts.append(self._base_system_prompt)

        if self._skill_instructions:
            parts.append(self._skill_instructions)

        prompt = "\n\n".join(parts) if parts else ""

        prompt = self._inject_session_metadata(prompt, session_state)

        authorized_tools = session_state.get("authorized_tools", [])
        if authorized_tools:
            prompt = self._inject_permission_context(prompt, authorized_tools)

        diagnostics = self._build_diagnostics(session_state)
        if diagnostics:
            prompt = f"{prompt}\n\n{diagnostics}"

        return prompt

    # ── User context ──────────────────────────────────────────────

    def _build_user_context(self, session_state: dict[str, Any]) -> dict[str, Any]:
        ctx: dict[str, Any] = {}

        if "project" in session_state:
            ctx["project"] = session_state["project"]
        if "cwd" in session_state:
            ctx["cwd"] = session_state["cwd"]

        tools = session_state.get("authorized_tools", [])
        if tools:
            ctx["authorized_tools"] = tools

        env = session_state.get("env")
        if env:
            ctx["env"] = env

        return ctx

    # ── Injection helpers ─────────────────────────────────────────

    def _inject_session_metadata(
        self,
        prompt: str,
        session_state: dict[str, Any],
    ) -> str:
        meta_parts: list[str] = []
        session_id = session_state.get("session_id")
        if session_id:
            meta_parts.append(f"Session ID: {session_id}")

        turn_count = session_state.get("turn_count")
        if turn_count is not None:
            meta_parts.append(f"Turn: {turn_count}")

        model = session_state.get("model")
        if model:
            meta_parts.append(f"Model: {model}")

        if not meta_parts:
            return prompt

        block = "\n".join(meta_parts)
        return f"{prompt}\n\n<session>\n{block}\n</session>"

    def _inject_permission_context(
        self,
        prompt: str,
        authorized_tools: list[str],
    ) -> str:
        tool_list = ", ".join(authorized_tools)
        block = (
            "<permissions>\n"
            f"Pre-authorized tools: {tool_list}\n"
            "These tools may be used without additional user confirmation.\n"
            "</permissions>"
        )
        return f"{prompt}\n\n{block}"

    # ── Diagnostics ───────────────────────────────────────────────

    def _build_diagnostics(
        self,
        session_state: dict[str, Any],
    ) -> Optional[str]:
        diag = session_state.get("diagnostics")
        if not diag:
            return None

        lines: list[str] = ["<diagnostics>"]
        if isinstance(diag, dict):
            for key, value in diag.items():
                lines.append(f"  {key}: {value}")
        elif isinstance(diag, str):
            lines.append(f"  {diag}")
        else:
            lines.append(f"  {diag!r}")
        lines.append("</diagnostics>")
        return "\n".join(lines)
