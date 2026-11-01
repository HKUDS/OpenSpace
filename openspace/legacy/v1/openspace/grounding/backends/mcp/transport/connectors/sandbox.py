"""
Sandbox connector for MCP implementations.

This module provides a connector for communicating with MCP implementations
that are executed inside a sandbox environment (supports any BaseSandbox implementation).
"""

import asyncio
import sys
import time

import aiohttp
from mcp import ClientSession

from openspace.utils.logging import Logger
from openspace.grounding.backends.mcp.transport.task_managers import SseConnectionManager
from openspace.grounding.core.security import BaseSandbox
from openspace.grounding.backends.mcp.transport.connectors.base import MCPBaseConnector

logger = Logger.get_logger(__name__)


class SandboxConnector(MCPBaseConnector):
    """Connector for MCP implementations running in a sandbox environment.

    This connector runs a user-defined stdio command within a sandbox environment
    through a BaseSandbox implementation (e.g., E2BSandbox), potentially wrapped 
    by a utility like 'supergateway' to expose its stdio.
    """

    def __init__(
        self,
        sandbox: BaseSandbox,
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
        supergateway_command: str = "npx -y supergateway",
        port: int = 3000,
        timeout: float = 5,
        sse_read_timeout: float = 60 * 5,
    ):
        """Initialize a new sandbox connector.

        Args:
            sandbox: A BaseSandbox implementation (e.g., E2BSandbox) to run commands in.
            command: The user's MCP server command to execute in the sandbox.
            args: Command line arguments for the user's MCP server command.
            env: Environment variables for the user's MCP server command.
            supergateway_command: Command to run supergateway (default: "npx -y supergateway").
            port: Port number for the sandbox server (default: 3000).
            timeout: Timeout for the sandbox process in seconds.
            sse_read_timeout: Timeout for the SSE connection in seconds.
        """
        # Store user command configuration
        self.user_command = command
        self.user_args = args or []
        self.user_env = env or {}
        self.port = port
        
        # Create a placeholder connection manager (will be set up in connect())
        # We need the sandbox to start first to get the base_url, so we can't create
        # the real SseConnectionManager until connect() is called
        from openspace.grounding.core.transport.task_managers import PlaceholderConnectionManager
        connection_manager = PlaceholderConnectionManager()
        super().__init__(connection_manager)

        # Sandbox configuration
        self._sandbox = sandbox
        self.supergateway_cmd_parts = supergateway_command
        
        # Runtime state
        self.process = None
        self.client_session: ClientSession | None = None
        self.errlog = sys.stderr
        self.base_url: str | None = None
        self._connected = False
        self._connection_manager: SseConnectionManager | None = None

        # SSE connection parameters
        self.headers = {}
        self.timeout = timeout
        self.sse_read_timeout = sse_read_timeout

        self.stdout_lines: list[str] = []
        self.stderr_lines: list[str] = []
        self._server_ready = asyncio.Event()

    def _handle_stdout(self, data: str) -> None:
        """Handle stdout data from the sandbox process."""
        self.stdout_lines.append(data)
        logger.debug(f"[SANDBOX STDOUT] {data}", end="", flush=True)

    def _handle_stderr(self, data: str) -> None:
        """Handle stderr data from the sandbox process."""
        self.stderr_lines.append(data)
        logger.debug(f"[SANDBOX STDERR] {data}", file=self.errlog, end="", flush=True)

    async def wait_for_server_response(self, base_url: str, timeout: int = 30) -> bool:
        """Wait for the server to respond to HTTP requests.
        
        Args:
            base_url: The base URL to check for server readiness
            timeout: Maximum time to wait in seconds
            
        Returns:
            True if server is responding, raises TimeoutError otherwise
        """
        logger.info(f"Waiting for server at {base_url} to respond...")
        sys.stdout.flush()

        start_time = time.time()
        ping_url = f"{base_url}/sse"

        # Try to connect to the server
        while time.time() - start_time < timeout:
            try:
                async with aiohttp.ClientSession() as session:
                    try:
                        # First try the endpoint
                        async with session.get(ping_url, timeout=2) as response:
                            if response.status == 200:
                                elapsed = time.time() - start_time
                                logger.info(f"Server is ready! SSE endpoint responded with 200 after {elapsed:.1f}s")
                                return True
                    except Exception:
                        # If sse endpoint doesn't work, try the base URL
                        async with session.get(base_url, timeout=2) as response:
                            if response.status < 500:  # Accept any non-server error
                                elapsed = time.time() - start_time
                                logger.info(
                                    f"Server is ready! Base URL responded with {response.status} after {elapsed:.1f}s"
                                )
                                return True
            except Exception:
                # Wait a bit before trying again
                await asyncio.sleep(0.5)
                continue

            # If we get here, the request failed
            await asyncio.sleep(0.5)

            # Log status every 5 seconds
            elapsed = time.time() - start_time
            if int(elapsed) % 5 == 0:
                logger.info(f"Still waiting for server to respond... ({elapsed:.1f}s elapsed)")
                sys.stdout.flush()

        # If we get here, we timed out
        raise TimeoutError(f"Timeout waiting for server to respond (waited {timeout} seconds)")

    async def _before_connect(self) -> None:
        """Set up the sandbox and prepare the connection manager."""
        logger.debug("Connecting to MCP implementation in sandbox")

        # Start the sandbox if not already active
        if not self._sandbox.is_active:
            logger.debug("Starting sandbox...")
            await self._sandbox.start()

        # Get the host for the sandbox
        # Note: This assumes the sandbox implementation has a get_host method
        # For E2BSandbox, this is available
        host = self._sandbox.get_host(self.port)
        self.base_url = f"https://{host}".rstrip("/")

        # Append command with args
        command = f"{self.user_command} {' '.join(self.user_args)}"

        # Construct the full command with supergateway
        full_command = f'{self.supergateway_cmd_parts} \
            --base-url {self.base_url} \
            --port {self.port} \
            --cors \
            --stdio "{command}"'

        logger.debug(f"Full command: {full_command}")

        # Execute the command in the sandbox
        self.process = await self._sandbox.execute_safe(
            full_command,
            envs=self.user_env,
            timeout=1000 * 60 * 10,  # 10 minutes timeout
            background=True,
            on_stdout=self._handle_stdout,
            on_stderr=self._handle_stderr,
        )

        # Wait for the server to be ready
        await self.wait_for_server_response(self.base_url, timeout=30)
        logger.debug("Initializing connection manager...")

        # Create the SSE connection URL
        sse_url = f"{self.base_url}/sse"

        # Create and set up the connection manager
        self._connection_manager = SseConnectionManager(sse_url, self.headers, self.timeout, self.sse_read_timeout)

    async def _after_connect(self) -> None:
        """Create ClientSession and log success."""
        await super()._after_connect()
        logger.debug(f"Successfully connected to MCP implementation via HTTP/SSE in sandbox: {self.base_url}")

    async def _before_disconnect(self) -> None:
        """Clean up sandbox-specific resources before disconnection."""
        logger.debug("Cleaning up sandbox resources")

        # Stop the sandbox (which will clean up processes)
        if self._sandbox and self._sandbox.is_active:
            try:
                logger.debug("Stopping sandbox instance")
                await self._sandbox.stop()
                logger.debug("Sandbox instance stopped successfully")
            except Exception as e:
                logger.warning(f"Error stopping sandbox: {e}")

        self.process = None

        # Call the parent method to clean up MCP resources
        await super()._before_disconnect()

        # Clear any collected output
        self.stdout_lines = []
        self.stderr_lines = []
        self.base_url = None
    
    async def _cleanup_on_connect_failure(self) -> None:
        """Clean up sandbox resources on connection failure."""
        # Stop the sandbox if it was started
        if self._sandbox and self._sandbox.is_active:
            try:
                await self._sandbox.stop()
            except Exception as e:
                logger.warning(f"Error stopping sandbox during cleanup: {e}")
        
        self.process = None
        self.stdout_lines = []
        self.stderr_lines = []
        self.base_url = None
        
        # Call parent cleanup
        await super()._cleanup_on_connect_failure()

    @property
    def sandbox(self) -> BaseSandbox:
        """Get the underlying sandbox instance."""
        return self._sandbox

    @property
    def public_identifier(self) -> str:
        """Get the identifier for the connector."""
        return {"type": "sandbox", "command": self.user_command, "args": self.user_args}
