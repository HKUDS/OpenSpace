"""
Client for managing MCP servers and sessions.

This module provides a high-level client that manages MCP servers, connectors,
and sessions from configuration.
"""
import asyncio
import warnings
from typing import Any, Optional

from openspace.grounding.core.types import SandboxOptions
from openspace.config.utils import get_config_value, save_json_file, load_json_file
from .config import create_connector_from_config
from .session import MCPSession
from .installer import MCPInstallerManager, MCPDependencyError

from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


class MCPClient:
    """Client for managing MCP servers and sessions.

    This class provides a unified interface for working with MCP servers,
    handling configuration, connector creation, and session management.
    """

    def __init__(
        self,
        config: str | dict[str, Any] | None = None,
        sandbox: bool = False,
        sandbox_options: SandboxOptions | None = None,
        timeout: float = 30.0,
        sse_read_timeout: float = 300.0,
        max_retries: int = 3,
        retry_interval: float = 2.0,
        installer: Optional[MCPInstallerManager] = None,
        check_dependencies: bool = True,
        tool_call_max_retries: int = 3,
        tool_call_retry_delay: float = 1.0,
    ) -> None:
        """Initialize a new MCP client.

        Args:
            config: Either a dict containing configuration or a path to a JSON config file.
                   If None, an empty configuration is used.
            sandbox: Whether to use sandboxed execution mode for running MCP servers.
            sandbox_options: Optional sandbox configuration options.
            timeout: Timeout for operations in seconds (default: 30.0)
            sse_read_timeout: SSE read timeout in seconds (default: 300.0)
            max_retries: Maximum number of retry attempts for failed operations (default: 3)
            retry_interval: Wait time between retries in seconds (default: 2.0)
            installer: Optional installer manager for dependency installation
            check_dependencies: Whether to check and install dependencies (default: True)
            tool_call_max_retries: Maximum number of retries for tool calls (default: 3)
            tool_call_retry_delay: Initial delay between tool call retries in seconds (default: 1.0)
        """
        self.config: dict[str, Any] = {}
        self.sandbox = sandbox
        self.sandbox_options = sandbox_options
        self.timeout = timeout
        self.sse_read_timeout = sse_read_timeout
        self.max_retries = max_retries
        self.retry_interval = retry_interval
        self.installer = installer
        self.check_dependencies = check_dependencies
        self.tool_call_max_retries = tool_call_max_retries
        self.tool_call_retry_delay = tool_call_retry_delay
        self.sessions: dict[str, MCPSession] = {}
        self.active_sessions: list[str] = []

        # Load configuration if provided
        if config is not None:
            if isinstance(config, str):
                self.config = load_json_file(config)
            else:
                self.config = config
    
    def _get_mcp_servers(self) -> dict[str, Any]:
        """Internal helper to get mcpServers configuration.
        
        Tries both 'mcpServers' and 'servers' keys for compatibility.
        
        Returns:
            Dictionary of MCP server configurations, empty dict if none found.
        """
        servers = get_config_value(self.config, "mcpServers", None)
        if servers is None:
            servers = get_config_value(self.config, "servers", {})
        return servers or {}

    @classmethod
    def from_dict(
        cls,
        config: dict[str, Any],
        sandbox: bool = False,
        sandbox_options: SandboxOptions | None = None,
        timeout: float = 30.0,
        sse_read_timeout: float = 300.0,
        max_retries: int = 3,
        retry_interval: float = 2.0,
    ) -> "MCPClient":
        """Create a MCPClient from a dictionary.

        Args:
            config: The configuration dictionary.
            sandbox: Whether to use sandboxed execution mode for running MCP servers.
            sandbox_options: Optional sandbox configuration options.
            timeout: Timeout for operations in seconds (default: 30.0)
            sse_read_timeout: SSE read timeout in seconds (default: 300.0)
            max_retries: Maximum number of retry attempts (default: 3)
            retry_interval: Wait time between retries in seconds (default: 2.0)
        """
        return cls(config=config, sandbox=sandbox, sandbox_options=sandbox_options, 
                   timeout=timeout, sse_read_timeout=sse_read_timeout,
                   max_retries=max_retries, retry_interval=retry_interval)

    @classmethod
    def from_config_file(
        cls, filepath: str, sandbox: bool = False, sandbox_options: SandboxOptions | None = None,
        timeout: float = 30.0, sse_read_timeout: float = 300.0,
        max_retries: int = 3, retry_interval: float = 2.0,
    ) -> "MCPClient":
        """Create a MCPClient from a configuration file.

        Args:
            filepath: The path to the configuration file.
            sandbox: Whether to use sandboxed execution mode for running MCP servers.
            sandbox_options: Optional sandbox configuration options.
            timeout: Timeout for operations in seconds (default: 30.0)
            sse_read_timeout: SSE read timeout in seconds (default: 300.0)
            max_retries: Maximum number of retry attempts (default: 3)
            retry_interval: Wait time between retries in seconds (default: 2.0)
        """
        return cls(config=load_json_file(filepath), sandbox=sandbox, sandbox_options=sandbox_options,
                   timeout=timeout, sse_read_timeout=sse_read_timeout,
                   max_retries=max_retries, retry_interval=retry_interval)

    def add_server(
        self,
        name: str,
        server_config: dict[str, Any],
    ) -> None:
        """Add a server configuration.

        Args:
            name: The name to identify this server.
            server_config: The server configuration.
        """
        mcp_servers = self._get_mcp_servers()
        if "mcpServers" not in self.config:
            self.config["mcpServers"] = {}
        
        self.config["mcpServers"][name] = server_config
        logger.debug(f"Added MCP server configuration: {name}")

    def remove_server(self, name: str) -> None:
        """Remove a server configuration.

        Args:
            name: The name of the server to remove.
        """
        mcp_servers = self._get_mcp_servers()
        if name in mcp_servers:
            # Remove from config
            if "mcpServers" in self.config:
                self.config["mcpServers"].pop(name, None)
            elif "servers" in self.config:
                self.config["servers"].pop(name, None)

            # If we removed an active session, remove it from active_sessions
            if name in self.active_sessions:
                self.active_sessions.remove(name)
            
            logger.debug(f"Removed MCP server configuration: {name}")
        else:
            logger.warning(f"Server '{name}' not found in configuration")

    def get_server_names(self) -> list[str]:
        """Get the list of configured server names.

        Returns:
            List of server names.
        """
        return list(self._get_mcp_servers().keys())

    def save_config(self, filepath: str) -> None:
        """Save the current configuration to a file.

        Args:
            filepath: The path to save the configuration to.
        """
        save_json_file(self.config, filepath)

    async def create_session(self, server_name: str, auto_initialize: bool = True) -> MCPSession:
        """Create a session for the specified server with retry logic.

        Args:
            server_name: The name of the server to create a session for.
            auto_initialize: Whether to automatically initialize the session.

        Returns:
            The created MCPSession.

        Raises:
            ValueError: If the specified server doesn't exist.
            Exception: If session creation fails after all retries.
        """
        # Check if session already exists
        if server_name in self.sessions:
            logger.debug(f"Session for server '{server_name}' already exists, returning existing session")
            return self.sessions[server_name]
        
        # Get server config
        servers = self._get_mcp_servers()
        
        if not servers:
            warnings.warn("No MCP servers defined in config", UserWarning, stacklevel=2)
            return None

        if server_name not in servers:
            raise ValueError(f"Server '{server_name}' not found in config. Available: {list(servers.keys())}")

        server_config = servers[server_name]

        # Retry logic for session creation
        last_exc: Exception | None = None
        
        for attempt in range(1, self.max_retries + 1):
            try:
                # Create connector with options (now async)
                connector = await create_connector_from_config(
                    server_config,
                    server_name=server_name,
                    sandbox=self.sandbox, 
                    sandbox_options=self.sandbox_options,
                    timeout=self.timeout,
                    sse_read_timeout=self.sse_read_timeout,
                    installer=self.installer,
                    check_dependencies=self.check_dependencies,
                    tool_call_max_retries=self.tool_call_max_retries,
                    tool_call_retry_delay=self.tool_call_retry_delay,
                )

                # Create the session with proper initialization parameters
                session = MCPSession(
                    connector=connector,
                    session_id=f"mcp-{server_name}",
                    auto_connect=True,
                    auto_initialize=False,  # We'll handle initialization explicitly below
                )
                
                # Initialize if requested
                if auto_initialize:
                    await session.initialize()
                    logger.debug(f"Initialized session for server '{server_name}'")
                
                # Store session
                self.sessions[server_name] = session

                # Add to active sessions
                if server_name not in self.active_sessions:
                    self.active_sessions.append(server_name)
                
                logger.info(f"Created session for MCP server '{server_name}' (attempt {attempt}/{self.max_retries})")
                return session
                
            except MCPDependencyError as e:
                # Don't retry dependency errors - they won't succeed on retry
                # Error already shown to user by installer, just re-raise
                logger.debug(f"Dependency error for server '{server_name}': {type(e).__name__}")
                raise
            except Exception as e:
                last_exc = e
                if attempt == self.max_retries:
                    break
                
                # Use info level for first attempt (common after fresh install), warning for subsequent
                log_level = logger.info if attempt == 1 else logger.warning
                log_level(
                    f"Failed to create session for server '{server_name}' (attempt {attempt}/{self.max_retries}): {e}, "
                    f"retrying in {self.retry_interval} seconds..."
                )
                await asyncio.sleep(self.retry_interval)
        
        # All retries failed
        error_msg = f"Failed to create session for server '{server_name}' after {self.max_retries} retries"
        logger.error(error_msg)
        raise last_exc or RuntimeError(error_msg)

    async def create_all_sessions(
        self,
        auto_initialize: bool = True,
    ) -> dict[str, MCPSession]:
        """Create sessions for all configured servers.

        Args:
            auto_initialize: Whether to automatically initialize the sessions.

        Returns:
            Dictionary mapping server names to their MCPSession instances.

        Warns:
            UserWarning: If no servers are configured.
        """
        servers = self._get_mcp_servers()
        
        if not servers:
            warnings.warn("No MCP servers defined in config", UserWarning, stacklevel=2)
            return {}

        # Create sessions for all servers (create_session already handles initialization)
        logger.debug(f"Creating sessions for {len(servers)} servers")
        for name in servers:
            try:
                await self.create_session(name, auto_initialize)
            except Exception as e:
                logger.error(f"Failed to create session for server '{name}': {e}")

        logger.info(f"Created {len(self.sessions)} MCP sessions")
        return self.sessions

    def get_session(self, server_name: str) -> MCPSession:
        """Get an existing session.

        Args:
            server_name: The name of the server to get the session for.
                        If None, uses the first active session.

        Returns:
            The MCPSession for the specified server.

        Raises:
            ValueError: If no active sessions exist or the specified session doesn't exist.
        """
        if server_name not in self.sessions:
            raise ValueError(f"No session exists for server '{server_name}'")

        return self.sessions[server_name]

    def get_all_active_sessions(self) -> dict[str, MCPSession]:
        """Get all active sessions.

        Returns:
            Dictionary mapping server names to their MCPSession instances.
        """
        return {name: self.sessions[name] for name in self.active_sessions if name in self.sessions}

    async def close_session(self, server_name: str) -> None:
        """Close a session.

        Args:
            server_name: The name of the server to close the session for.

        Raises:
            ValueError: If no active sessions exist or the specified session doesn't exist.
        """
        # Check if the session exists
        if server_name not in self.sessions:
            logger.warning(f"No session exists for server '{server_name}', nothing to close")
            return

        # Get the session
        session = self.sessions[server_name]
        error_occurred = False

        try:
            # Disconnect from the session
            logger.debug(f"Closing session for server '{server_name}'")
            await session.disconnect()
            logger.info(f"Successfully closed session for server '{server_name}'")
        except Exception as e:
            error_occurred = True
            logger.error(f"Error closing session for server '{server_name}': {e}")
        finally:
            # Remove the session regardless of whether disconnect succeeded
            self.sessions.pop(server_name, None)

            # Remove from active_sessions
            if server_name in self.active_sessions:
                self.active_sessions.remove(server_name)
            
            if error_occurred:
                logger.warning(f"Session for '{server_name}' removed from tracking despite disconnect error")

    async def close_all_sessions(self) -> None:
        """Close all active sessions.

        This method ensures all sessions are closed even if some fail.
        """
        # Get a list of all session names first to avoid modification during iteration
        server_names = list(self.sessions.keys())
        errors = []

        for server_name in server_names:
            try:
                logger.debug(f"Closing session for server '{server_name}'")
                await self.close_session(server_name)
            except Exception as e:
                error_msg = f"Failed to close session for server '{server_name}': {e}"
                logger.error(error_msg)
                errors.append(error_msg)

        # Log summary if there were errors
        if errors:
            logger.error(f"Encountered {len(errors)} errors while closing sessions")
        else:
            logger.debug("All sessions closed successfully")
