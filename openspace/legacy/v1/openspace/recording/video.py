"""
Video Recorder

Communicates with local_server through platforms.RecordingClient
Supports local and remote recording (through configuration LOCAL_SERVER_URL)
"""

from pathlib import Path
from typing import Optional

from openspace.utils.logging import Logger
from openspace.platforms import RecordingClient

logger = Logger.get_logger(__name__)


class VideoRecorder:
    def __init__(
        self,
        output_path: str,
        base_url: Optional[str] = None,
    ):
        """
        Initialize video recorder
        
        Args:
            output_path: output video path
            base_url: local_server address (None = read from config/environment variables)
        """
        self.output_path = Path(output_path)
        self.base_url = base_url
        self.is_recording = False
        self._client: Optional[RecordingClient] = None
    
    async def start(self):
        """Start recording screen"""
        if self.is_recording:
            return False
        
        try:
            if self._client is None:
                self._client = RecordingClient(base_url=self.base_url)
            
            success = await self._client.start_recording()
            
            if success:
                self.is_recording = True
                logger.info(f"Video recording started")
                return True
            else:
                logger.warning("Video recording failed to start")
                return False
        
        except Exception as e:
            logger.warning(f"Video recording failed to start: {e}")
            return False
    
    async def stop(self):
        """Stop recording screen and save to local"""
        if not self.is_recording:
            return False
        
        try:
            if self._client:
                video_bytes = await self._client.end_recording(dest=str(self.output_path))
                
                if video_bytes:
                    video_size_mb = len(video_bytes) / (1024 * 1024)
                    self.is_recording = False
                    logger.info(f"Video recording stopped ({video_size_mb:.2f} MB)")
                    return True
                else:
                    logger.warning("Video recording failed to stop")
                    return False
        
        except Exception as e:
            logger.warning(f"Video recording failed to stop: {e}")
            return False
        finally:
            if self._client:
                try:
                    await self._client.close()
                except Exception:
                    pass
                self._client = None


__all__ = ['VideoRecorder']