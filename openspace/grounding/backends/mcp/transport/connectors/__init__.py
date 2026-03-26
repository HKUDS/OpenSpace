"""
Connectors for various MCP transports.

This module provides interfaces for connecting to MCP implementations
through different transport mechanisms.
"""

from importlib.util import find_spec

from .base import MCPBaseConnector  # noqa: F401
from .http import HttpConnector  # noqa: F401
from .sandbox import SandboxConnector  # noqa: F401
from .stdio import StdioConnector  # noqa: F401

if find_spec("websockets") is not None:
    from .websocket import WebSocketConnector  # noqa: F401
else:
    class WebSocketConnector:
        """Fallback connector when optional websocket dependency is unavailable."""

        def __init__(self, *args, **kwargs):
            raise ImportError(
                "WebSocket MCP transport requires optional dependency 'websockets'. "
                "Install it with: pip install websockets"
            )

__all__ = [
    "MCPBaseConnector",
    "StdioConnector",
    "HttpConnector",
    "WebSocketConnector",
    "SandboxConnector",
]
