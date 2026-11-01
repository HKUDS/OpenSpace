"""No-op connection manager for local (in-process) connectors.

Local connectors execute commands directly via subprocess, so they don't
need a real network connection. This manager satisfies the
BaseConnectionManager interface that BaseConnector requires.
"""
import asyncio
from typing import Any
from .base import BaseConnectionManager


class NoOpConnectionManager(BaseConnectionManager[Any]):
    """Connection manager that immediately reports 'ready' without
    establishing any real connection.
    
    Used by LocalShellConnector and LocalGUIConnector.
    """

    async def _establish_connection(self) -> Any:
        """No-op: return a sentinel value."""
        return True

    async def _close_connection(self) -> None:
        """No-op: nothing to close."""
        pass

