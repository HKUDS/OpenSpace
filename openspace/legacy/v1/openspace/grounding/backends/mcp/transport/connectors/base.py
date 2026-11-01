"""
Base connector for MCP implementations.

This module provides the base connector interface that all MCP connectors must implement.
"""

import asyncio
from abc import abstractmethod
from typing import Any

from mcp import ClientSession
from mcp.shared.exceptions import McpError
from mcp.types import CallToolResult, GetPromptResult, Prompt, ReadResourceResult, Resource, Tool

from openspace.grounding.core.transport.task_managers import BaseConnectionManager
from openspace.grounding.core.transport.connectors import BaseConnector
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

# Default retry settings for tool calls
DEFAULT_TOOL_CALL_MAX_RETRIES = 3
DEFAULT_TOOL_CALL_RETRY_DELAY = 1.0


class MCPBaseConnector(BaseConnector[ClientSession]):
    """Base class for MCP connectors.

    This class defines the interface that all MCP connectors must implement.
    """

    def __init__(
        self, 
        connection_manager: BaseConnectionManager[ClientSession],
        tool_call_max_retries: int = DEFAULT_TOOL_CALL_MAX_RETRIES,
        tool_call_retry_delay: float = DEFAULT_TOOL_CALL_RETRY_DELAY,
    ):
        """Initialize base connector with common attributes.
        
        Args:
            connection_manager: The connection manager to use for the connection.
            tool_call_max_retries: Maximum number of retries for tool calls (default: 3)
            tool_call_retry_delay: Initial delay between retries in seconds (default: 1.0)
        """
        super().__init__(connection_manager)
        self.client_session: ClientSession | None = None
        self._tools: list[Tool] | None = None
        self._resources: list[Resource] | None = None
        self._prompts: list[Prompt] | None = None
        self.auto_reconnect = True  # Whether to automatically reconnect on connection loss (not configurable for now)
        self.tool_call_max_retries = tool_call_max_retries
        self.tool_call_retry_delay = tool_call_retry_delay

    @property
    @abstractmethod
    def public_identifier(self) -> str:
        """Get the identifier for the connector."""
        pass
    
    async def _get_streams_from_connection(self):
        """Get read and write streams from the connection. Override in subclasses if needed."""
        # Default implementation for most MCP connectors (stdio, HTTP)
        # Returns the connection directly as it should be a tuple of (read_stream, write_stream)
        return self._connection
    
    async def _after_connect(self) -> None:
        """Create ClientSession after connection is established.
        
        Some connectors (like WebSocket) don't use ClientSession and may override this method.
        """
        # Get streams from the connection
        streams = await self._get_streams_from_connection()
        
        if streams is None:
            # Some connectors (like WebSocket) don't use ClientSession
            # They should override this method to set up their own resources
            logger.debug("No streams returned, ClientSession creation skipped")
            return
        
        if isinstance(streams, tuple) and len(streams) == 2:
            read_stream, write_stream = streams
            # Create the client session
            self.client_session = ClientSession(read_stream, write_stream, sampling_callback=None)
            await self.client_session.__aenter__()
            logger.debug("MCP ClientSession created successfully")
        else:
            raise RuntimeError(f"Invalid streams format: expected tuple of 2 elements, got {type(streams)}")

    async def _before_disconnect(self) -> None:
        """Clean up MCP-specific resources before disconnection."""
        errors = []

        # Close the client session
        if self.client_session:
            try:
                logger.debug("Closing MCP client session")
                await self.client_session.__aexit__(None, None, None)
            except Exception as e:
                error_msg = f"Error closing client session: {e}"
                logger.warning(error_msg)
                errors.append(error_msg)
            finally:
                self.client_session = None

        # Reset tools, resources, and prompts
        self._tools = None
        self._resources = None
        self._prompts = None

        if errors:
            logger.warning(f"Encountered {len(errors)} errors during MCP resource cleanup")
    
    async def _cleanup_on_connect_failure(self) -> None:
        """Override to add MCP-specific cleanup on connection failure."""
        # Clean up client session if it was created
        if self.client_session:
            try:
                await self.client_session.__aexit__(None, None, None)
            except Exception:
                pass
            finally:
                self.client_session = None
        
        # Call parent cleanup
        await super()._cleanup_on_connect_failure()

    async def initialize(self) -> dict[str, Any]:
        """Initialize the MCP session and return session information."""
        if not self.client_session:
            raise RuntimeError("MCP client is not connected")

        logger.debug("Initializing MCP session")

        # Initialize the session
        result = await self.client_session.initialize()

        server_capabilities = result.capabilities

        if server_capabilities.tools:
            # Get available tools
            tools_result = await self.list_tools()
            self._tools = tools_result or []
        else:
            self._tools = []

        if server_capabilities.resources:
            # Get available resources
            resources_result = await self.list_resources()
            self._resources = resources_result or []
        else:
            self._resources = []

        if server_capabilities.prompts:
            # Get available prompts
            prompts_result = await self.list_prompts()
            self._prompts = prompts_result or []
        else:
            self._prompts = []

        logger.debug(
            f"MCP session initialized with {len(self._tools)} tools, "
            f"{len(self._resources)} resources, "
            f"and {len(self._prompts)} prompts"
        )

        return result

    @property
    def tools(self) -> list[Tool]:
        """Get the list of available tools."""
        if self._tools is None:
            raise RuntimeError("MCP client is not initialized")
        return self._tools

    @property
    def resources(self) -> list[Resource]:
        """Get the list of available resources."""
        if self._resources is None:
            raise RuntimeError("MCP client is not initialized")
        return self._resources

    @property
    def prompts(self) -> list[Prompt]:
        """Get the list of available prompts."""
        if self._prompts is None:
            raise RuntimeError("MCP client is not initialized")
        return self._prompts

    @property
    def is_connected(self) -> bool:
        """Check if the connector is actually connected and the connection is alive.

        This property checks not only the connected flag but also verifies that
        the client session exists and the underlying connection is still active.

        Returns:
            True if the connector is connected and the connection is alive, False otherwise.
        """
        # First check the basic connected flag
        if not self._connected:
            return False

        # Check if we have a client session
        if not self.client_session:
            self._connected = False
            return False

        # Check if connection manager task is still running (if applicable)
        if self._connection_manager and hasattr(self._connection_manager, "_task"):
            task = self._connection_manager._task
            if task and task.done():
                logger.debug("Connection manager task is done, marking as disconnected")
                self._connected = False
                return False

        return True

    async def _ensure_connected(self) -> None:
        """Ensure the connector is connected, reconnecting if necessary.

        Raises:
            RuntimeError: If connection cannot be established and auto_reconnect is False.
        """
        if not self.client_session:
            raise RuntimeError("MCP client is not connected")

        if not self.is_connected:
            if self.auto_reconnect:
                logger.debug("Connection lost, attempting to reconnect...")
                try:
                    await self.connect()
                    logger.debug("Reconnection successful")
                except Exception as e:
                    raise RuntimeError(f"Failed to reconnect to MCP server: {e}") from e
            else:
                raise RuntimeError(
                    "Connection to MCP server has been lost. Auto-reconnection is disabled. Please reconnect manually."
                )

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        """Call an MCP tool with automatic reconnection handling and retry logic.

        Args:
            name: The name of the tool to call.
            arguments: The arguments to pass to the tool.

        Returns:
            The result of the tool call.

        Raises:
            RuntimeError: If the connection is lost and cannot be reestablished.
            Exception: If the tool call fails after all retries.
        """
        last_error: Exception | None = None
        
        for attempt in range(self.tool_call_max_retries):
            # Ensure we're connected
            await self._ensure_connected()

            logger.debug(f"Calling tool '{name}' with arguments: {arguments} (attempt {attempt + 1}/{self.tool_call_max_retries})")
            try:
                result = await self.client_session.call_tool(name, arguments)
                logger.debug(f"Tool '{name}' called successfully")
                return result
            except Exception as e:
                last_error = e
                error_str = str(e).lower()
                
                # Check if the error might be due to connection loss
                if not self.is_connected:
                    logger.warning(f"Tool call '{name}' failed due to connection loss: {e}")
                    # Try to reconnect on next iteration
                    continue
                
                # Check for retryable HTTP errors (400, 500, 502, 503, 504)
                is_retryable = any(code in error_str for code in ['400', '500', '502', '503', '504', 'bad request', 'internal server error', 'service unavailable', 'gateway timeout'])
                
                if is_retryable and attempt < self.tool_call_max_retries - 1:
                    delay = self.tool_call_retry_delay * (2 ** attempt)  # Exponential backoff
                    logger.warning(
                        f"Tool call '{name}' failed with retryable error: {e}, "
                        f"retrying in {delay:.1f}s (attempt {attempt + 1}/{self.tool_call_max_retries})"
                    )
                    await asyncio.sleep(delay)
                    continue
                
                # Non-retryable error or max retries reached, re-raise
                raise
        
        # All retries exhausted
        error_msg = f"Tool call '{name}' failed after {self.tool_call_max_retries} retries"
        logger.error(error_msg)
        raise RuntimeError(error_msg) from last_error

    async def list_tools(self) -> list[Tool]:
        """List all available tools from the MCP implementation."""

        # Ensure we're connected
        await self._ensure_connected()

        logger.debug("Listing tools")
        try:
            result = await self.client_session.list_tools()
            return result.tools
        except McpError as e:
            logger.error(f"Error listing tools: {e}")
            return []

    async def list_resources(self) -> list[Resource]:
        """List all available resources from the MCP implementation."""
        # Ensure we're connected
        await self._ensure_connected()

        logger.debug("Listing resources")
        try:
            result = await self.client_session.list_resources()
            return result.resources
        except McpError as e:
            logger.error(f"Error listing resources: {e}")
            return []

    async def read_resource(self, uri: str) -> ReadResourceResult:
        """Read a resource by URI."""
        if not self.client_session:
            raise RuntimeError("MCP client is not connected")

        logger.debug(f"Reading resource: {uri}")
        result = await self.client_session.read_resource(uri)
        return result

    async def list_prompts(self) -> list[Prompt]:
        """List all available prompts from the MCP implementation."""
        # Ensure we're connected
        await self._ensure_connected()

        logger.debug("Listing prompts")
        try:
            result = await self.client_session.list_prompts()
            return result.prompts
        except McpError as e:
            logger.error(f"Error listing prompts: {e}")
            return []

    async def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> GetPromptResult:
        """Get a prompt by name."""
        # Ensure we're connected
        await self._ensure_connected()

        logger.debug(f"Getting prompt: {name}")
        result = await self.client_session.get_prompt(name, arguments)
        return result

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send a raw request to the MCP implementation."""
        # Ensure we're connected
        await self._ensure_connected()

        logger.debug(f"Sending request: {method} with params: {params}")
        return await self.client_session.request({"method": method, "params": params or {}})

    async def invoke(self, name: str, params: dict[str, Any]) -> Any:
        await self._ensure_connected()

        if not name.startswith("__"):
            return await self.call_tool(name, params)

        if name == "__read_resource__":
            return await self.read_resource(params["uri"])
        if name == "__list_prompts__":
            return await self.list_prompts()
        if name == "__get_prompt__":
            return await self.get_prompt(params["name"], params.get("args"))

        raise ValueError(f"Unsupported MCP invoke name: {name}")