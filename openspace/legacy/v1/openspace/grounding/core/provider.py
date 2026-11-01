"""
provider is to manage sessions of a backend, if the backend is mcp, then provider will manage sessions through servers
"""
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional, Generic, TypeVar

from .tool import BaseTool
from .types import BackendType, SessionConfig, ToolResult, ToolStatus
from .session import BaseSession
from .security.policies import SecurityPolicyManager
from openspace.config import get_config
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)
TSession = TypeVar('TSession', bound=BaseSession)


class Provider(ABC, Generic[TSession]):
    """Backend provider base class"""  
    def __init__(self, backend_type: BackendType, config: Dict[str, Any] = None):
        self.backend_type = backend_type
        self.config = config or {}
        self.is_initialized = False
        self._sessions: Dict[str, TSession] = {}  # session management
        self._session_counter: int = 0
        self.security_manager = SecurityPolicyManager()
        
        self._setup_security_policy(config)
        
    def _setup_security_policy(self, config: dict | None = None):   
        security_policy = get_config().get_security_policy(self.backend_type.value)
        self.security_manager.set_backend_policy(BackendType.SHELL, security_policy)
        
    async def ensure_initialized(self) -> None:
        """
         Internal helper.  Guarantee that `initialize()` has been executed
        """
        if not self.is_initialized:
            await self.initialize()
        
    @abstractmethod
    async def initialize(self) -> None:
        """Initialize provider, call `create_session` to create all sessions if not exist        
        Subclasses should set `self.is_initialized = True` after successful initialization
        """
        pass
    
    @abstractmethod
    async def create_session(self, session_config: SessionConfig) -> TSession:
        """Create session, update _sessions"""
        pass

    @abstractmethod
    async def close_session(self, session_name: str) -> None:
        """Close session"""
        pass
    
    def list_sessions(self) -> List[str]:
        """Get all session IDs"""
        return list(self._sessions.keys())
    
    def get_session(self, session_name: str) -> Optional[TSession]:
        """Get session object by ID"""
        return self._sessions.get(session_name)
    
    async def close_all_sessions(self) -> None:
        """Provider shutdown cleanup"""
        for session_name in list(self._sessions.keys()):
            try:
                await self.close_session(session_name)
            except Exception as e:
                print(f"Error closing session {session_name}: {e}")
        
        self._sessions.clear()
        self.is_initialized = False

    def __repr__(self) -> str:
        return (f"Provider(backend={self.backend_type.value}, "
                f"initialized={self.is_initialized}, "
                f"sessions={len(self._sessions)}, "
                f"config_items={len(self.config)})")
        
    async def list_tools(self, session_name: Optional[str] = None) -> List[BaseTool]:
        """
        Return BaseTool list.
        If session_name is specified, only return the tools of the specified session. 
        If session_name is not specified, return all tools of all sessions.
        """
        await self.ensure_initialized()
        
        if session_name:
            session = self._sessions.get(session_name)
            return await session.list_tools() if session else []

        tools: list[BaseTool] = []
        for sess in self._sessions.values():
            tools.extend(await sess.list_tools())
        return tools
    
    async def call_tool(
        self,
        session_name: str,
        tool_name: str,
        parameters: Dict[str, Any] | None = None,
    ) -> ToolResult:
        
        await self.ensure_initialized()
        parameters = parameters or {}

        session = self._sessions.get(session_name)
        if session is None:
            return ToolResult(
                status=ToolStatus.ERROR,
                content="",
                error=f"Session '{session_name}' not found",
                metadata={"session_name": session_name, "tool_name": tool_name},
            )

        try:
            return await session.call_tool(tool_name, parameters)
        except Exception as e:
            logger.error("Execute tool error: %s @%s - %s", tool_name, session_name, e)
            return ToolResult(
                status=ToolStatus.ERROR,
                content="",
                error=str(e),
                metadata={"session_name": session_name, "tool_name": tool_name},
            )


class ProviderRegistry:
    """
    Maintain mapping of BackendType -> Provider, and provide dynamic registration / retrieval capabilities
    """
    def __init__(self) -> None:
        self._providers: dict[BackendType, Provider] = {}

    def register(self, provider: "Provider") -> None:
        self._providers[provider.backend_type] = provider
        logger.debug("Provider for %s registered", provider.backend_type)

    def get(self, backend: BackendType) -> "Provider":
        if backend not in self._providers: 
            raise KeyError(f"Provider for '{backend.value}' not registered")
        return self._providers[backend]

    def list(self) -> dict[BackendType, "Provider"]:
        return dict(self._providers)