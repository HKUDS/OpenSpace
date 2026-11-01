from typing import Any
from .base import BaseConnectionManager


class PlaceholderConnectionManager(BaseConnectionManager[Any]):
    """A placeholder connection manager that does nothing.
    
    This is used by connectors that set up their real connection manager
    during the connect() phase.
    """
    
    async def _establish_connection(self) -> Any:
        """Establish the connection (placeholder implementation)."""
        raise NotImplementedError("PlaceholderConnectionManager should be replaced before use")
    
    async def _close_connection(self) -> None:
        """Close the connection (placeholder implementation)."""
        pass