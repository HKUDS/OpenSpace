from typing import Dict, Any, Union
import os
from openspace.grounding.core.session import BaseSession
from openspace.grounding.core.types import BackendType, SessionStatus, SessionConfig
from openspace.utils.logging import Logger
from .transport.connector import GUIConnector
from .transport.local_connector import LocalGUIConnector
from .tool import GUIAgentTool
from .config import build_llm_config

logger = Logger.get_logger(__name__)


class GUISession(BaseSession):
    """
    Session for GUI desktop environment.
    Manages connection and tools for GUI automation.
    """
    
    def __init__(
        self,
        connector: Union[GUIConnector, LocalGUIConnector],
        session_id: str,
        backend_type: BackendType.GUI,
        config: SessionConfig,
        auto_connect: bool = True,
        auto_initialize: bool = True,
    ):
        """
        Initialize GUI session.
        
        Args:
            connector: GUI HTTP connector
            session_id: Unique session identifier
            backend_type: Backend type (GUI)
            config: Session configuration
            auto_connect: Auto-connect on context enter
            auto_initialize: Auto-initialize on context enter
        """
        super().__init__(
            connector=connector,
            session_id=session_id,
            backend_type=backend_type,
            auto_connect=auto_connect,
            auto_initialize=auto_initialize,
        )
        self.config = config
        self.gui_connector = connector
    
    async def initialize(self) -> Dict[str, Any]:
        """
        Initialize session: connect and discover tools.
        
        Returns:
            Session information dict
        """
        logger.info(f"Initializing GUI session: {self.session_id}")
        
        # Ensure connected
        if not self.connector.is_connected:
            await self.connect()
        
        # Create LLM client if configured
        llm_client = None
        user_llm_config = self.config.connection_params.get("llm_config")
        
        # Build complete LLM config with auto-detection
        # If user provides llm_config, merge with auto-detected values
        # If user doesn't provide llm_config, try to auto-build one if ANTHROPIC_API_KEY exists
        if user_llm_config or os.environ.get("ANTHROPIC_API_KEY"):
            llm_config = build_llm_config(user_llm_config)
            
            if llm_config.get("type") == "anthropic":
                # Check if API key is available
                if not llm_config.get("api_key"):
                    logger.warning(
                        "Anthropic API key not found. Skipping LLM client initialization. "
                        "Set ANTHROPIC_API_KEY environment variable or provide api_key in llm_config."
                    )
                else:
                    try:
                        from .anthropic_client import AnthropicGUIClient
                        
                        # Detect actual screen size from screenshot (most accurate)
                        # PyAutoGUI may report logical resolution, but we need the actual screenshot size
                        try:
                            screenshot_bytes = await self.gui_connector.get_screenshot()
                            if screenshot_bytes:
                                from PIL import Image
                                import io
                                img = Image.open(io.BytesIO(screenshot_bytes))
                                actual_screen_size = img.size
                                logger.info(f"Auto-detected screen size from screenshot: {actual_screen_size}")
                                screen_size = actual_screen_size
                            else:
                                raise RuntimeError("Could not get screenshot")
                        except Exception as e:
                            # Fallback to pyautogui detection
                            actual_screen_size = await self.gui_connector.get_screen_size()
                            if actual_screen_size:
                                logger.info(f"Auto-detected screen size from pyautogui: {actual_screen_size}")
                                screen_size = actual_screen_size
                            else:
                                # Final fallback to configured value
                                screen_size = llm_config.get("screen_size", (1920, 1080))
                                logger.warning(f"Could not auto-detect screen size, using configured: {screen_size}")
                        
                        # Detect PyAutoGUI working size (logical pixels)
                        pyautogui_size = await self.gui_connector.get_screen_size()
                        if pyautogui_size:
                            logger.info(f"PyAutoGUI working size (logical): {pyautogui_size}")
                        else:
                            # If we can't detect PyAutoGUI size, assume it's the same as screen size
                            pyautogui_size = screen_size
                            logger.warning(f"Could not detect PyAutoGUI size, assuming same as screen: {pyautogui_size}")
                        
                        llm_client = AnthropicGUIClient(
                            model=llm_config["model"],
                            platform=llm_config["platform"],
                            api_key=llm_config["api_key"],
                            provider=llm_config["provider"],
                            screen_size=screen_size,
                            pyautogui_size=pyautogui_size,
                            max_tokens=llm_config["max_tokens"],
                            only_n_most_recent_images=llm_config["only_n_most_recent_images"],
                        )
                        logger.info(
                            f"Initialized Anthropic LLM client - "
                            f"Model: {llm_config['model']}, Platform: {llm_config['platform']}"
                        )
                    except Exception as e:
                        logger.warning(f"Failed to initialize Anthropic client: {e}")
        
        # Get recording_manager from connection_params if available
        recording_manager = self.config.connection_params.get("recording_manager")
        
        # Create GUI Agent Tool
        self.tools = [
            GUIAgentTool(
                connector=self.gui_connector, 
                llm_client=llm_client,
                recording_manager=recording_manager
            )
        ]
        
        logger.info(f"Initialized GUI session with {len(self.tools)} tool(s)")
        
        # Return session info
        session_info = {
            "session_id": self.session_id,
            "backend_type": self.backend_type.value,
            "vm_ip": self.gui_connector.vm_ip,
            "server_port": self.gui_connector.server_port,
            "num_tools": len(self.tools),
            "tools": [tool.name for tool in self.tools],
            "llm_client": "anthropic" if llm_client else "none",
        }
        
        return session_info
    
    async def connect(self) -> None:
        """Connect to GUI desktop environment"""
        if self.connector.is_connected:
            return
        
        self.status = SessionStatus.CONNECTING
        logger.info(f"Connecting to desktop_env at {self.gui_connector.base_url}")
        
        await self.connector.connect()
        
        self.status = SessionStatus.CONNECTED
        logger.info("Connected to desktop environment")
    
    async def disconnect(self) -> None:
        """Disconnect from GUI desktop environment"""
        if not self.connector.is_connected:
            return
        
        logger.info("Disconnecting from desktop environment")
        await self.connector.disconnect()
        
        self.status = SessionStatus.DISCONNECTED
        logger.info("Disconnected from desktop environment")
    
    @property
    def is_connected(self) -> bool:
        """Check if session is connected"""
        return self.connector.is_connected