"""
Long-lived aiohttp ClientSession manager based on AsyncContextConnectionManager.

It keeps a single ClientSession open during the lifetime of a backend
session, saving the overhead of creating and closing a TCP connection
for every request.
"""
from typing import Optional
import aiohttp

from .async_ctx import AsyncContextConnectionManager


class AioHttpConnectionManager(
    AsyncContextConnectionManager[aiohttp.ClientSession, ...]
):
    """Manage a persistent aiohttp.ClientSession."""

    def __init__(
        self,
        base_url: str,
        headers: Optional[dict[str, str]] = None,
        timeout: float = 30,
    ):
        self.base_url = base_url.rstrip("/")
        timeout_cfg = aiohttp.ClientTimeout(total=timeout)
        super().__init__(
            aiohttp.ClientSession,
            timeout=timeout_cfg,
            headers=headers or {},
        )
        self._logger.debug(
            "Init AioHttpConnectionManager base_url=%s timeout=%s", self.base_url, timeout
        )

    async def _establish_connection(self) -> aiohttp.ClientSession:
        """Create and enter the aiohttp.ClientSession context."""
        session = await super()._establish_connection()
        self._logger.debug("aiohttp ClientSession created")
        return session

    async def _close_connection(self) -> None:
        """Close the session and then call the parent cleanup.
        
        Ensures proper cleanup even if close() fails.
        """
        if self._ctx:
            try:
                self._logger.debug("Closing aiohttp ClientSession")
                await self._ctx.close()
                # Give aiohttp time to finish its internal cleanup callbacks
                import asyncio
                await asyncio.sleep(0.1)
            except Exception as e:
                self._logger.warning(f"Error closing aiohttp ClientSession: {e}")
        await super()._close_connection()