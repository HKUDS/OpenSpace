import aiohttp
from typing import Optional, Dict, Any
from openspace.utils.logging import Logger
from .config import get_client_base_url

logger = Logger.get_logger(__name__)


class SystemInfoClient:
    """
    This client provides simple methods to get:
    - Platform info (OS, architecture, version, etc.)
    - Screen size
    - Cursor position  
    """
    
    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: int = 10
    ):
        """
        Initialize system info client.
        
        Args:
            base_url: Base URL of the local server
                     (default: read from local_server/config.json or env LOCAL_SERVER_URL)
            timeout: Request timeout in seconds
        """
        # Get base_url: priority is explicit > env > config file
        if base_url is None:
            base_url = get_client_base_url()
        
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session: Optional[aiohttp.ClientSession] = None
        self._cached_info: Optional[Dict[str, Any]] = None
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        return self._session
    
    async def get_system_info(self, use_cache: bool = True) -> Optional[Dict[str, Any]]:
        """
        Get comprehensive system information.
        
        Returns information including:
        - system: OS name (Linux, Darwin, Windows)
        - release: OS release version
        - version: Detailed version string
        - machine: Architecture (x86_64, arm64, etc.)
        - processor: Processor type
        - Additional platform-specific info
        
        Args:
            use_cache: Whether to use cached info (default: True)
        """
        # Check cache
        if use_cache and self._cached_info:
            logger.debug("Using cached system info")
            return self._cached_info
        
        try:
            session = await self._get_session()
            url = f"{self.base_url}/platform"
            
            async with session.get(url) as response:
                if response.status == 200:
                    info = await response.json()
                    
                    # Cache the result
                    if use_cache:
                        self._cached_info = info
                    
                    logger.debug(f"System info retrieved: {info.get('system')}")
                    return info
                else:
                    error_text = await response.text()
                    logger.error(f"Failed to get system info: HTTP {response.status} - {error_text}")
                    return None
        
        except Exception as e:
            logger.error(f"Failed to get system info: {e}")
            return None
    
    async def get_screen_size(self) -> Optional[Dict[str, int]]:
        """
        Get screen size.
        
        Returns:
            Dict with 'width' and 'height', or None on failure
        """
        try:
            session = await self._get_session()
            url = f"{self.base_url}/screen_size"
            
            async with session.get(url) as response:
                if response.status == 200:
                    size = await response.json()
                    logger.debug(f"Screen size: {size.get('width')}x{size.get('height')}")
                    return {
                        "width": size.get("width"),
                        "height": size.get("height")
                    }
                else:
                    error_text = await response.text()
                    logger.error(f"Failed to get screen size: HTTP {response.status} - {error_text}")
                    return None
        
        except Exception as e:
            logger.error(f"Failed to get screen size: {e}")
            return None
    
    async def get_cursor_position(self) -> Optional[Dict[str, int]]:
        """
        Get current cursor position.
        
        Returns:
            Dict with 'x' and 'y', or None on failure
        """
        try:
            session = await self._get_session()
            url = f"{self.base_url}/cursor_position"
            
            async with session.get(url) as response:
                if response.status == 200:
                    pos = await response.json()
                    return {
                        "x": pos.get("x"),
                        "y": pos.get("y")
                    }
                else:
                    error_text = await response.text()
                    logger.error(f"Failed to get cursor position: HTTP {response.status} - {error_text}")
                    return None
        
        except Exception as e:
            logger.error(f"Failed to get cursor position: {e}")
            return None
    
    def clear_cache(self):
        """Clear cached system information."""
        self._cached_info = None
        logger.debug("System info cache cleared")
    
    async def close(self):
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            logger.debug("System info client session closed")
    
    async def __aenter__(self):
        """Context manager entry."""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        await self.close()
        return False

async def get_system_info(base_url: Optional[str] = None) -> Optional[Dict[str, Any]]:
    async with SystemInfoClient(base_url=base_url) as client:
        return await client.get_system_info(use_cache=False)


async def get_screen_size(base_url: Optional[str] = None) -> Optional[Dict[str, int]]:
    async with SystemInfoClient(base_url=base_url) as client:
        return await client.get_screen_size()