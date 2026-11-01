"""
Screenshot client for capturing screens via HTTP API.

This module provides a screenshot client that captures screenshots by calling
the local_server's /screenshot endpoint.

Always uses HTTP API (like RecordingClient):
- Local: http://127.0.0.1:5000/screenshot
- Remote: http://remote-vm:5000/screenshot
"""
import aiohttp
from typing import Optional
from openspace.utils.logging import Logger
from .config import get_client_base_url

logger = Logger.get_logger(__name__)


class ScreenshotClient:
    
    def __init__(
        self, 
        base_url: Optional[str] = None,
        timeout: int = 10
    ):
        """
        Initialize screenshot client.
        
        Args:
            base_url: Base URL of local_server 
                     (default: read from config/env, typically http://127.0.0.1:5000)
            timeout: Request timeout (seconds)
        """
        # Get base_url from config if not provided
        if base_url is None:
            base_url = get_client_base_url()
        
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = None
        
        logger.debug(f"ScreenshotClient initialized: {self.base_url}")
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        return self._session
    
    @staticmethod
    def _is_valid_image_response(content_type: str, data: Optional[bytes]) -> bool:
        """
        Validate image response using magic bytes.
        
        Args:
            content_type: HTTP Content-Type header
            data: Response data bytes
        
        Returns:
            True if data is valid PNG/JPEG image
        """
        if not isinstance(data, (bytes, bytearray)) or not data:
            return False
        
        # PNG magic bytes: \x89PNG\r\n\x1a\n
        if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
            return True
        
        # JPEG magic bytes: \xff\xd8\xff
        if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
            return True
        
        # Fallback to content-type check
        if content_type and ("image/png" in content_type or "image/jpeg" in content_type):
            return True
        
        return False
    
    async def capture(self) -> Optional[bytes]:
        """
        Capture screenshot via HTTP API.
        
        Calls: GET {base_url}/screenshot
        
        Returns:
            PNG image bytes, or None on failure
        """
        try:
            session = await self._get_session()
            url = f"{self.base_url}/screenshot"
            
            logger.debug(f"Requesting screenshot: {url}")
            
            async with session.get(url) as response:
                if response.status == 200:
                    content_type = response.headers.get("Content-Type", "")
                    screenshot_bytes = await response.read()
                    
                    # Validate image format
                    if self._is_valid_image_response(content_type, screenshot_bytes):
                        logger.debug(f"Screenshot captured: {len(screenshot_bytes)} bytes")
                        return screenshot_bytes
                    else:
                        logger.error("Invalid screenshot format received")
                        return None
                else:
                    error_text = await response.text()
                    logger.error(f"Failed to capture screenshot: HTTP {response.status} - {error_text}")
                    return None
        
        except Exception as e:
            logger.error(f"Failed to capture screenshot: {e}")
            return None
    
    async def capture_to_file(self, output_path: str) -> bool:
        try:
            screenshot = await self.capture()
            if screenshot:
                import os
                os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
                with open(output_path, 'wb') as f:
                    f.write(screenshot)
                logger.info(f"Screenshot saved to: {output_path}")
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to save screenshot to file: {e}")
            return False
    
    async def get_screen_size(self) -> tuple[int, int]:
        """
        Get screen size via HTTP API.
        
        Calls: GET {base_url}/screen_size
        
        Returns:
            (width, height)
        """
        try:
            session = await self._get_session()
            url = f"{self.base_url}/screen_size"
            
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    width = data.get('width', 1920)
                    height = data.get('height', 1080)
                    logger.debug(f"Screen size: {width}x{height}")
                    return (width, height)
                else:
                    logger.warning("Failed to get screen size, using default")
                    return (1920, 1080)
        
        except Exception as e:
            logger.error(f"Failed to get screen size: {e}")
            return (1920, 1080)
    
    async def close(self):
        """Close HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            logger.debug("Screenshot client session closed")
    
    async def __aenter__(self):
        """Context manager entry."""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        await self.close()
        return False


class AutoScreenshotWrapper:
    """
    Wrapper that automatically captures screenshots after backend calls.
    
    This wrapper can be used to wrap any backend tool/session and automatically
    capture screenshots after each operation.
    
    Usage:
        # Wrap a backend tool
        wrapped_tool = AutoScreenshotWrapper(
            tool=gui_tool,
            screenshot_client=screenshot_client,
            on_screenshot=lambda screenshot: recorder.record_step(...)
        )
        
        # Use wrapped tool normally
        result = await wrapped_tool.execute(...)
        # Screenshot is automatically captured and handled
    """
    
    def __init__(
        self,
        tool,
        screenshot_client: Optional[ScreenshotClient] = None,
        on_screenshot=None,
        enabled: bool = True
    ):
        """
        Initialize auto-screenshot wrapper.
        
        Args:
            tool: The tool/session to wrap
            screenshot_client: Screenshot client to use (created if None)
            on_screenshot: Callback function(screenshot_bytes) called after each screenshot
            enabled: Whether auto-screenshot is enabled
        """
        self._tool = tool
        self._screenshot_client = screenshot_client or ScreenshotClient()
        self._on_screenshot = on_screenshot
        self._enabled = enabled
    
    def __getattr__(self, name):
        """Delegate attribute access to wrapped tool."""
        return getattr(self._tool, name)
    
    async def _capture_and_notify(self):
        """Capture screenshot and notify callback."""
        if not self._enabled:
            return
        
        try:
            screenshot = await self._screenshot_client.capture()
            if screenshot and self._on_screenshot:
                await self._on_screenshot(screenshot)
        except Exception as e:
            logger.warning(f"Failed to auto-capture screenshot: {e}")
    
    async def execute(self, *args, **kwargs):
        """
        Execute tool and auto-capture screenshot.
        """
        # Execute original method
        result = await self._tool.execute(*args, **kwargs)
        
        # Capture screenshot after execution
        await self._capture_and_notify()
        
        return result
    
    async def _arun(self, *args, **kwargs):
        """
        Run tool and auto-capture screenshot.
        """
        # Execute original method
        result = await self._tool._arun(*args, **kwargs)
        
        # Capture screenshot after execution
        await self._capture_and_notify()
        
        return result
    
    def enable(self):
        """Enable auto-screenshot."""
        self._enabled = True
    
    def disable(self):
        """Disable auto-screenshot."""
        self._enabled = False