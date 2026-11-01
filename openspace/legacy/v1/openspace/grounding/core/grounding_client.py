import asyncio
import time
from collections import OrderedDict
from datetime import datetime
from typing import Any, Dict, List, Optional

from .types import BackendType, SessionConfig, SessionInfo, SessionStatus, ToolResult
from .exceptions import ErrorCode, GroundingError
from .tool import BaseTool
from .provider import Provider, ProviderRegistry
from .session import BaseSession
from .search_tools import SearchCoordinator
from openspace.config import GroundingConfig, get_config
from openspace.config.utils import get_config_value
from openspace.utils.logging import Logger
import importlib


class GroundingClient:
    """
    Global Entry, Facing Agent/Application, only concerned with Provider & Session
    """
    def __init__(self, config: Optional[GroundingConfig] = None, recording_manager=None) -> None:
        # Initialize logger first (needed by other initialization steps)
        self._logger = Logger.get_logger(__name__)
        
        self._config: GroundingConfig = config or get_config()
        self._registry: ProviderRegistry = ProviderRegistry()
        
        # Register providers from config
        self._register_providers_from_config()

        # Session
        self._sessions: Dict[str, BaseSession] = {}
        self._session_info: Dict[str, SessionInfo] = {}
        self._server_session_map: dict[tuple[BackendType, str], str] = {}             # (backend, server) -> session_name

        # Tool cache
        self._tool_cache: "OrderedDict[str, tuple[List[BaseTool], float]]" = OrderedDict()
        self._tool_cache_ttl: int = get_config_value(self._config, "tool_cache_ttl", 300)
        self._tool_cache_maxsize: int = get_config_value(self._config, "tool_cache_maxsize", 300)

        # Concurrent control
        self._lock = asyncio.Lock()
        self._cache_lock = asyncio.Lock()

        # Tool search coordinator
        self._search_coordinator: Optional[SearchCoordinator] = None
        
        # Recording manager (optional, for GUI intermediate step recording)
        self._recording_manager = recording_manager
        
        # Tool quality manager
        self._quality_manager = self._init_quality_manager()
        
        # Register SystemProvider (requires GroundingClient instance, so must be done after __init__)
        self._register_system_provider()
        
    def _register_providers_from_config(self) -> None:
            """
            Based on GroundingConfig.enabled_backends, register Provider instances to
            self._registry. Here only do *instantiation*, not await initialize(),
            to avoid blocking the event loop in the import stage; Provider will be lazily initialized when it is first used.
            
            Note: SystemProvider is skipped here and registered separately in _register_system_provider()
            because it requires a GroundingClient instance.
            """
            if not self._config.enabled_backends:
                self._logger.warning("No enabled_backends defined in config")
                return

            for item in self._config.enabled_backends:
                be_name: str | None = item.get("name")
                cls_path: str | None = item.get("provider_cls")
                if not (be_name and cls_path):
                    self._logger.warning("Invalid backend entry: %s", item)
                    continue

                backend = BackendType(be_name.lower())
                
                # Skip system backend - it will be registered separately
                if backend == BackendType.SYSTEM:
                    self._logger.debug("Skipping system backend in config registration (will be registered separately)")
                    continue
                
                if backend in self._registry.list():
                    continue        # Already registered

                # Dynamically import Provider class
                try:
                    module_path, _, cls_name = cls_path.rpartition(".")
                    module = importlib.import_module(module_path)
                    prov_cls = getattr(module, cls_name)
                except (ModuleNotFoundError, AttributeError) as e:
                    self._logger.error("Import provider failed: %s (%s)", cls_path, e)
                    continue

                backend_cfg = self._config.get_backend_config(be_name)
                provider: Provider = prov_cls(backend_cfg)
                self._registry.register(provider)
    
    def _register_system_provider(self) -> None:
        """
        Register SystemProvider separately because it requires GroundingClient instance.
        SystemProvider provides meta-level tools for querying system state (list providers, tools, etc.)
        and is always available regardless of configuration.
        """
        try:
            from .system import SystemProvider
            system_provider = SystemProvider(self)
            self._registry.register(system_provider)
            self._logger.debug("SystemProvider registered successfully")
        except Exception as e:
            self._logger.warning(f"Failed to register SystemProvider: {e}")
    
    def _init_quality_manager(self):
        """Initialize tool quality manager based on config."""
        try:
            # Check if quality tracking is enabled in config
            quality_config = getattr(self._config, 'tool_quality', None)
            if not quality_config or not getattr(quality_config, 'enabled', True):
                self._logger.debug("Tool quality tracking disabled")
                return None

            from .quality import ToolQualityManager, set_quality_manager
            from pathlib import Path
            from openspace.config.constants import PROJECT_ROOT

            # Shared DB path
            db_path = getattr(quality_config, 'db_path', None)
            if db_path:
                db_path = Path(db_path)
            else:
                # Default: same location as SkillStore
                db_dir = PROJECT_ROOT / ".openspace"
                db_dir.mkdir(parents=True, exist_ok=True)
                db_path = db_dir / "openspace.db"

            manager = ToolQualityManager(
                db_path=db_path,
                enable_persistence=getattr(quality_config, 'enable_persistence', True),
                auto_save=True,
                evolve_interval=getattr(quality_config, 'evolve_interval', 5),
            )

            # Set as global manager for BaseTool access
            set_quality_manager(manager)

            self._logger.info(
                f"ToolQualityManager initialized "
                f"(records={len(manager._records)})"
            )
            return manager

        except Exception as e:
            self._logger.warning(f"Failed to initialize ToolQualityManager: {e}")
            return None
    
    @property
    def quality_manager(self):
        """Get the tool quality manager."""
        return self._quality_manager
    
    # Quality API for Upper Layer
    def get_quality_report(self) -> Dict[str, Any]:
        """
        Get comprehensive tool quality report.
        """
        if not self._quality_manager:
            return {"status": "disabled", "message": "Quality tracking not enabled"}
        return self._quality_manager.get_quality_report()
    
    async def evolve_quality(self) -> Dict[str, Any]:
        """
        Run quality self-evolution cycle.
        
        This triggers:
        - Tool change detection
        - Description re-evaluation for updated tools
        - Adaptive quality weight computation
        
        Call this periodically or after tool set changes.
        """
        if not self._quality_manager:
            return {"status": "disabled"}
        
        # Get all tools
        all_tools = await self.list_tools()
        return await self._quality_manager.evolve(all_tools)
    
    def get_tool_insights(self, tool: BaseTool) -> Dict[str, Any]:
        """
        Get detailed quality insights for a specific tool.
        """
        if not self._quality_manager:
            return {"status": "disabled"}
        return self._quality_manager.get_tool_insights(tool)

    def register_provider(self, provider: Provider) -> None:
        self._registry.register(provider)
    
    def get_provider(self, backend: BackendType) -> Provider:
        return self._registry.get(backend)

    def list_providers(self) -> Dict[BackendType, Provider]:
        return self._registry.list()
    
    @property
    def recording_manager(self):
        """Get the recording manager."""
        return self._recording_manager
    
    @recording_manager.setter
    def recording_manager(self, manager):
        """
        Set or update the recording manager.
        This allows coordinator to inject recording_manager after GroundingClient creation.
        """
        self._recording_manager = manager
        self._logger.info("GroundingClient: RecordingManager updated")
    
    async def initialize_all_providers(self) -> None:
        await asyncio.gather(*[provider.initialize() for provider in self._registry.list().values() if not provider.is_initialized])


    async def create_session(
        self,
        *,
        backend: BackendType,
        name: str | None = None,
        connection_params: Dict[str, Any] | None = None,
        server: str | None = None,
        **options,
    ) -> str:
        """
        Create and initialize Session, return "session_name" (external visible)
        name is auto generated when it's None: <backend>-<index>
        MCP backend needs to provide server
        """
        async with self._lock:
            # Check concurrent sessions limit
            max_sessions = get_config_value(self._config, "max_concurrent_sessions", 100)
            if len(self._sessions) >= max_sessions:
                raise GroundingError(f"Reached maximum session limit: {max_sessions}")

            # Session naming strategy
            if server:                                       # Only MCP will pass in server
                name = name or f"{backend.value}-{server}"
            else:
                name = name or backend.value                 # Other backends have a fixed 1 session
                
            if name in self._sessions:
                # Reuse existing session
                self._logger.warning("Session '%s' exists, reusing.", name)
                return name

        # Get Provider (initialize if first time)
        provider = self._registry.get(backend)
        if not provider.is_initialized:
            await provider.initialize()
            
        if backend == BackendType.MCP:
            if server is None:
                raise GroundingError("Must specify 'server' when creating MCP session")

        # Construct SessionConfig, pass to Provider to create
        connection_params = connection_params or {}
        if server:
            connection_params.setdefault("server", server)
        
        # Inject recording_manager for GUI backend (for intermediate step recording)
        if backend == BackendType.GUI and self._recording_manager is not None:
            connection_params.setdefault("recording_manager", self._recording_manager)

        sess_cfg = SessionConfig(
            session_name=name, # Use external visible name
            backend_type=backend,
            connection_params=connection_params,
            **options,
        )
        session_obj = await provider.create_session(sess_cfg)

        # Store session and monitoring info
        async with self._lock:
            self._sessions[name] = session_obj
            now = datetime.utcnow()
            self._session_info[name] = SessionInfo(
                session_name=name,
                backend_type=backend,
                status=SessionStatus.CONNECTED,
                created_at=now,
                last_activity=now,
            )
            if server:
                self._server_session_map[(backend, server)] = name

        self._logger.info("Session created: %s", name)
        return name
    
    def list_sessions(self) -> List[str]:
        return list(self._sessions.keys())

    async def close_session(self, name: str) -> None:
        async with self._lock:
            session = self._sessions.pop(name, None)
            info = self._session_info.pop(name, None)
            self._tool_cache.pop(name, None)

            for k, v in list(self._server_session_map.items()):
                if v == name:
                    self._server_session_map.pop(k)

        if not session:
            self._logger.warning("Session '%s' not found", name)
            return

        try:
            provider = self._registry.get(info.backend_type) if info else None
            if provider:
                await provider.close_session(name)
            else:
                # Fallback: if no provider, disconnect directly
                await session.disconnect()
        finally:
            self._logger.info("Session closed: %s", name)

    async def close_all_sessions(self) -> None:
        for sid in list(self._sessions.keys()):
            await self.close_session(sid)
            
    async def ensure_session(self, backend: BackendType, server: str | None = None) -> str:
        sid = backend.value if server is None else f"{backend.value}-{server}"
        if sid not in self._sessions:
            await self.create_session(backend=backend, name=sid, server=server)
        return sid
            
    def get_session_info(self, name: str) -> SessionInfo:
        """Get session monitoring info"""
        if name not in self._session_info:
            raise GroundingError(f"Session not found: {name}", code=ErrorCode.SESSION_NOT_FOUND)
        return self._session_info[name]
    
    def get_session(self, name: str) -> BaseSession:
        """Get session"""
        if name not in self._sessions:
            raise GroundingError(f"Session not found: {name}", code=ErrorCode.SESSION_NOT_FOUND)
        return self._sessions[name]
    
    
    async def _fetch_tools(
        self,
        backend: BackendType,
        *,
        session_name: str | None = None,
        use_cache: bool = False,
        bind_runtime_info: bool = True,  
    ) -> List[BaseTool]:
        """
        Fetch tools from provider.
        
        Args:
            backend: Backend type
            session_name: 
                - None: fetch all tools from all sessions of this backend
                - str: fetch tools from specific session
            use_cache: Whether to use cache
            bind_runtime_info: Whether to bind runtime info to tool instances
        """
        now = time.time()
        
        # Auto-generate cache_scope from parameters
        if session_name:
            cache_scope = session_name
        else:
            cache_scope = f"backend-{backend.value}"

        # Check cache
        if use_cache:
            async with self._cache_lock:
                if cache_scope in self._tool_cache:
                    tools, ts = self._tool_cache[cache_scope]
                    if now - ts < self._tool_cache_ttl:
                        self._tool_cache.move_to_end(cache_scope)
                        return tools

        provider = self._registry.get(backend)
        if not provider.is_initialized:
            await provider.initialize()

        tools = await provider.list_tools(session_name=session_name)

        if bind_runtime_info:
            # If session_name is specified, bind all tools to that session
            if session_name:
                server_name = None
                if backend == BackendType.MCP:
                    server_name = session_name.replace(f"{backend.value}-", "", 1)
                
                for tool in tools:
                    tool.bind_runtime_info(
                        backend=backend,
                        session_name=session_name,
                        server_name=server_name,
                        grounding_client=self,
                    )
            else:
                # No session_name specified - get tools from all sessions
                # For each backend, find the default/primary session
                # For Shell/Web/GUI: use the default session (backend.value)
                # For MCP: tools should already be bound by the provider
                default_session_name = None
                
                # Try to find an existing session for this backend
                for sid, info in self._session_info.items():
                    if info.backend_type == backend:
                        default_session_name = sid
                        break
                
                # Fallback: use backend default naming
                if not default_session_name:
                    default_session_name = backend.value
                
                server_name = None
                if backend == BackendType.MCP and default_session_name:
                    server_name = default_session_name.replace(f"{backend.value}-", "", 1)
                
                for tool in tools:
                    # Only bind if tool doesn't have runtime info already
                    # (some providers like MCP bind runtime info during list_tools)
                    if not tool.is_bound:
                        tool.bind_runtime_info(
                            backend=backend,
                            session_name=default_session_name,
                            server_name=server_name,
                            grounding_client=self,
                        )
                    elif not tool.runtime_info.grounding_client:
                        # Tool has runtime info but no grounding_client, add it
                        tool.bind_runtime_info(
                            backend=tool.runtime_info.backend,
                            session_name=tool.runtime_info.session_name,
                            server_name=tool.runtime_info.server_name,
                            grounding_client=self,
                        )

        # Save to cache
        if use_cache:
            async with self._cache_lock:
                self._tool_cache[cache_scope] = (tools, now)
                self._tool_cache.move_to_end(cache_scope)
                while len(self._tool_cache) > self._tool_cache_maxsize:
                    self._tool_cache.popitem(last=False)

        return tools

    async def list_tools(
        self,
        backend: BackendType | list[BackendType] | None = None,
        session_name: str | None = None,
        *,
        use_cache: bool = False,
    ) -> List[BaseTool]:
        """
        List tools from backend(s) or session.
        
        1. session_name is provided → return tools from that session
        2. backend is list → return tools from multiple backends
        3. backend is single → return tools from that backend
        4. backend is None → return tools from all backends
        
        Args:
            backend: Single backend, list of backends, or None for all
            session_name: Specific session name (overrides backend parameter)
            use_cache: Whether to use cache
            
        Returns:
            List of tools
        """
        # Session-level
        if session_name:                  
            if session_name not in self._sessions:
                raise GroundingError(f"Session not found: {session_name}", code=ErrorCode.SESSION_NOT_FOUND)
            backend_type = self._session_info[session_name].backend_type
            return await self._fetch_tools(
                backend_type,
                session_name=session_name,
                use_cache=use_cache,
            )
        
        # Multiple backends
        if isinstance(backend, list):
            tools: List[BaseTool] = []
            for be in backend:
                backend_tools = await self._fetch_tools(
                    be,
                    session_name=None,  # Provider aggregates all sessions
                    use_cache=use_cache,
                )
                tools.extend(backend_tools)
            return tools
        
        # Single backend
        if backend is not None:
            return await self._fetch_tools(
                backend,
                session_name=None,
                use_cache=use_cache,
            )

        # All backends
        tools: List[BaseTool] = []
        for backend_type in self._registry.list().keys():
            backend_tools = await self._fetch_tools(
                backend_type,
                session_name=None,
                use_cache=use_cache,
            )
            tools.extend(backend_tools)
        return tools

    async def list_backend_tools(
        self, 
        backend: BackendType | list[BackendType] | None = None,
        use_cache: bool = False
    ) -> list[BaseTool]:
        return await self.list_tools(backend=backend, session_name=None, use_cache=use_cache)

    async def list_session_tools(
        self, 
        session_name: str, 
        use_cache: bool = False
    ) -> list[BaseTool]:
        if session_name not in self._session_info:
            raise GroundingError(f"Session not found: {session_name}", code=ErrorCode.SESSION_NOT_FOUND)
        backend = self._session_info[session_name].backend_type
        return await self.list_tools(backend, session_name, use_cache)

    async def list_all_backend_tools(
        self,
        use_cache: bool = False
    ) -> Dict[BackendType, list[BaseTool]]:
        """List static tools for every registered backend."""
        result = {}
        for backend_type in self.list_providers().keys():
            tools = await self.list_backend_tools(backend=backend_type, use_cache=use_cache)
            result[backend_type] = tools
        return result

    async def search_tools(
        self,
        task_description: str,
        *,
        backend: BackendType | list[BackendType] | None = None,
        session_name: str | None = None,
        max_tools: int | None = None,
        search_mode: str | None = None,
        use_cache: bool = True,
        llm_callable = None,
        enable_llm_filter: bool | None = None,
        llm_filter_threshold: int | None = None,
        enable_cache_persistence: bool | None = None,
        cache_dir: str | None = None,
    ) -> list[BaseTool]:
        """
        Search tools from backend(s) or session.
        
        Args:
            task_description: Task description for searching relevant tools
            backend: Backend type(s) to search
            session_name: Specific session to search
            max_tools: Maximum number of tools to return
            search_mode: Search mode ("semantic", "keyword", "hybrid")
            use_cache: Whether to use cached tool list
            llm_callable: LLM client for intelligent filtering
            enable_llm_filter: Whether to use LLM pre-filtering
            llm_filter_threshold: Threshold for applying LLM filter
            enable_cache_persistence: Whether to persist embeddings to disk. If None, uses config value.
            cache_dir: Directory for persistent cache. If None, uses config value or default.
        """
        candidate_tools = await self.list_tools(
            backend=backend,
            session_name=session_name,
            use_cache=use_cache,
        )
        
        if not candidate_tools:
            self._logger.warning("No candidate tools found for search")
            return []
        
        # lazy initialize SearchCoordinator (or recreate if parameters changed)
        if self._search_coordinator is None:
            # Get quality ranking settings from config
            quality_config = getattr(self._config, 'tool_quality', None)
            enable_quality_ranking = getattr(quality_config, 'enable_quality_ranking', True) if quality_config else True
            
            self._search_coordinator = SearchCoordinator(
                max_tools=max_tools,
                llm=llm_callable,
                enable_llm_filter=enable_llm_filter,
                llm_filter_threshold=llm_filter_threshold,
                enable_cache_persistence=enable_cache_persistence,
                cache_dir=cache_dir,
                quality_manager=self._quality_manager,
                enable_quality_ranking=enable_quality_ranking,
            )
        
        # execute search and sort
        try:
            filtered_tools = await self._search_coordinator._arun(
                task_prompt=task_description,
                candidate_tools=candidate_tools,
                max_tools=max_tools,
                mode=search_mode,
            )
            return filtered_tools
        except Exception as exc:
            self._logger.error(f"Tool search failed: {exc}")
            # fallback: return top N tools
            fallback_max = max_tools or self._config.tool_search.max_tools
            return candidate_tools[:fallback_max]
    
    def get_last_search_debug_info(self) -> Optional[Dict[str, Any]]:
        """Get debug info from the last tool search operation.
        
        Returns:
            Dict containing search debug info, or None if no search has been performed.
        """
        if self._search_coordinator is None:
            return None
        return self._search_coordinator.get_last_search_debug_info()
    
    async def get_tools_with_auto_search(
        self,
        *,
        task_description: str | None = None,
        backend: BackendType | list[BackendType] | None = None,
        session_name: str | None = None,
        max_tools: int | None = None,
        search_mode: str | None = None,
        use_cache: bool = True,
        llm_callable = None,
        enable_llm_filter: bool | None = None,
        llm_filter_threshold: int | None = None,
        enable_cache_persistence: bool | None = None,
        cache_dir: str | None = None,
    ) -> list[BaseTool]:
        """
        Intelligent tool retrieval: automatically decides whether to return all tools or trigger search.
        
        Logic:
        - If tool_count <= max_tools: return all tools directly
        - If tool_count > max_tools: trigger search and return top max_tools
        
        Args:
            task_description: Task description (required for search if triggered). 
                If None, search will not be triggered even if tool count exceeds max_tools.
            backend: Backend type(s) to query
            session_name: Specific session name
            max_tools: Maximum number of tools to return. Also acts as the threshold for triggering search.
                - None: Use value from config (default: 30)
            search_mode: Search mode ("semantic", "keyword", "hybrid")
            use_cache: Whether to use cache
            llm_callable: LLM client (for intelligent filtering)
            enable_llm_filter: Whether to use LLM for backend/server pre-filtering.
                - None: Use config default
                - False: Disable LLM filter, use tool-level search only
                - True: Enable LLM filter
            llm_filter_threshold: Only apply LLM filter when tool count > this threshold.
                - None: Use default (50)
                - N: Only apply LLM filter when > N tools
            enable_cache_persistence: Whether to persist embeddings to disk. If None, uses config value.
            cache_dir: Directory for persistent cache. If None, uses config value or default.
            
        Returns:
            List of tools (at most max_tools)
            
        Examples:
            # Scenario 1: Auto-detect whether search is needed
            tools = await gc.get_tools_with_auto_search(
                task_description="Create a flowchart",
                backend=BackendType.MCP
            )
            
            # Scenario 2: Custom max_tools
            tools = await gc.get_tools_with_auto_search(
                task_description="Edit file",
                backend=BackendType.SHELL,
                max_tools=30  # Return at most 30 tools
            )
            
            # Scenario 3: Disable search (return all tools regardless of count)
            tools = await gc.get_tools_with_auto_search(
                backend=BackendType.MCP  # No task_description = no search
            )
        """
        # Fetch all candidate tools
        all_tools = await self.list_tools(
            backend=backend,
            session_name=session_name,
            use_cache=use_cache,
        )
        
        if not all_tools:
            self._logger.warning("No tools found")
            return []
        
        # Determine max_tools from config if not provided
        if max_tools is None:
            max_tools = self._config.tool_search.max_tools
        
        # Decide whether search is needed
        tools_count = len(all_tools)
        need_search = tools_count > max_tools and task_description is not None
        
        if need_search:
            self._logger.info(
                f"Tool count ({tools_count}) > max_tools ({max_tools}), "
                f"triggering search to filter relevant tools..."
            )
            return await self.search_tools(
                task_description=task_description,
                backend=backend,
                session_name=session_name,
                max_tools=max_tools,
                search_mode=search_mode,
                use_cache=use_cache,
                llm_callable=llm_callable,
                enable_llm_filter=enable_llm_filter,
                llm_filter_threshold=llm_filter_threshold,
                enable_cache_persistence=enable_cache_persistence,
                cache_dir=cache_dir,
            )
        else:
            if task_description is None:
                self._logger.debug(
                    f"No task description provided, returning all {tools_count} tools"
                )
            else:
                self._logger.debug(
                    f"Tool count ({tools_count}) ≤ max_tools ({max_tools}), "
                    f"returning all tools without search"
                )
            return all_tools

    async def invoke_tool(
        self,
        tool: BaseTool | str,
        parameters: Dict[str, Any] | None = None,
        *,
        backend: BackendType | None = None,
        session_name: str | None = None,
        server: str | None = None,
        keep_session: bool = False,
        **kwargs
    ) -> ToolResult:
        """
        Universal tool invocation method.
        Supports multiple calling patterns:
        
        1. Using BaseTool instance with bound runtime info
        2. Using BaseTool instance with explicit backend/session
        3. Using tool name with automatic lookup
        4. Using tool name with explicit backend/session/server
        
        Args:
            tool: BaseTool instance or tool name string
            parameters: Tool parameters as dict
            backend: Backend type (optional for BaseTool with runtime_info)
            session_name: Session name (optional for BaseTool with runtime_info)
            server: Server name (for MCP, optional for BaseTool with runtime_info)
            keep_session: Whether to keep session alive after invocation
            **kwargs: Alternative parameter passing
        
        Returns:
            ToolResult
        
        Examples:
            # Pattern 1: Tool instance with runtime info (from list_tools)
            tools = await gc.list_tools()
            tool = next(t for t in tools if t.name == "read_file")
            result = await gc.invoke_tool(tool, {"path": "/tmp/a.txt"})
            
            # Pattern 2: Tool instance with explicit backend/session
            my_tool = MyTool()
            result = await gc.invoke_tool(
                my_tool, 
                {"arg": "value"}, 
                backend=BackendType.SHELL
            )
            
            # Pattern 3: Tool name with automatic lookup
            result = await gc.invoke_tool("read_file", {"path": "/tmp/a.txt"})
            
            # Pattern 4: Tool name with explicit backend/server
            result = await gc.invoke_tool(
                "read_file",
                {"path": "/tmp/a.txt"},
                backend=BackendType.MCP,
                server="filesystem"
            )
        """
        params = parameters or kwargs
        
        # BaseTool instance
        if isinstance(tool, BaseTool):
            tool_name = tool.schema.name
            
            # Try to use bound runtime info first
            if tool.is_bound and not (backend or session_name or server):
                # Use runtime info
                runtime_backend = tool.runtime_info.backend
                runtime_session = tool.runtime_info.session_name
                runtime_server = tool.runtime_info.server_name
            else:
                # Use provided or tool's default backend
                runtime_backend = backend or tool.backend_type
                runtime_session = session_name
                runtime_server = server
                
                if runtime_backend == BackendType.NOT_SET:
                    raise GroundingError(
                        f"Cannot invoke tool '{tool_name}': no backend specified. "
                        f"Either bind runtime info or provide backend parameter.",
                        code=ErrorCode.TOOL_EXECUTION_FAIL
                    )
    
        # Tool name string
        elif isinstance(tool, str):
            tool_name = tool
            
            # If explicit backend/session provided, use them
            if backend or session_name:
                runtime_session = session_name
                runtime_server = server
                
                # Infer backend: prefer explicit backend; otherwise get from session
                if backend is not None:
                    runtime_backend = backend
                else:
                    if runtime_session not in self._session_info:
                        raise GroundingError(f"Session not found: {runtime_session}", code=ErrorCode.SESSION_NOT_FOUND)
                    runtime_backend = self._session_info[
                        runtime_session
                    ].backend_type
            else:
                # Auto-lookup: search for the tool
                all_tools = await self.list_tools(use_cache=True)
                matching = [t for t in all_tools if t.name == tool_name]
                
                if not matching:
                    raise GroundingError(
                        f"Tool '{tool_name}' not found",
                        code=ErrorCode.TOOL_NOT_FOUND
                    )
                
                if len(matching) > 1:
                    sources = [
                        f"{t.runtime_info.backend.value}/{t.runtime_info.session_name}" 
                        for t in matching if t.is_bound
                    ]
                    raise GroundingError(
                        f"Multiple tools named '{tool_name}' found in: {sources}. "
                        f"Please specify 'backend' or 'session_name' parameter.",
                        code=ErrorCode.AMBIGUOUS_TOOL
                    )
                
                # Use the found tool's runtime info
                found_tool = matching[0]
                runtime_backend = found_tool.runtime_info.backend
                runtime_session = found_tool.runtime_info.session_name
                runtime_server = found_tool.runtime_info.server_name
        
        # Execute the tool
        # Ensure session exists (except for SYSTEM backend which doesn't use sessions)
        # Check if session really exists - cached tools have session_name but session may not be running
        if runtime_backend != BackendType.SYSTEM:
            if not runtime_session or runtime_session not in self._sessions:
                runtime_session = await self.ensure_session(runtime_backend, runtime_server)
        
        try:
            provider = self._registry.get(runtime_backend)
            # SystemProvider doesn't use sessions, pass a dummy value
            session_param = runtime_session if runtime_session else "system"
            result = await provider.call_tool(session_param, tool_name, params)
            
            # Update last_activity in session_info (skip for SYSTEM backend)
            if runtime_backend != BackendType.SYSTEM and runtime_session and runtime_session in self._session_info:
                async with self._lock:
                    old_info = self._session_info[runtime_session]
                    self._session_info[runtime_session] = old_info.model_copy(
                        update={"last_activity": datetime.utcnow()}
                    )
            
            return result
        finally:
            # Auto-close session if requested (skip for SYSTEM backend)
            if runtime_backend != BackendType.SYSTEM and not keep_session and runtime_session:
                if runtime_server or runtime_session.startswith(runtime_backend.value):
                    await self.close_session(runtime_session)