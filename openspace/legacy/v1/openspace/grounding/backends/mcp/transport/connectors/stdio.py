"""
StdIO connector for MCP implementations.

This module provides a connector for communicating with MCP implementations
through the standard input/output streams.
"""

import sys

from mcp import ClientSession, StdioServerParameters

from openspace.utils.logging import Logger
from ..task_managers import StdioConnectionManager
from .base import MCPBaseConnector

logger = Logger.get_logger(__name__)


class StdioConnector(MCPBaseConnector):
    """Connector for MCP implementations using stdio transport.

    This connector uses the stdio transport to communicate with MCP implementations
    that are executed as child processes. It uses a connection manager to handle
    the proper lifecycle management of the stdio client.
    """

    def __init__(
        self,
        command: str = "npx",
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        errlog=None,
    ):
        """Initialize a new stdio connector.

        Args:
            command: The command to execute.
            args: Optional command line arguments.
            env: Optional environment variables.
            errlog: Stream to write error output to (defaults to filtered stderr).
                   StdioConnectionManager will wrap this to filter harmless errors.
        """
        self.command = command
        self.args = args or []  # Ensure args is never None
        
        # Ensure env is not None and add settings to suppress non-JSON output from servers
        self.env = env or {}
        # Add environment variables to encourage MCP servers to suppress non-JSON output
        # Many Node.js-based servers respect NODE_ENV=production
        if "NODE_ENV" not in self.env:
            self.env["NODE_ENV"] = "production"
        # Add flag to suppress informational messages (some servers respect this)
        if "MCP_SILENT" not in self.env:
            self.env["MCP_SILENT"] = "true"
        
        self.errlog = errlog
        
        # Create server parameters and connection manager
        # StdioConnectionManager will wrap errlog in FilteredStderrWrapper
        server_params = StdioServerParameters(command=self.command, args=self.args, env=self.env)
        connection_manager = StdioConnectionManager(server_params, self.errlog)
        super().__init__(connection_manager)

    async def _before_connect(self) -> None:
        """Log connection attempt."""
        logger.debug(f"Connecting to MCP implementation: {self.command}")

    async def _after_connect(self) -> None:
        """Create ClientSession and log success."""
        # Call parent's _after_connect to create the ClientSession
        await super()._after_connect()
        logger.debug(f"Successfully connected to MCP implementation: {self.command}")

    @property
    def public_identifier(self) -> dict[str, str]:
        return {"type": "stdio", "command&args": f"{self.command} {' '.join(self.args)}"}