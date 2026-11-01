from typing import Dict, Any, Union
from openspace.grounding.core.types import BackendType, SessionConfig
from openspace.grounding.core.provider import Provider
from openspace.grounding.core.session import BaseSession
from openspace.config import get_config
from openspace.config.utils import get_config_value
from openspace.platforms import get_local_server_config
from openspace.utils.logging import Logger
from .transport.connector import GUIConnector
from .transport.local_connector import LocalGUIConnector
from .session import GUISession

logger = Logger.get_logger(__name__)


class GUIProvider(Provider):
    """
    Provider for GUI desktop environment.
    Manages communication with desktop_env through HTTP API or local in-process execution.
    
    Supports two modes:
    - "local": Execute GUI operations directly in-process (no server needed)
    - "server": Connect to a running local_server via HTTP API
    
    Supports automatic default session creation:
    - If no session exists, a default session will be created on first use
    - Default session uses configuration from config file or environment
    """
    
    DEFAULT_SID = BackendType.GUI.value
    
    def __init__(self, config: Dict[str, Any] = None):
        """
        Initialize GUI provider.
        
        Args:
            config: Provider configuration
        """
        super().__init__(BackendType.GUI, config)
        self.connectors: Dict[str, Union[GUIConnector, LocalGUIConnector]] = {}
    
    async def initialize(self) -> None:
        """
        Initialize the provider and create default session.
        """
        if not self.is_initialized:
            logger.info("Initializing GUI provider")
            # Auto-create default session
            await self.create_session(SessionConfig(
                session_name=self.DEFAULT_SID,
                backend_type=BackendType.GUI,
                connection_params={}
            ))
            self.is_initialized = True
    
    async def create_session(self, session_config: SessionConfig) -> BaseSession:
        """
        Create GUI session.
        
        Args:
            session_config: Session configuration
        
        Returns:
            GUISession instance
        """
        # Load GUI backend configuration
        gui_config = get_config().get_backend_config("gui")
        
        # Determine execution mode: "local" or "server"
        mode = getattr(gui_config, "mode", "local")
        
        # Extract connection parameters
        conn_params = session_config.connection_params
        timeout = get_config_value(conn_params, 'timeout', gui_config.timeout)
        retry_times = get_config_value(conn_params, 'retry_times', gui_config.max_retries)
        retry_interval = get_config_value(conn_params, 'retry_interval', gui_config.retry_interval)
        
        # Build pkgs_prefix with failsafe setting
        failsafe_str = "True" if gui_config.failsafe else "False"
        pkgs_prefix = get_config_value(
            conn_params, 
            'pkgs_prefix', 
            gui_config.pkgs_prefix.format(failsafe=failsafe_str, command="{command}")
        )
        
        if mode == "local":
            # ---------- LOCAL MODE ----------
            logger.info("GUI backend using LOCAL mode (no server required)")
            connector = LocalGUIConnector(
                timeout=timeout,
                retry_times=retry_times,
                retry_interval=retry_interval,
                pkgs_prefix=pkgs_prefix,
            )
        else:
            # ---------- SERVER MODE ----------
            logger.info("GUI backend using SERVER mode (connecting to local_server)")
            local_server_config = get_local_server_config()
            vm_ip = get_config_value(conn_params, 'vm_ip', local_server_config['host'])
            server_port = get_config_value(conn_params, 'server_port', local_server_config['port'])
            
            connector = GUIConnector(
                vm_ip=vm_ip,
                server_port=server_port,
                timeout=timeout,
                retry_times=retry_times,
                retry_interval=retry_interval,
                pkgs_prefix=pkgs_prefix,
            )
        
        # Create session
        session = GUISession(
            connector=connector,
            session_id=session_config.session_name,
            backend_type=BackendType.GUI,
            config=session_config,
        )
        
        # Store connector and session
        self.connectors[session_config.session_name] = connector
        self._sessions[session_config.session_name] = session
        
        logger.info(f"Created GUI session: {session_config.session_name} (mode={mode})")
        return session
    
    async def close_session(self, session_name: str) -> None:
        """
        Close GUI session.
        
        Args:
            session_name: Name of the session to close
        """
        if session_name in self._sessions:
            session = self._sessions[session_name]
            await session.disconnect()
            del self._sessions[session_name]
            
        if session_name in self.connectors:
            connector = self.connectors[session_name]
            await connector.disconnect()
            del self.connectors[session_name]
        
        logger.info(f"Closed GUI session: {session_name}")