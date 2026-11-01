from abc import ABC, abstractmethod
from typing import Any, Dict, List
from datetime import datetime

from .tool import BaseTool
from .transport.connectors import BaseConnector
from .types import SessionInfo, SessionStatus, BackendType, ToolResult
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


class BaseSession(ABC):
    """
    Session manager for all backends.
    """
    def __init__(
        self,
        connector: BaseConnector,
        *,
        session_id: str,
        backend_type: BackendType | None = None,
        auto_connect: bool = True,
        auto_initialize: bool = True,
    ) -> None:
        self.connector = connector
        self.session_id = session_id
        self.backend_type = backend_type or BackendType.NOT_SET
        self.auto_connect = auto_connect
        self.auto_initialize = auto_initialize

        self.status: SessionStatus = SessionStatus.DISCONNECTED
        self.session_info: Dict[str, Any] | None = None
        self._created_at = datetime.utcnow()
        self._last_activity = self._created_at
        self.tools: List[BaseTool] = []

    async def __aenter__(self) -> "BaseSession":
        if self.auto_connect:
            await self.connect()
        if self.auto_initialize:
            self.session_info = await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit the async context manager.

        Args:
            exc_type: The exception type, if an exception was raised.
            exc_val: The exception value, if an exception was raised.
            exc_tb: The exception traceback, if an exception was raised.
        """
        await self.disconnect()

    async def connect(self) -> None:
        if self.connector.is_connected:
            return
        self.status = SessionStatus.CONNECTING
        await self.connector.connect()
        self.status = SessionStatus.CONNECTED

    async def disconnect(self) -> None:
        if not self.connector.is_connected:
            return
        await self.connector.disconnect()
        self.status = SessionStatus.DISCONNECTED

    @property
    def is_connected(self) -> bool:
        return self.connector.is_connected

    @abstractmethod
    async def initialize(self) -> Dict[str, Any]:
        """
        Negotiate with the backend, discover tools, etc.
        Return session information (can be an empty dict).
        
        `self.tools` need to be set in this method.
        """
        raise NotImplementedError("Sub-class must implement this method")
    
    async def list_tools(self) -> List[BaseTool]:
        """
        Return tools discovered during `initialize()`.
        """
        if not self.tools:
            self.session_info = await self.initialize()
        return self.tools
    
    async def call_tool(self, tool_name: str, parameters=None) -> ToolResult:
        parameters = parameters or {}
        
        # Ensure tools are initialized before calling
        if not self.tools:
            logger.debug(f"Tools not initialized for session {self.session_id}, initializing now...")
            self.session_info = await self.initialize()
        
        tool_map = {t.schema.name: t for t in self.tools}
        if tool_name not in tool_map:
            raise ValueError(f"Unknown tool: {tool_name}")
        result = await tool_map[tool_name].arun(**parameters)
        self._touch()
        return result
 
    # Update when a successful call is made
    def _touch(self):
        self._last_activity = datetime.utcnow()

    @property
    def info(self) -> SessionInfo:
        return SessionInfo(
            session_id=self.session_id,
            backend_type=getattr(self, "backend_type", BackendType.NOT_SET),
            status=self.status,
            created_at=self._created_at,
            last_activity=self._last_activity,
            metadata=self.session_info or {},
        )