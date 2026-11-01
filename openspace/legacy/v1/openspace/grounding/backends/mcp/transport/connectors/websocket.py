"""
WebSocket connector for MCP implementations.

This module provides a connector for communicating with MCP implementations
through WebSocket connections.
"""

import asyncio
import json
import uuid
from typing import Any

from mcp.types import Tool
from websockets import ClientConnection

from openspace.utils.logging import Logger
from openspace.grounding.core.transport.task_managers.base import BaseConnectionManager
from ..task_managers import WebSocketConnectionManager
from .base import MCPBaseConnector

logger = Logger.get_logger(__name__)


class WebSocketConnector(MCPBaseConnector):
    """Connector for MCP implementations using WebSocket transport.

    This connector uses WebSockets to communicate with remote MCP implementations,
    using a connection manager to handle the proper lifecycle management.
    """

    def __init__(
        self,
        url: str,
        auth_token: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        """Initialize a new WebSocket connector.

        Args:
            url: The WebSocket URL to connect to.
            auth_token: Optional authentication token.
            headers: Optional additional headers.
        """
        self.url = url
        self.auth_token = auth_token
        self.headers = headers or {}
        if auth_token:
            self.headers["Authorization"] = f"Bearer {auth_token}"

        self.ws: ClientConnection | None = None
        self._receiver_task: asyncio.Task | None = None
        self.pending_requests: dict[str, asyncio.Future] = {}
        self._tools: list[Tool] | None = None
        
        # Create connection manager with actual parameters
        connection_manager = WebSocketConnectionManager(self.url, self.headers)
        super().__init__(connection_manager)
        self._connected = False

    async def _get_streams_from_connection(self):
        """WebSocket doesn't use streams, return None to skip ClientSession creation."""
        return None
    
    async def _after_connect(self) -> None:
        """Set up WebSocket-specific resources after connection.
        
        WebSocket doesn't use ClientSession, so we skip the parent's implementation
        and set up WebSocket-specific resources instead.
        """
        # Store the WebSocket connection
        self.ws = self._connection
        
        # Start the message receiver task
        self._receiver_task = asyncio.create_task(self._receive_messages(), name="websocket_receiver_task")
        
        logger.debug(f"Successfully connected to MCP implementation via WebSocket: {self.url}")

    async def _receive_messages(self) -> None:
        """Continuously receive and process messages from the WebSocket."""
        if not self.ws:
            raise RuntimeError("WebSocket is not connected")

        try:
            async for message in self.ws:
                # Parse the message
                data = json.loads(message)

                # Check if this is a response to a pending request
                request_id = data.get("id")
                if request_id and request_id in self.pending_requests:
                    future = self.pending_requests.pop(request_id)
                    if "result" in data:
                        future.set_result(data["result"])
                    elif "error" in data:
                        future.set_exception(Exception(data["error"]))

                    logger.debug(f"Received response for request {request_id}")
                else:
                    logger.debug(f"Received message: {data}")
        except Exception as e:
            logger.error(f"Error in WebSocket message receiver: {e}")
            # If the websocket connection was closed or errored,
            # reject all pending requests
            for future in self.pending_requests.values():
                if not future.done():
                    future.set_exception(e)

    async def _before_disconnect(self) -> None:
        """Clean up WebSocket-specific resources before disconnection."""
        errors = []

        # First cancel the receiver task
        if self._receiver_task and not self._receiver_task.done():
            try:
                logger.debug("Cancelling WebSocket receiver task")
                self._receiver_task.cancel()
                try:
                    await self._receiver_task
                except asyncio.CancelledError:
                    logger.debug("WebSocket receiver task cancelled successfully")
                except Exception as e:
                    logger.warning(f"Error during WebSocket receiver task cancellation: {e}")
            except Exception as e:
                error_msg = f"Error cancelling WebSocket receiver task: {e}"
                logger.warning(error_msg)
                errors.append(error_msg)
            finally:
                self._receiver_task = None

        # Reject any pending requests
        if self.pending_requests:
            logger.debug(f"Rejecting {len(self.pending_requests)} pending requests")
            for future in self.pending_requests.values():
                if not future.done():
                    future.set_exception(ConnectionError("WebSocket disconnected"))
            self.pending_requests.clear()

        # Reset WebSocket and tools
        self.ws = None
        self._tools = None

        if errors:
            logger.warning(f"Encountered {len(errors)} errors during WebSocket resource cleanup")
    
    async def _cleanup_on_connect_failure(self) -> None:
        """Clean up WebSocket resources on connection failure."""
        # Cancel receiver task if it was started
        if self._receiver_task and not self._receiver_task.done():
            try:
                self._receiver_task.cancel()
                await self._receiver_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            finally:
                self._receiver_task = None
        
        # Reject pending requests
        for future in self.pending_requests.values():
            if not future.done():
                future.set_exception(ConnectionError("Connection failed"))
        self.pending_requests.clear()
        
        # Call parent cleanup
        await super()._cleanup_on_connect_failure()
        self.ws = None

    async def _send_request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send a request and wait for a response."""
        if not self.ws:
            raise RuntimeError("WebSocket is not connected")

        # Create a request ID
        request_id = str(uuid.uuid4())

        # Create a future to receive the response
        future = asyncio.Future()
        self.pending_requests[request_id] = future

        # Send the request
        await self.ws.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))

        logger.debug(f"Sent request {request_id} method: {method}")

        # Wait for the response
        try:
            return await future
        except Exception as e:
            # Remove the request from pending requests
            self.pending_requests.pop(request_id, None)
            logger.error(f"Error waiting for response to request {request_id}: {e}")
            raise

    async def initialize(self) -> dict[str, Any]:
        """Initialize the MCP session and return session information."""
        logger.debug("Initializing MCP session")
        result = await self._send_request("initialize")

        # Get available tools
        tools_result = await self.list_tools()
        self._tools = [Tool(**tool) for tool in tools_result]

        logger.debug(f"MCP session initialized with {len(self._tools)} tools")
        return result

    async def list_tools(self) -> list[dict[str, Any]]:
        """List all available tools from the MCP implementation."""
        logger.debug("Listing tools")
        result = await self._send_request("tools/list")
        return result.get("tools", [])

    @property
    def tools(self) -> list[Tool]:
        """Get the list of available tools."""
        if not self._tools:
            raise RuntimeError("MCP client is not initialized")
        return self._tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call an MCP tool with the given arguments."""
        logger.debug(f"Calling tool '{name}' with arguments: {arguments}")
        return await self._send_request("tools/call", {"name": name, "arguments": arguments})

    async def list_resources(self) -> list[dict[str, Any]]:
        """List all available resources from the MCP implementation."""
        logger.debug("Listing resources")
        result = await self._send_request("resources/list")
        return result

    async def read_resource(self, uri: str) -> tuple[bytes, str]:
        """Read a resource by URI."""
        logger.debug(f"Reading resource: {uri}")
        result = await self._send_request("resources/read", {"uri": uri})
        return result.get("content", b""), result.get("mimeType", "")

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send a raw request to the MCP implementation."""
        logger.debug(f"Sending request: {method} with params: {params}")
        return await self._send_request(method, params)

    @property
    def public_identifier(self) -> str:
        """Get the identifier for the connector."""
        return {"type": "websocket", "url": self.url}
