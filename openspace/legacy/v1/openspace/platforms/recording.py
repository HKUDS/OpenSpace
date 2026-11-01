import aiohttp
from typing import Optional
from openspace.utils.logging import Logger
from .config import get_client_base_url

logger = Logger.get_logger(__name__)


class RecordingClient:
    """
    Client for screen recording via HTTP API.
    
    This client directly calls the local server's recording endpoints:
    - POST /start_recording
    - POST /end_recording
    """
    
    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: int = 30
    ):
        """
        Initialize recording client.
        
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
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        return self._session
    
    async def start_recording(self, auto_cleanup: bool = True) -> bool:
        """
        Start screen recording.
        
        Args:
            auto_cleanup: If True, automatically end previous recording if one is in progress
        """
        try:
            session = await self._get_session()
            url = f"{self.base_url}/start_recording"
            
            async with session.post(url) as response:
                if response.status == 200:
                    logger.info("Screen recording started")
                    return True
                elif response.status == 400 and auto_cleanup:
                    # Check if error is due to recording already in progress
                    error_text = await response.text()
                    if "already in progress" in error_text.lower():
                        logger.warning("Recording already in progress, stopping previous recording...")
                        
                        # Try to end the previous recording
                        video_bytes = await self.end_recording()
                        if video_bytes:
                            logger.info("Previous recording ended successfully, retrying start...")
                        else:
                            logger.warning("Failed to end previous recording, but will retry start anyway...")
                        
                        # Retry starting recording (without auto_cleanup to avoid infinite loop)
                        return await self.start_recording(auto_cleanup=False)
                    else:
                        logger.error(f"Failed to start recording: HTTP {response.status} - {error_text}")
                        return False
                else:
                    error_text = await response.text()
                    logger.error(f"Failed to start recording: HTTP {response.status} - {error_text}")
                    return False
        
        except Exception as e:
            logger.error(f"Failed to start recording: {e}")
            return False
    
    async def end_recording(self, dest: Optional[str] = None) -> Optional[bytes]:
        """
        End screen recording and optionally save to file.
        """
        try:
            session = await self._get_session()
            url = f"{self.base_url}/end_recording"
            
            # Use longer timeout for end_recording (file may be large)
            async with session.post(url, timeout=aiohttp.ClientTimeout(total=60)) as response:
                if response.status == 200:
                    video_bytes = await response.read()
                    
                    # Save to file if destination provided
                    if dest:
                        try:
                            with open(dest, "wb") as f:
                                f.write(video_bytes)
                            logger.info(f"Recording saved to: {dest}")
                        except Exception as e:
                            logger.error(f"Failed to save recording file: {e}")
                            return None
                    
                    logger.info("Screen recording ended")
                    return video_bytes
                else:
                    error_text = await response.text()
                    logger.error(f"Failed to end recording: HTTP {response.status} - {error_text}")
                    return None
        
        except Exception as e:
            logger.error(f"Failed to end recording: {e}")
            return None
    
    async def close(self):
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            # Give aiohttp time to finish cleanup callbacks
            import asyncio
            await asyncio.sleep(0.25)
            logger.debug("Recording client session closed")
    
    async def __aenter__(self):
        """Context manager entry."""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        await self.close()
        return False


class RecordingContextManager:

    def __init__(
        self,
        base_url: Optional[str] = None,
        output_path: Optional[str] = None,
        timeout: Optional[int] = None
    ):
        """
        Initialize recording context manager.
        
        Args:
            base_url: Base URL of the local server (default: from config)
            output_path: Path to save recording (default: from config)
            timeout: Request timeout in seconds (default: from config)
        """
        # Load output_path from config if not provided
        if output_path is None:
            try:
                from openspace.config import get_config
                config = get_config()
                if config.recording.screen_recording_path:
                    output_path = config.recording.screen_recording_path
            except Exception:
                pass
        
        self.client = RecordingClient(base_url=base_url, timeout=timeout)
        self.output_path = output_path
        self.recording_started = False
    
    async def __aenter__(self) -> RecordingClient:
        """Start recording on context entry."""
        success = await self.client.start_recording()
        if success:
            self.recording_started = True
            logger.info("Recording context started")
        else:
            logger.warning("Failed to start recording in context")
        
        return self.client
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Stop recording on context exit."""
        if self.recording_started:
            try:
                await self.client.end_recording(dest=self.output_path)
                logger.info("Recording context ended")
            except Exception as e:
                logger.error(f"Failed to end recording in context: {e}")
        
        await self.client.close()
        return False