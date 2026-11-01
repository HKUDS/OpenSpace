from typing import List, Dict, Any
from ..provider import Provider
from ..types import BackendType, SessionConfig
from ..grounding_client import GroundingClient
from .tool import SYSTEM_TOOLS, _BaseSystemTool
from ..exceptions import GroundingError, ErrorCode


class SystemProvider(Provider):
    """
    Provider for system-level query tools
    """
    def __init__(self, client: GroundingClient):
        super().__init__(BackendType.SYSTEM, {})
        # Instantiates all system tools
        self._tools: List[_BaseSystemTool] = [tool_cls(client) for tool_cls in SYSTEM_TOOLS]

    async def initialize(self): 
        self.is_initialized = True

    async def create_session(self, session_config: SessionConfig):
        raise GroundingError(
            "SystemProvider does not support sessions",
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
                f"System tool '{tool_name}' not found",
                code=ErrorCode.TOOL_NOT_FOUND,
            )
        return await tool_map[tool_name].arun(**(parameters or {}))

    async def close_session(self, session_name: str) -> None:
        return