"""
Session manager for MCP connections.

This module provides a session manager for MCP connections,
which handles authentication, initialization, and tool discovery.
"""

from typing import Any, Dict

from openspace.grounding.backends.mcp.transport.connectors import MCPBaseConnector
from openspace.grounding.backends.mcp.tool_converter import convert_mcp_tool_to_base_tool
from openspace.grounding.core.session import BaseSession
from openspace.grounding.core.types import BackendType
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


class MCPSession(BaseSession):
    """Session manager for MCP connections.

    This class manages the lifecycle of an MCP connection, including
    authentication, initialization, and tool discovery.
    """

    def __init__(
        self,
        connector: MCPBaseConnector,
        *,
        session_id: str = "",
        auto_connect: bool = True,
        auto_initialize: bool = True,
    ) -> None:
        """Initialize a new MCP session.

        Args:
            connector: The connector to use for communicating with the MCP implementation.
            session_id: Unique identifier for this session
            auto_connect: Whether to automatically connect to the MCP implementation.
            auto_initialize: Whether to automatically initialize the session.
        """
        super().__init__(
            connector=connector,
            session_id=session_id,
            backend_type=BackendType.MCP,
            auto_connect=auto_connect,
            auto_initialize=auto_initialize,
        )

    async def initialize(self) -> Dict[str, Any]:
        """Initialize the MCP session and discover available tools.

        Returns:
            The session information returned by the MCP implementation.
        """
        # Make sure we're connected
        if not self.is_connected and self.auto_connect:
            await self.connect()

        # Initialize the session through connector
        logger.debug(f"Initializing MCP session {self.session_id}")
        session_info = await self.connector.initialize()

        # List tools from MCP server and convert to BaseTool
        mcp_tools = self.connector.tools  # MCPBaseConnector caches tools after initialize
        logger.debug(f"Converting {len(mcp_tools)} MCP tools to BaseTool")
        
        self.tools = [
            convert_mcp_tool_to_base_tool(mcp_tool, self.connector)
            for mcp_tool in mcp_tools
        ]
        
        logger.debug(f"MCP session {self.session_id} initialized with {len(self.tools)} tools")

        return session_info