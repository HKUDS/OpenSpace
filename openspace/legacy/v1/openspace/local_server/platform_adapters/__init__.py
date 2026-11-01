import platform
from typing import Optional, Any

platform_name = platform.system()

if platform_name == "Darwin":
    try:
        from .macos_adapter import MacOSAdapter as PlatformAdapter
        ADAPTER_AVAILABLE = True
    except ImportError:
        PlatformAdapter = None
        ADAPTER_AVAILABLE = False
elif platform_name == "Linux":
    try:
        from .linux_adapter import LinuxAdapter as PlatformAdapter
        ADAPTER_AVAILABLE = True
    except ImportError:
        PlatformAdapter = None
        ADAPTER_AVAILABLE = False
elif platform_name == "Windows":
    try:
        from .windows_adapter import WindowsAdapter as PlatformAdapter
        ADAPTER_AVAILABLE = True
    except ImportError:
        PlatformAdapter = None
        ADAPTER_AVAILABLE = False
else:
    PlatformAdapter = None
    ADAPTER_AVAILABLE = False

def get_platform_adapter() -> Optional[Any]:
    if ADAPTER_AVAILABLE and PlatformAdapter:
        return PlatformAdapter()
    return None

__all__ = ["PlatformAdapter", "get_platform_adapter", "ADAPTER_AVAILABLE"]

