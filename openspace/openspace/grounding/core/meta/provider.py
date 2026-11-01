import os
import time
from typing import TYPE_CHECKING, Any, Dict, List

from ..exceptions import ErrorCode, GroundingError
from ..provider import Provider
from ..types import BackendType, SessionConfig
from .tool import META_TOOLS, _BaseMetaTool

if TYPE_CHECKING:
    from ..grounding_client import GroundingClient


class MetaProvider(Provider):
    """Provider for internal meta-level query tools."""

    def __init__(self, client: "GroundingClient"):
        super().__init__(BackendType.META, {})
        self._client = client
        self._tools: List[_BaseMetaTool] = [tool_cls(client) for tool_cls in META_TOOLS]

    async def initialize(self):
        self.is_initialized = True

    async def create_session(self, session_config: SessionConfig):
        raise GroundingError(
            "MetaProvider does not support sessions",
            code=ErrorCode.CONFIG_INVALID,
        )

    async def list_tools(self, session_name: str | None = None):
        return self._tools

    async def call_tool(
        self,
        session_name: str,
        tool_name: str,
        parameters: Dict[str, Any] | None = None,
    ):
        tool_map = {t.schema.name: t for t in self._tools}
        if tool_name not in tool_map:
            raise GroundingError(
                f"Meta tool '{tool_name}' not found",
                code=ErrorCode.TOOL_NOT_FOUND,
            )
        from openspace.tool_runtime.direct_context import build_direct_tool_use_context
        from openspace.tool_runtime.pipeline.execution import (
            run_tool_use,
            tool_call_result_to_tool_result,
        )

        tool_call = {
            "id": f"meta-call-{time.time_ns()}",
            "type": "function",
            "function": {"name": tool_name, "arguments": parameters or {}},
        }
        context = build_direct_tool_use_context(
            tools=list(self._tools),
            all_tools=list(self._tools),
            model="meta-provider",
            cwd=os.getcwd(),
            agent_id="meta-provider",
            recording_manager=getattr(self._client, "recording_manager", None),
            quality_manager=getattr(self._client, "quality_manager", None),
            tui_available=False,
        )
        pipeline_result = await run_tool_use(tool_call, tool_map, context)
        return tool_call_result_to_tool_result(pipeline_result)

    async def close_session(self, session_name: str) -> None:
        return
