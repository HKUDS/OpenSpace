"""
Streamable HTTP connection management for MCP implementations.

This module provides a connection manager for streamable HTTP-based MCP connections
that ensures proper task isolation and resource cleanup.
"""

from datetime import timedelta
from typing import Any, Tuple
from contextlib import asynccontextmanager

from mcp.client.streamable_http import streamablehttp_client
from openspace.utils.logging import Logger
from openspace.grounding.core.transport.task_managers import (
    AsyncContextConnectionManager,
)

logger = Logger.get_logger(__name__)


def _make_shim():
    """
    Create a shim that wraps streamablehttp_client with improved error handling.
    """
    @asynccontextmanager
    async def _shim(**kw):
        client_streams = None
        ctx_manager = None
        
        try:
            # Enter the context - this may raise ExceptionGroup during concurrent init
            ctx_manager = streamablehttp_client(**kw)
            try:
                r, w, _sid_cb = await ctx_manager.__aenter__()
                client_streams = (r, w)
            except Exception as conn_error:
                # Handle connection errors during __aenter__
                error_msg = str(conn_error).lower()
                if "unhandled errors in a taskgroup" in error_msg:
                    logger.debug(f"TaskGroup race condition during connection: {type(conn_error).__name__}")
                    # Clean up and re-raise to trigger retry
                    if ctx_manager:
                        try:
                            await ctx_manager.__aexit__(None, None, None)
                        except Exception:
                            pass  # Ignore cleanup errors
                    raise
                else:
                    # Other connection errors - log and re-raise
                    logger.warning(f"Connection error: {conn_error}")
                    raise
            
            # Yield to caller
            yield client_streams
            
        except GeneratorExit:
            # Normal generator exit - this happens during cleanup
            logger.debug("StreamableHTTP generator exit (normal cleanup)")
            
        finally:
            # Always try to exit the context manager
            if ctx_manager is not None:
                try:
                    await ctx_manager.__aexit__(None, None, None)
                except (GeneratorExit, RuntimeError, OSError, Exception) as e:
                    # Cleanup errors are expected during concurrent shutdown
                    # Log at debug level and suppress
                    error_type = type(e).__name__
                    if "ExceptionGroup" in error_type or "TaskGroup" in str(e):
                        logger.debug(f"Benign TaskGroup cleanup error: {error_type}")
                    else:
                        logger.debug(f"Benign cleanup error: {error_type}")
                    
    return _shim


class StreamableHttpConnectionManager(
    AsyncContextConnectionManager[Tuple[Any, Any], ...]
):
    """
    MCP Streamable-HTTP connection manager based on the generic
    AsyncContextConnectionManager.  Extra session-id callback returned by the
    SDK is discarded by the shim above.
    """

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 5,
        read_timeout: float = 60 * 5,
    ):
        shim = _make_shim()              
        super().__init__(
            shim,
            url=url,
            headers=headers or {},
            timeout=timedelta(seconds=timeout),
            sse_read_timeout=timedelta(seconds=read_timeout),
        )
        self.url = url
        self.headers = headers or {}
        logger.debug("StreamableHttpConnectionManager init url=%s", url)