"""Executor protocol used by ``GroundingClient.invoke_tool``.

This keeps ``grounding.core`` from importing the tool-runtime implementation
directly while preserving the invoke facade.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Protocol


class ToolExecutor(Protocol):
    async def run_tool_use(
        self,
        tool_call: dict[str, Any],
        tool_map: dict[str, Any],
        context: Any,
        **kwargs: Any,
    ) -> Any:
        """Execute one model tool call through the configured runtime."""


@dataclass(slots=True)
class ModuleToolExecutor:
    """Lazy module-backed executor for the canonical tool runtime."""

    runtime_module: str = "openspace.tool_runtime.pipeline.execution"

    async def run_tool_use(
        self,
        tool_call: dict[str, Any],
        tool_map: dict[str, Any],
        context: Any,
        **kwargs: Any,
    ) -> Any:
        module = importlib.import_module(self.runtime_module)
        runner = getattr(module, "run_tool_use")
        return await runner(tool_call, tool_map, context, **kwargs)


def get_default_tool_executor() -> ToolExecutor:
    return ModuleToolExecutor()
