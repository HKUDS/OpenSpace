"""
WebSocket connection management for MCP implementations.

This module provides a connection manager for WebSocket-based MCP connections.
"""

from typing import Any, Tuple
from mcp.client.websocket import websocket_client
from openspace.utils.logging import Logger
from openspace.grounding.core.transport.task_managers import (
    AsyncContextConnectionManager,
)

logger = Logger.get_logger(__name__)

class WebSocketConnectionManager(
    AsyncContextConnectionManager[Tuple[Any, Any], ...]
):

    def __init__(self, url: str, headers: dict[str, str] | None = None):
        # Note: The current MCP websocket_client implementation doesn't support headers
        # If headers need to be passed, this would need to be updated when MCP supports it
        super().__init__(websocket_client, url)
        self.url = url
        self.headers = headers or {}
        logger.debug("WebSocketConnectionManager init url=%s", url)