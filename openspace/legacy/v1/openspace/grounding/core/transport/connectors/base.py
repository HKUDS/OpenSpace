"""
Base connector abstraction.

A connector is a very thin wrapper-class that owns a *connection manager*
(e.g. AioHttpConnectionManager, AsyncContextConnectionManager, …).
It exposes a unified `connect / disconnect / is_connected` lifecycle and
defines an abstract `request()` method which concrete back-ends must
implement.
"""
import asyncio
from abc import ABC, abstractmethod
from typing import Any, Generic, TypeVar, Type
from pydantic import BaseModel
from ..task_managers import BaseConnectionManager

T = TypeVar("T")        # The object returned by manager.start(): session / connection


class BaseConnector(ABC, Generic[T]):
    """
    Generic connector that delegates the heavy lifting to the supplied
    *connection manager*. Concrete subclasses only need to implement
    their own `request()` method.
    """

    def __init__(self, connection_manager: BaseConnectionManager[T]):
        self._connection_manager = connection_manager        # e.g. AioHttpConnectionManager instance
        # The raw connection object returned by the manager, for reusing the established long-term connection
        self._connection: T | None = None
        self._connected = False

    async def connect(self) -> None:
        """Create the underlying session/connection via the manager."""
        if self._connected:
            return
        
        try:
            # Hook: before connection
            await self._before_connect()
            
            # Start the connection manager
            self._connection = await self._connection_manager.start()
            
            # Hook: after connection established
            await self._after_connect()
            
            # Mark as connected
            self._connected = True
        except Exception:
            # Clean up on failure
            await self._cleanup_on_connect_failure()
            raise

    async def disconnect(self) -> None:
        """Close the session/connection and reset state.
        
        Ensures proper cleanup of all resources including aiohttp sessions.
        """
        if not self._connected:
            return
        
        # Hook: before disconnection
        await self._before_disconnect()
        
        # Stop the connection manager
        if self._connection_manager:
            await self._connection_manager.stop()
            self._connection = None
        
        # Hook: after disconnection
        await self._after_disconnect()
        
        self._connected = False

    async def _before_connect(self) -> None:
        """Hook called before establishing connection. Override in subclasses if needed."""
        pass

    async def _after_connect(self) -> None:
        """Hook called after connection is established. Override in subclasses if needed."""
        pass

    async def _cleanup_on_connect_failure(self) -> None:
        """Hook called when connection fails. Override in subclasses if needed."""
        if self._connection_manager:
            try:
                await self._connection_manager.stop()
            except Exception:
                pass
        self._connection = None

    async def _before_disconnect(self) -> None:
        """Hook called before disconnection. Override in subclasses if needed."""
        pass

    async def _after_disconnect(self) -> None:
        """Hook called after disconnection. Override in subclasses if needed."""
        pass

    @property
    def is_connected(self) -> bool:
        """Return True iff `connect()` has completed successfully."""
        return self._connected

    @staticmethod
    def _to_json_compatible(obj: Any) -> Any:
        """
        Convert a Pydantic BaseModel to a JSON-serialisable dict (by_alias=True).
        Leave all other types unchanged.
        """
        if isinstance(obj, BaseModel):
            return obj.model_dump(by_alias=True)
        return obj

    @staticmethod
    def _parse_as(data: Any, model_cls: "Type[BaseModel] | None" = None) -> Any:
        """
        Try to parse *data* into *model_cls* (a subclass of BaseModel).  
        If `model_cls` is None or not a subclass of BaseModel, return the original data.
        """
        if model_cls is None:
            return data
        if isinstance(model_cls, type) and issubclass(model_cls, BaseModel):
            return model_cls.model_validate(data)
        return data
    
    @abstractmethod
    async def invoke(self, name: str, params: dict[str, Any]) -> Any:
        """
        Unified RPC entry for all tools.
        Sub-class maps this to its actual RPC like call_tool / run_cmd.
        """
        raise NotImplementedError

    @abstractmethod
    async def request(self, *args: Any, **kwargs: Any) -> Any:
        """Abstract RPC / HTTP / WS request method to be implemented by child classes."""
        raise NotImplementedError("This connector has not implemented 'request'")