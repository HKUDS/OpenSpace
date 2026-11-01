"""
HTTP connector for MCP implementations.

This module provides a connector for communicating with MCP implementations
through MCP Streamable HTTP or SSE transports.
"""

import asyncio
from mcp import ClientSession

from openspace.utils.logging import Logger
from openspace.grounding.backends.mcp.transport.task_managers import SseConnectionManager, StreamableHttpConnectionManager
from openspace.grounding.backends.mcp.transport.connectors.base import MCPBaseConnector, DEFAULT_TOOL_CALL_MAX_RETRIES, DEFAULT_TOOL_CALL_RETRY_DELAY

logger = Logger.get_logger(__name__)


def _build_sse_candidate_urls(base_url: str) -> list[str]:
    """Try the common FastMCP `/sse` endpoint before the raw base URL."""
    normalized = base_url.rstrip("/")
    if normalized.endswith("/sse"):
        return [normalized]
    return [f"{normalized}/sse", normalized]


class HttpConnector(MCPBaseConnector):
    """Connector for MCP implementations using HTTP transport.

    This connector uses HTTP/SSE or streamable HTTP to communicate with remote MCP implementations,
    using a connection manager to handle the proper lifecycle management.
    """

    def __init__(
        self,
        base_url: str,
        auth_token: str | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 5,
        sse_read_timeout: float = 60 * 5,
        tool_call_max_retries: int = DEFAULT_TOOL_CALL_MAX_RETRIES,
        tool_call_retry_delay: float = DEFAULT_TOOL_CALL_RETRY_DELAY,
    ):
        """Initialize a new HTTP connector.

        Args:
            base_url: The base URL of the MCP HTTP API.
            auth_token: Optional authentication token.
            headers: Optional additional headers.
            timeout: Timeout for HTTP operations in seconds.
            sse_read_timeout: Timeout for SSE read operations in seconds.
            tool_call_max_retries: Maximum number of retries for tool calls (default: 3)
            tool_call_retry_delay: Initial delay between retries in seconds (default: 1.0)
        """
        self.base_url = base_url.rstrip("/")
        self.auth_token = auth_token
        self.headers = headers or {}
        if auth_token:
            self.headers["Authorization"] = f"Bearer {auth_token}"
        self.timeout = timeout
        self.sse_read_timeout = sse_read_timeout

        super().__init__(
            None,
            tool_call_max_retries=tool_call_max_retries,
            tool_call_retry_delay=tool_call_retry_delay,
        )

    async def connect(self) -> None:
        """Create the underlying MCP session/connection."""
        if self._connected:
            return
        
        try:
            # Hook: before connection - this sets up transport type
            await self._before_connect()

            # Use normal connection flow with connection manager. If
            # _before_connect() already established a connection, reuse it.
            if self._connection is None:
                self._connection = await self._connection_manager.start()
            await self._after_connect()
            self._connected = True
        except Exception:
            await self._cleanup_on_connect_failure()
            raise

    async def disconnect(self) -> None:
        """Close the session/connection and reset state."""
        if not self._connected:
            return
        
        # Hook: before disconnection
        await self._before_disconnect()
        
        if self._connection_manager:
            await self._connection_manager.stop()
            self._connection = None
        
        # Hook: after disconnection
        await self._after_disconnect()
        
        self._connected = False

    async def _before_connect(self) -> None:
        """Negotiate transport type and set up the appropriate connection manager.
        
        Tries transports in order:
        1. Streamable HTTP (new MCP transport)
        2. SSE (legacy MCP transport)
        
        This implements MCP transport negotiation without falling back to
        non-standard HTTP RPC protocols.
        """
        self.transport_type = None
        connection_manager = None
        streamable_error = None
        sse_error = None

        # First, try the new streamable HTTP transport
        try:
            logger.debug(f"Attempting streamable HTTP connection to: {self.base_url}")
            connection_manager = StreamableHttpConnectionManager(
                self.base_url, self.headers, self.timeout, self.sse_read_timeout
            )

            # Test the connection by starting it with built-in timeout
            read_stream, write_stream = await connection_manager.start(timeout=self.timeout)

            # Create and verify ClientSession
            test_client = ClientSession(read_stream, write_stream, sampling_callback=None)
            
            # Add timeout to __aenter__ - use asyncio.wait_for instead of anyio.fail_after
            # to avoid cancel scope conflicts with background tasks
            try:
                await asyncio.wait_for(test_client.__aenter__(), timeout=self.timeout)
            except asyncio.TimeoutError:
                raise TimeoutError(f"ClientSession enter timed out after {self.timeout}s")

            try:
                # Add timeout to initialize() using asyncio.wait_for to prevent hanging
                try:
                    await asyncio.wait_for(test_client.initialize(), timeout=self.timeout)
                except asyncio.TimeoutError:
                    raise TimeoutError(f"initialize() timed out after {self.timeout}s")
                    
                try:
                    await asyncio.wait_for(test_client.list_tools(), timeout=self.timeout)
                except asyncio.TimeoutError:
                    raise TimeoutError(f"list_tools() timed out after {self.timeout}s")
                
                # SUCCESS! Keep the client session (don't close it, closing destroys the streams)
                # Store it directly as the client_session for later use
                self.transport_type = "streamable HTTP"
                self._connection_manager = connection_manager
                self._connection = connection_manager.get_streams()
                self.client_session = test_client  # Reuse the working session
                logger.debug("Streamable HTTP transport selected")
                return
            except TimeoutError:
                try:
                    await asyncio.wait_for(test_client.__aexit__(None, None, None), timeout=2)
                except (asyncio.TimeoutError, Exception):
                    pass
                raise
            except Exception as init_error:
                # Clean up the test client only on error
                try:
                    await asyncio.wait_for(test_client.__aexit__(None, None, None), timeout=2)
                except (asyncio.TimeoutError, Exception):
                    pass
                raise init_error

        except Exception as e:
            streamable_error = e
            logger.debug(f"Streamable HTTP failed: {e}")

            # Clean up the failed connection manager
            if connection_manager:
                try:
                    await asyncio.wait_for(connection_manager.stop(), timeout=2)
                except (asyncio.TimeoutError, Exception):
                    pass

        # Try SSE fallback. FastMCP commonly exposes legacy SSE on `/sse`,
        # but some callers may already pass the full endpoint.
        for sse_url in _build_sse_candidate_urls(self.base_url):
            connection_manager = None
            try:
                logger.debug(f"Attempting SSE fallback connection to: {sse_url}")
                connection_manager = SseConnectionManager(
                    sse_url, self.headers, self.timeout, self.sse_read_timeout
                )

                # Test the connection by starting it with built-in timeout
                read_stream, write_stream = await connection_manager.start(timeout=self.timeout)

                # Create and verify ClientSession
                test_client = ClientSession(read_stream, write_stream, sampling_callback=None)

                # Add timeout to __aenter__ - use asyncio.wait_for instead of anyio.fail_after
                # to avoid cancel scope conflicts with background tasks
                try:
                    await asyncio.wait_for(test_client.__aenter__(), timeout=self.timeout)
                except asyncio.TimeoutError:
                    raise TimeoutError(f"ClientSession enter timed out after {self.timeout}s")

                try:
                    try:
                        await asyncio.wait_for(test_client.initialize(), timeout=self.timeout)
                    except asyncio.TimeoutError:
                        raise TimeoutError(f"initialize() timed out after {self.timeout}s")

                    try:
                        await asyncio.wait_for(test_client.list_tools(), timeout=self.timeout)
                    except asyncio.TimeoutError:
                        raise TimeoutError(f"list_tools() timed out after {self.timeout}s")

                    # SUCCESS! Keep the client session (don't close it, closing destroys the streams)
                    # Store it directly as the client_session for later use
                    self.transport_type = "SSE"
                    self._connection_manager = connection_manager
                    self._connection = connection_manager.get_streams()
                    self.client_session = test_client  # Reuse the working session
                    logger.debug("SSE transport selected")
                    return
                except TimeoutError:
                    try:
                        await asyncio.wait_for(test_client.__aexit__(None, None, None), timeout=2)
                    except (asyncio.TimeoutError, Exception):
                        pass
                    raise
                except Exception as init_error:
                    # Clean up the test client only on error
                    try:
                        await asyncio.wait_for(test_client.__aexit__(None, None, None), timeout=2)
                    except (asyncio.TimeoutError, Exception):
                        pass
                    raise init_error

            except Exception as e:
                sse_error = e
                logger.debug(f"SSE failed for {sse_url}: {e}")

                # Clean up the failed connection manager
                if connection_manager:
                    try:
                        await asyncio.wait_for(connection_manager.stop(), timeout=2)
                    except (asyncio.TimeoutError, Exception):
                        pass

        logger.error(
            f"All MCP transport methods failed for {self.base_url}. "
            f"Streamable HTTP: {streamable_error}, SSE: {sse_error}"
        )
        raise streamable_error or sse_error or RuntimeError(
            f"No supported MCP HTTP transport available for {self.base_url}"
        )

    async def _after_connect(self) -> None:
        """Create ClientSession and log success."""
        # Skip creating ClientSession if _before_connect() already created one.
        if self.client_session is None:
            await super()._after_connect()
        else:
            logger.debug("Reusing ClientSession from _before_connect()")
        
        logger.debug(f"Successfully connected to MCP implementation via {self.transport_type}: {self.base_url}")

    async def _before_disconnect(self) -> None:
        """Clean up resources before disconnection."""
        await super()._before_disconnect()

    @property
    def public_identifier(self) -> dict[str, str | None]:
        """Get the identifier for the connector."""
        return {"type": self.transport_type, "base_url": self.base_url}
