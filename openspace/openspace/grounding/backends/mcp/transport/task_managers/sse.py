"""
SSE connection management for MCP implementations.

This module provides a connection manager for SSE-based MCP connections
that ensures proper task isolation and resource cleanup.
"""

from typing import Any, Tuple
from mcp.client.sse import sse_client
from openspace.utils.logging import Logger
from openspace.grounding.core.transport.task_managers import (
    AsyncContextConnectionManager,
)

logger = Logger.get_logger(__name__)


class SseConnectionManager(AsyncContextConnectionManager[Tuple[Any, Any], ...]):
    """Connection manager for SSE-based MCP connections.

    This class handles the proper task isolation for sse_client context managers
    to prevent the "cancel scope in different task" error. It runs the sse_client
    in a dedicated task and manages its lifecycle.
    """

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 5,
        sse_read_timeout: float = 60 * 5,
    ):
        """Initialize a new SSE connection manager.

        Args:
            url: The SSE endpoint URL
            headers: Optional HTTP headers
            timeout: Timeout for HTTP operations in seconds
            sse_read_timeout: Timeout for SSE read operations in seconds
        """
        super().__init__(
            sse_client,
            url=url,
            headers=headers or {},
            timeout=timeout,
            sse_read_timeout=sse_read_timeout,
        )
        self.url = url
        self.headers = headers or {}
        logger.debug("SseConnectionManager init url=%s", url)
