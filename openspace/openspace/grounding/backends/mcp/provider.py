"""
MCP Provider implementation.

This module provides a provider for managing MCP server sessions.
"""
import asyncio
from typing import Dict, List, Optional

from openspace.grounding.backends.mcp.session import MCPSession
from openspace.grounding.core.provider import Provider
from openspace.grounding.core.types import SessionConfig, BackendType, ToolSchema
from openspace.grounding.backends.mcp.client import MCPClient
from openspace.grounding.backends.mcp.installer import MCPInstallerManager, MCPDependencyError
from openspace.grounding.backends.mcp.tool_cache import get_tool_cache
from openspace.grounding.backends.mcp.tool_converter import _sanitize_mcp_schema
from openspace.grounding.core.tool import BaseTool, RemoteTool
from openspace.utils.logging import Logger
from openspace.config.utils import get_config_value

logger = Logger.get_logger(__name__)


class MCPProvider(Provider[MCPSession]):
    """
    MCP Provider manages multiple MCP server sessions.
    
    Each MCP server defined in config corresponds to one session.
    The provider handles lazy/eager session creation and tool aggregation.
    """
    
    def __init__(self, config: Dict | None = None, installer: Optional[MCPInstallerManager] = None):
        """Initialize MCP Provider.
        
        Args:
            config: Configuration dict with MCP server definitions.
                   Example: {"mcpServers": {"server1": {...}, "server2": {...}}}
            installer: Optional installer manager for dependency installation
        """
        super().__init__(BackendType.MCP, config)
        
        # Extract MCP-specific configuration
        sandbox = get_config_value(config, "sandbox", False)
        timeout = get_config_value(config, "timeout", 30)
        sse_read_timeout = get_config_value(config, "sse_read_timeout", 300.0)
        max_retries = get_config_value(config, "max_retries", 3)
        retry_interval = get_config_value(config, "retry_interval", 2.0)
        check_dependencies = get_config_value(config, "check_dependencies", True)
        auto_install = get_config_value(config, "auto_install", False)
        # Tool call retry settings (for transient errors like 400, 500, etc.)
        tool_call_max_retries = get_config_value(config, "tool_call_max_retries", 3)
        tool_call_retry_delay = get_config_value(config, "tool_call_retry_delay", 1.0)
        
        # Create sandbox options if sandbox is enabled
        sandbox_options = None
        if sandbox:
            sandbox_options = {
                "timeout": timeout,
                "sse_read_timeout": sse_read_timeout,
            }
        
        # Create installer with auto_install setting if not provided
        if installer is None and auto_install:
            installer = MCPInstallerManager(auto_install=True)
        
        # Initialize MCPClient with configuration
        self._client = MCPClient(
            config=config or {},
            sandbox=sandbox,
            sandbox_options=sandbox_options,
            timeout=timeout,
            sse_read_timeout=sse_read_timeout,
            max_retries=max_retries,
            retry_interval=retry_interval,
            installer=installer,
            check_dependencies=check_dependencies,
            tool_call_max_retries=tool_call_max_retries,
            tool_call_retry_delay=tool_call_retry_delay,
        )
        
        # Map server name to session for quick lookup
        self._server_sessions: Dict[str, MCPSession] = {}

    async def initialize(self) -> None:
        """Initialize the MCP provider.
        
        If config["eager_sessions"] is True, creates sessions for all configured servers.
        Otherwise, sessions are created lazily on first access.
        """
        if self.is_initialized:
            return

        # config can be dict or Pydantic model, use utility function
        eager = get_config_value(self.config, "eager_sessions", False)
        if eager:
            servers = self.list_servers()
            logger.debug(f"Eagerly initializing {len(servers)} MCP server sessions")
            for srv in servers:
                if srv not in self._server_sessions:
                    cfg = SessionConfig(
                        session_name=f"mcp-{srv}",
                        backend_type=BackendType.MCP,
                        connection_params={"server": srv},
                    )
                    await self.create_session(cfg)

        self.is_initialized = True
        logger.info(
            f"MCPProvider initialized with {len(self.list_servers())} servers (eager={eager})"
        )

    def list_servers(self) -> List[str]:
        """Return all configured MCP server names from MCPClient config.
        
        Returns:
            List of server names
        """
        return self._client.get_server_names()

    async def create_session(self, session_config: SessionConfig) -> MCPSession:
        """Create a new MCP session for a specific server.
        
        Args:
            session_config: Must contain 'server' in connection_params
            
        Returns:
            MCPSession instance
            
        Raises:
            ValueError: If 'server' not in connection_params
            Exception: If session creation or initialization fails
        """
        server = get_config_value(session_config.connection_params, "server")
        if not server:
            raise ValueError("MCPProvider.create_session requires 'server' in connection_params")

        # Generate session_id: mcp-<server_name>
        session_id = f"{self.backend_type.value}-{server}"

        # Check if session already exists
        if server in self._server_sessions:
            logger.debug(f"Session for server '{server}' already exists, returning existing session")
            return self._server_sessions[server]

        # Create session through MCPClient
        try:
            logger.debug(f"Creating new session for MCP server: {server}")
            session = await self._client.create_session(server, auto_initialize=True)
            session.session_id = session_id

            # Store in both maps
            self._server_sessions[server] = session
            self._sessions[session_id] = session
            
            logger.info(f"Created MCP session '{session_id}' for server '{server}'")
            return session
        except MCPDependencyError as e:
            # Dependency errors already shown to user, just debug log
            logger.debug(f"Dependency error for server '{server}': {type(e).__name__}")
            raise
        except Exception as e:
            logger.error(f"Failed to create session for server '{server}': {e}")
            raise

    async def close_session(self, session_name: str) -> None:
        """Close an MCP session by session name.
        
        Args:
            session_name: Session name in format 'mcp-<server_name>'
        """
        # Parse server name from session_name (format: mcp-<server_name>)
        try:
            prefix, server_name = session_name.split("-", 1)
            if prefix != self.backend_type.value:
                raise ValueError(f"Invalid MCP session name format: {session_name}, expected 'mcp-<server_name>'")
        except ValueError as e:
            logger.warning(f"Invalid session_name format: {session_name} - {e}")
            return

        # Check if session exists
        if session_name not in self._sessions and server_name not in self._server_sessions:
            logger.warning(f"Session '{session_name}' not found, nothing to close")
            return

        error_occurred = False
        try:
            logger.debug(f"Closing MCP session '{session_name}' (server: {server_name})")
            await self._client.close_session(server_name)
            logger.info(f"Successfully closed MCP session '{session_name}'")
        except Exception as e:
            error_occurred = True
            logger.error(f"Error closing MCP session '{session_name}': {e}")
        finally:
            # Clean up both maps regardless of errors
            self._server_sessions.pop(server_name, None)
            self._sessions.pop(session_name, None)
            
            if error_occurred:
                logger.warning(f"Session '{session_name}' removed from tracking despite close error")

    async def list_tools(self, session_name: str | None = None, use_cache: bool = True) -> List[BaseTool]:
        """List tools from MCP sessions.
        
        Args:
            session_name: If provided, only list tools from that session.
                         If None, list tools from all sessions.
            use_cache: If True, try to load from cache first (no server startup).
                      If False, start servers and get live tools.
        
        Returns:
            List of BaseTool instances
        """
        await self.ensure_initialized()
        
        # Case 1: List tools from specific session (always live, no cache)
        if session_name:
            sess = self._sessions.get(session_name)
            if sess:
                try:
                    tools = await sess.list_tools()
                    server_name = session_name.replace(f"{self.backend_type.value}-", "", 1)
                    for tool in tools:
                        tool.bind_runtime_info(
                            backend=self.backend_type,
                            session_name=session_name,
                            server_name=server_name,
                        )
                    return tools
                except Exception as e:
                    logger.error(f"Error listing tools from session '{session_name}': {e}")
                    return []
            else:
                logger.warning(f"Session '{session_name}' not found")
                return []

        # Case 2: List tools from all servers
        # Try cache first if enabled
        if use_cache:
            cache = get_tool_cache()
            if cache.has_cache():
                tools = self._load_tools_from_cache()
                if tools:
                    logger.info(f"Loaded {len(tools)} tools from cache (no server startup)")
                    return tools
        
        # No cache or cache disabled, start servers
        return await self._list_tools_live()
    
    def _load_tools_from_cache(self) -> List[BaseTool]:
        """Load tools from cache file without starting servers.
        
        Priority:
        1. Try to load from sanitized cache (mcp_tool_cache_sanitized.json)
        2. If not exists, load from raw cache and sanitize, then save sanitized version
        """
        cache = get_tool_cache()
        config_servers = self.list_servers()
        
        # Try sanitized cache first
        if cache.has_sanitized_cache():
            logger.debug("Loading from sanitized cache")
            all_cached_tools = cache.get_all_sanitized_tools()
            return self._build_tools_from_cache(all_cached_tools, config_servers)
        
        # Fall back to raw cache, sanitize and save
        if cache.has_cache():
            logger.info("Sanitized cache not found, building from raw cache...")
            all_cached_tools = cache.get_all_tools()
            sanitized_servers = self._sanitize_and_save_cache(all_cached_tools, cache)
            return self._build_tools_from_cache(sanitized_servers, config_servers)
        
        return []
    
    def _sanitize_and_save_cache(
        self, 
        raw_tools: Dict[str, List[Dict]], 
        cache
    ) -> Dict[str, List[Dict]]:
        """Sanitize raw cache and save to sanitized cache file."""
        sanitized_servers: Dict[str, List[Dict]] = {}
        
        for server_name, tool_list in raw_tools.items():
            sanitized_tools = []
            for tool_meta in tool_list:
                raw_params = tool_meta.get("parameters", {})
                sanitized_params = _sanitize_mcp_schema(raw_params)
                sanitized_tools.append({
                    "name": tool_meta["name"],
                    "description": tool_meta.get("description", ""),
                    "parameters": sanitized_params,
                })
            sanitized_servers[server_name] = sanitized_tools
        
        # Save sanitized cache for future use
        cache.save_sanitized(sanitized_servers)
        logger.info(f"Created sanitized cache with {len(sanitized_servers)} servers")
        
        return sanitized_servers
    
    def _build_tools_from_cache(
        self, 
        all_cached_tools: Dict[str, List[Dict]], 
        config_servers: List[str]
    ) -> List[BaseTool]:
        """Build BaseTool instances from cached tool metadata."""
        tools: List[BaseTool] = []
        
        for server_name in config_servers:
            tool_list = all_cached_tools.get(server_name)
            if not tool_list:
                continue
            
            session_name = f"{self.backend_type.value}-{server_name}"
            for tool_meta in tool_list:
                schema = ToolSchema(
                    name=tool_meta["name"],
                    description=tool_meta.get("description", ""),
                    parameters=tool_meta.get("parameters", {}),
                    backend_type=BackendType.MCP,
                )
                tool = RemoteTool(schema=schema, connector=None, backend=BackendType.MCP)
                tool.bind_runtime_info(
                    backend=self.backend_type,
                    session_name=session_name,
                    server_name=server_name,
                )
                tools.append(tool)
        
        return tools
    
    async def _list_tools_live(self) -> List[BaseTool]:
        """List tools by starting all servers.
        
        Uses a semaphore to serialize session creation, avoiding TaskGroup race conditions
        that occur when multiple MCP connections are initialized concurrently.
        """
        servers = self.list_servers()
        
        if not servers:
            logger.warning("No MCP servers configured")
            return []
        
        # Find servers that don't have sessions yet
        to_create = [s for s in servers if s not in self._server_sessions]

        # Create missing sessions with serialized execution using semaphore
        if to_create:
            logger.info(f"Creating {len(to_create)} MCP sessions (serialized to avoid race conditions)")
            
            # Use semaphore with limit=1 to serialize session creation
            # This avoids TaskGroup race conditions in concurrent HTTP connection setup
            semaphore = asyncio.Semaphore(1)
            
            async def _create_with_semaphore(server: str):
                async with semaphore:
                    logger.debug(f"Creating session for '{server}'")
                    return await self._lazy_create(server)
            
            tasks = [_create_with_semaphore(s) for s in to_create]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Log errors
            for i, result in enumerate(results):
                if isinstance(result, MCPDependencyError):
                    logger.debug(f"Dependency error for '{to_create[i]}': {type(result).__name__}")
                elif isinstance(result, Exception):
                    logger.error(f"Failed to create session for '{to_create[i]}': {result}")

        # Aggregate tools from all sessions
        uniq: Dict[tuple[str, str], BaseTool] = {}
        failed_servers = []
        
        logger.debug(f"Listing tools from {len(self._server_sessions)} sessions")
        for server, sess in self._server_sessions.items():
            try:
                tools = await sess.list_tools()
                session_name = f"{self.backend_type.value}-{server}"
                for tool in tools:
                    key = (server, tool.schema.name)
                    if key not in uniq:
                        tool.bind_runtime_info(
                            backend=self.backend_type,
                            session_name=session_name,
                            server_name=server,
                        )
                        uniq[key] = tool
            except Exception as e:
                failed_servers.append(server)
                logger.error(f"Error listing tools from server '{server}': {e}")
        
        if failed_servers:
            logger.warning(f"Failed to list tools from {len(failed_servers)} server(s): {failed_servers}")
        
        tools_list = list(uniq.values())
        logger.debug(f"Listed {len(tools_list)} unique tools from {len(self._server_sessions)} MCP servers")
        
        # Save to cache for next time
        await self._save_tools_to_cache(tools_list)
        
        return tools_list
    
    async def _save_tools_to_cache(self, tools: List[BaseTool]) -> None:
        """Save tools metadata to cache file."""
        cache = get_tool_cache()
        
        # Group tools by server
        servers: Dict[str, List[Dict]] = {}
        for tool in tools:
            server_name = tool.runtime_info.server_name if tool.is_bound else "unknown"
            if server_name not in servers:
                servers[server_name] = []
            servers[server_name].append({
                "name": tool.schema.name,
                "description": tool.schema.description or "",
                "parameters": tool.schema.parameters or {},
            })
        
        cache.save(servers)
    
    async def ensure_server_session(self, server_name: str) -> Optional[MCPSession]:
        """Ensure a server session exists, creating it if needed.
        
        This is used for on-demand server startup when executing tools.
        """
        if server_name in self._server_sessions:
            return self._server_sessions[server_name]
        
        # Server not running, start it
        logger.info(f"Starting MCP server on-demand: {server_name}")
        cfg = SessionConfig(
            session_name=f"mcp-{server_name}",
            backend_type=BackendType.MCP,
            connection_params={"server": server_name},
        )
        
        try:
            session = await self.create_session(cfg)
            return session
        except Exception as e:
            logger.error(f"Failed to start server '{server_name}': {e}")
            return None

    async def _lazy_create(self, server: str) -> None:
        """Internal helper for lazy session creation.
        
        Args:
            server: Server name to create session for
            
        Raises:
            Exception: Re-raises any exception from session creation for error tracking
        """
        # Double-check to avoid race conditions
        if server in self._server_sessions:
            logger.debug(f"Session for server '{server}' already exists, skipping lazy creation")
            return
        
        cfg = SessionConfig(
            session_name=f"mcp-{server}",
            backend_type=BackendType.MCP,
            connection_params={"server": server},
        )
        
        try:
            await self.create_session(cfg)
            logger.debug(f"Lazily created session for server '{server}'")
        except MCPDependencyError as e:
            # Dependency errors already shown to user
            logger.debug(f"Dependency error for server '{server}': {type(e).__name__}")
            # Re-raise so that asyncio.gather can track the error
            raise
        except Exception as e:
            logger.error(f"Failed to lazily create session for server '{server}': {e}")
            # Re-raise so that asyncio.gather can track the error
            raise