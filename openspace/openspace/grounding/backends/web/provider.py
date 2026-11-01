from typing import Dict, Any
from openspace.config import get_config
from openspace.grounding.core.types import BackendType, SessionConfig
from openspace.grounding.core.provider import Provider
from .session import WebSession
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


class WebProvider(Provider[WebSession]):
    
    DEFAULT_SID = BackendType.WEB.value
    
    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(BackendType.WEB, config)
    
    async def initialize(self) -> None:
        """Initialize Web Provider and create default session"""
        if not self.is_initialized:
            logger.info("Initializing Web provider (WebSearch/WebFetch)")
            # Auto-create default session
            await self.create_session(SessionConfig(
                session_name=self.DEFAULT_SID,
                backend_type=BackendType.WEB,
                connection_params={}
            ))
            self.is_initialized = True
    
    async def create_session(self, session_config: SessionConfig) -> WebSession:
        """Create Web session and pass WebConfig through to the tools."""
        session_name = session_config.session_name
        
        if session_name in self._sessions:
            logger.warning(f"Session {session_name} already exists, returning existing session")
            return self._sessions[session_name]
        
        web_config = self.config or get_config().get_backend_config(BackendType.WEB.value)

        # Create WebSession with auto-connect and auto-initialize enabled.
        # WebSession uses a no-op connector, so this never requires API keys
        # or network access just to list tools.
        session = WebSession(
            session_id=session_name,
            config=session_config,
            web_config=web_config,
            auto_connect=True,
            auto_initialize=True
        )
        
        self._sessions[session_name] = session
        
        logger.info(f"Created Web session (WebSearch/WebFetch): {session_name}")
        return session
    
    async def close_session(self, session_name: str) -> None:
        """Close Web session"""
        session = self._sessions.pop(session_name, None)
        if session:
            await session.disconnect()
            logger.info(f"Closed Web session: {session_name}")
