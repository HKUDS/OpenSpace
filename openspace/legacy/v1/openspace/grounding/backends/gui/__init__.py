from .provider import GUIProvider
from .session import GUISession
from .transport.connector import GUIConnector
from .transport.local_connector import LocalGUIConnector

try:
    from .anthropic_client import AnthropicGUIClient
    from . import anthropic_utils
    _anthropic_available = True
except ImportError:
    _anthropic_available = False

__all__ = [
    # Core Provider and Session
    "GUIProvider",
    "GUISession",
    
    # Transport layer
    "GUIConnector",
    "LocalGUIConnector",
]

# Add Anthropic modules to exports if available
if _anthropic_available:
    __all__.extend(["AnthropicGUIClient", "anthropic_utils"])