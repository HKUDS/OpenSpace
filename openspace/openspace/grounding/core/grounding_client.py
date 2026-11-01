import asyncio
import time
from collections import OrderedDict
from datetime import datetime
from typing import Any, Dict, List, Optional

from .types import BackendType, SessionConfig, SessionInfo, SessionStatus, ToolResult, ToolStatus
from .exceptions import ErrorCode, GroundingError
from .tool import BaseTool
from .provider import Provider, ProviderRegistry
from .session import BaseSession
from .search_tools import ToolPreselector
from .tool_executor import ToolExecutor, get_default_tool_executor
from openspace.services.tooling.context import ReadFileEntry
from openspace.config import GroundingConfig, get_config
from openspace.config.utils import get_config_value
from openspace.utils.logging import Logger
import importlib


class GroundingClient:
    """
    Global Entry, Facing Agent/Application, only concerned with Provider & Session
    """
    def __init__(
        self,
        config: Optional[GroundingConfig] = None,
        recording_manager=None,
        tool_executor: Optional[ToolExecutor] = None,
    ) -> None:
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
        self._direct_read_file_states: dict[tuple[str, str, str], dict[str, ReadFileEntry]] = {}

        # Tool cache
        self._tool_cache: "OrderedDict[str, tuple[List[BaseTool], float]]" = OrderedDict()
        self._tool_cache_ttl: int = get_config_value(self._config, "tool_cache_ttl", 300)
        self._tool_cache_maxsize: int = get_config_value(self._config, "tool_cache_maxsize", 300)

        # Concurrent control
        self._lock = asyncio.Lock()
        self._cache_lock = asyncio.Lock()

        # System-side tool preselector. This is distinct from model-facing
        # ``tool_search`` deferred schema discovery.
        self._tool_preselector: Optional[ToolPreselector] = None
        
        # Recording manager (optional, for GUI intermediate step recording)
        self._recording_manager = recording_manager
        
        # Tool quality manager
        self._quality_manager = self._init_quality_manager()

        # Full pipeline executor for the legacy invoke_tool facade.  The
        # default is lazy and module-backed so grounding.core has no direct
        # import edge to the tool runtime implementation.
        self._tool_executor: ToolExecutor = tool_executor or get_default_tool_executor()
        
        # Register MetaProvider (requires GroundingClient instance, so must be done after __init__)
        self._register_meta_provider()

    @property
    def tool_executor(self) -> ToolExecutor:
        return self._tool_executor

    def set_tool_executor(self, tool_executor: Optional[ToolExecutor]) -> None:
        self._tool_executor = tool_executor or get_default_tool_executor()
        
    def _register_providers_from_config(self) -> None:
            """
            Based on GroundingConfig.enabled_backends, register Provider instances to
            self._registry. Here only do *instantiation*, not await initialize(),
            to avoid blocking the event loop in the import stage; Provider will be lazily initialized when it is first used.
            
            Note: MetaProvider is skipped here and registered separately in _register_meta_provider()
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
                
                # Skip meta backend - it will be registered separately
                if backend == BackendType.META:
                    self._logger.debug("Skipping meta backend in config registration (will be registered separately)")
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
    
    def _register_meta_provider(self) -> None:
        """
        Register MetaProvider separately because it requires GroundingClient instance.
        MetaProvider provides meta-level tools for querying backend state (list providers, tools, etc.)
        and is always available regardless of configuration.
        """
        try:
            from .meta import MetaProvider
            meta_provider = MetaProvider(self)
            self._registry.register(meta_provider)
            self._logger.debug("MetaProvider registered successfully")
        except Exception as e:
            self._logger.warning(f"Failed to register MetaProvider: {e}")
    
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
            )

            # Share the active manager with the tool execution quality hook.
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

    def list_provider_sessions(self, backend: BackendType) -> List[str]:
        """Return active session names owned by a provider."""
        provider = self._registry.get(backend)
        return provider.list_sessions()

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

    @staticmethod
    def _configure_session_workspace_object(
        session: BaseSession,
        workspace_dir: str,
    ) -> bool:
        configure_workspace = getattr(session, "configure_workspace", None)
        if callable(configure_workspace):
            configure_workspace(workspace_dir)
            return True

        if hasattr(session, "default_working_dir"):
            setattr(session, "default_working_dir", workspace_dir)
            return True

        return False

    def configure_session_workspace(
        self,
        session_name: str,
        workspace_dir: str,
    ) -> bool:
        """Update a session's default workspace when the backend supports it."""
        session = self._sessions.get(session_name)
        if session is not None and self._configure_session_workspace_object(
            session,
            workspace_dir,
        ):
            return True

        registry = getattr(self, "_registry", None)
        if registry is None:
            return False

        for provider in registry.list().values():
            provider_session = provider.get_session(session_name)
            if provider_session is not None:
                return self._configure_session_workspace_object(
                    provider_session,
                    workspace_dir,
                )
        return False

    def configure_backend_workspace(
        self,
        backend: BackendType,
        workspace_dir: str,
    ) -> int:
        """Update all active sessions for a backend to use a workspace."""
        updated = 0
        seen_session_names: set[str] = set()
        for name, info in list(self._session_info.items()):
            if info.backend_type != backend:
                continue
            session = self._sessions.get(name)
            if session is not None and self._configure_session_workspace_object(
                session,
                workspace_dir,
            ):
                updated += 1
                seen_session_names.add(name)

        registry = getattr(self, "_registry", None)
        if registry is None:
            return updated

        try:
            provider = registry.get(backend)
        except Exception:
            return updated

        for name in provider.list_sessions():
            if name in seen_session_names:
                continue
            session = provider.get_session(name)
            if session is None:
                continue
            if self._configure_session_workspace_object(session, workspace_dir):
                updated += 1
                seen_session_names.add(name)
        return updated
    
    
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

    async def preselect_tools(
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
        Preselect relevant tools from backend(s) or session.
        
        Args:
            task_description: Task description for preselecting relevant tools
            backend: Backend type(s) to search
            session_name: Specific session to search
            max_tools: Maximum number of tools to return
            search_mode: Ranking mode ("semantic", "keyword", "hybrid")
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
            self._logger.warning("No candidate tools found for preselection")
            return []
        
        # Lazily initialize the system-side preselector.
        if self._tool_preselector is None:
            # Get quality ranking settings from config
            quality_config = getattr(self._config, 'tool_quality', None)
            enable_quality_ranking = getattr(quality_config, 'enable_quality_ranking', True) if quality_config else True
            
            self._tool_preselector = ToolPreselector(
                max_tools=max_tools,
                llm=llm_callable,
                enable_llm_filter=enable_llm_filter,
                llm_filter_threshold=llm_filter_threshold,
                enable_cache_persistence=enable_cache_persistence,
                cache_dir=cache_dir,
                quality_manager=self._quality_manager,
                enable_quality_ranking=enable_quality_ranking,
            )
        
        # Execute preselection and ranking.
        try:
            filtered_tools = await self._tool_preselector._arun(
                task_prompt=task_description,
                candidate_tools=candidate_tools,
                max_tools=max_tools,
                mode=search_mode,
            )
            return filtered_tools
        except Exception as exc:
            self._logger.error(f"Tool preselection failed: {exc}")
            # fallback: return top N tools
            fallback_max = max_tools or self._config.tool_search.max_tools
            return candidate_tools[:fallback_max]
    
    def get_last_preselection_debug_info(self) -> Optional[Dict[str, Any]]:
        """Get debug info from the last tool preselection operation.
        
        Returns:
            Dict containing preselection debug info, or None if no preselection has been performed.
        """
        if self._tool_preselector is None:
            return None
        return self._tool_preselector.get_last_preselection_debug_info()
    
    async def get_tools_with_auto_preselection(
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
        Intelligent tool retrieval: automatically decides whether to return all tools or trigger preselection.
        
        Logic:
        - If tool_count <= max_tools: return all tools directly
        - If tool_count > max_tools: trigger preselection and return top max_tools
        
        Args:
            task_description: Task description (required for preselection if triggered).
                If None, preselection will not be triggered even if tool count exceeds max_tools.
            backend: Backend type(s) to query
            session_name: Specific session name
            max_tools: Maximum number of tools to return. Also acts as the threshold for triggering preselection.
                - None: Use value from config (default: 30)
            search_mode: Ranking mode ("semantic", "keyword", "hybrid")
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
            # Scenario 1: Auto-detect whether preselection is needed
            tools = await gc.get_tools_with_auto_preselection(
                task_description="Create a flowchart",
                backend=BackendType.MCP
            )
            
            # Scenario 2: Custom max_tools
            tools = await gc.get_tools_with_auto_preselection(
                task_description="Edit file",
                backend=BackendType.SHELL,
                max_tools=30  # Return at most 30 tools
            )
            
            # Scenario 3: Disable preselection (return all tools regardless of count)
            tools = await gc.get_tools_with_auto_preselection(
                backend=BackendType.MCP  # No task_description = no preselection
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
        
        # Decide whether preselection is needed
        tools_count = len(all_tools)
        need_preselection = tools_count > max_tools and task_description is not None
        
        if need_preselection:
            self._logger.info(
                f"Tool count ({tools_count}) > max_tools ({max_tools}), "
                f"triggering preselection to filter relevant tools..."
            )
            return await self.preselect_tools(
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

    async def _resolve_tool_invocation(
        self,
        tool: BaseTool | str,
        parameters: Dict[str, Any] | None,
        *,
        backend: BackendType | None,
        session_name: str | None,
        server: str | None,
        kwargs: Dict[str, Any],
    ) -> tuple[
        str,
        Dict[str, Any],
        BackendType,
        str | None,
        str | None,
        BaseTool | None,
        bool,
    ]:
        params = parameters or kwargs
        resolved_tool: BaseTool | None = None
        from_tool_name = False

        if isinstance(tool, BaseTool):
            resolved_tool = tool
            tool_name = tool.schema.name

            if tool.is_bound and not (backend or session_name or server):
                runtime_info = tool.runtime_info
                runtime_backend = runtime_info.backend
                runtime_session = runtime_info.session_name
                runtime_server = runtime_info.server_name
            else:
                runtime_backend = backend or tool.backend_type
                runtime_session = session_name
                runtime_server = server

                if runtime_backend == BackendType.NOT_SET:
                    raise GroundingError(
                        f"Cannot invoke tool '{tool_name}': no backend specified. "
                        f"Either bind runtime info or provide backend parameter.",
                        code=ErrorCode.TOOL_EXECUTION_FAIL,
                    )

        elif isinstance(tool, str):
            from_tool_name = True
            tool_name = tool

            if backend or session_name:
                runtime_session = session_name
                runtime_server = server

                if backend is not None:
                    runtime_backend = backend
                else:
                    if runtime_session not in self._session_info:
                        raise GroundingError(
                            f"Session not found: {runtime_session}",
                            code=ErrorCode.SESSION_NOT_FOUND,
                        )
                    runtime_backend = self._session_info[
                        runtime_session
                    ].backend_type
            else:
                all_tools = await self.list_tools(use_cache=True)
                matching = [t for t in all_tools if t.name == tool_name]

                if not matching:
                    raise GroundingError(
                        f"Tool '{tool_name}' not found",
                        code=ErrorCode.TOOL_NOT_FOUND,
                    )

                if len(matching) > 1:
                    sources = [
                        f"{t.runtime_info.backend.value}/{t.runtime_info.session_name}"
                        for t in matching if t.is_bound
                    ]
                    raise GroundingError(
                        f"Multiple tools named '{tool_name}' found in: {sources}. "
                        f"Please specify 'backend' or 'session_name' parameter.",
                        code=ErrorCode.AMBIGUOUS_TOOL,
                    )

                resolved_tool = matching[0]
                runtime_info = resolved_tool.runtime_info
                runtime_backend = runtime_info.backend
                runtime_session = runtime_info.session_name
                runtime_server = runtime_info.server_name
        else:
            raise TypeError("tool must be a BaseTool instance or tool name string")

        return (
            tool_name,
            params,
            runtime_backend,
            runtime_session,
            runtime_server,
            resolved_tool,
            from_tool_name,
        )

    async def _ensure_invocation_session(
        self,
        runtime_backend: BackendType,
        runtime_session: str | None,
        runtime_server: str | None,
    ) -> str | None:
        if runtime_backend == BackendType.META:
            return runtime_session
        if not runtime_session or runtime_session not in self._sessions:
            return await self.ensure_session(runtime_backend, runtime_server)
        return runtime_session

    @staticmethod
    def _requires_live_provider_tool(tool: BaseTool) -> bool:
        missing = object()
        connector = getattr(tool, "_conn", missing)
        return connector is None and connector is not missing

    def _bind_invocation_runtime(
        self,
        tool: BaseTool,
        runtime_backend: BackendType,
        runtime_session: str | None,
        runtime_server: str | None,
    ) -> None:
        if runtime_backend == BackendType.META or not runtime_session:
            return

        runtime_info = tool.runtime_info if tool.is_bound else None
        if (
            runtime_info is None
            or runtime_info.backend != runtime_backend
            or runtime_info.session_name != runtime_session
            or runtime_info.server_name != runtime_server
            or runtime_info.grounding_client is None
        ):
            tool.bind_runtime_info(
                backend=runtime_backend,
                session_name=runtime_session,
                server_name=runtime_server,
                grounding_client=self,
            )

    async def _resolve_pipeline_tool(
        self,
        *,
        tool_name: str,
        resolved_tool: BaseTool | None,
        from_tool_name: bool,
        runtime_backend: BackendType,
        runtime_session: str | None,
        runtime_server: str | None,
    ) -> BaseTool:
        needs_provider_tool = (
            from_tool_name
            or resolved_tool is None
            or self._requires_live_provider_tool(resolved_tool)
        )

        if not needs_provider_tool:
            self._bind_invocation_runtime(
                resolved_tool,
                runtime_backend,
                runtime_session,
                runtime_server,
            )
            return resolved_tool

        if runtime_backend == BackendType.META:
            candidates = await self.list_tools(backend=runtime_backend, use_cache=False)
        else:
            candidates = await self.list_tools(session_name=runtime_session, use_cache=False)

        pipeline_tool = next((t for t in candidates if t.name == tool_name), None)
        if pipeline_tool is None:
            raise GroundingError(
                f"Tool '{tool_name}' not found",
                code=ErrorCode.TOOL_NOT_FOUND,
            )

        self._bind_invocation_runtime(
            pipeline_tool,
            runtime_backend,
            runtime_session,
            runtime_server,
        )
        return pipeline_tool

    def _make_direct_tool_use_context(
        self,
        tool: BaseTool,
        *,
        backend: BackendType | None = None,
        session_name: str | None = None,
        server: str | None = None,
    ):
        import os

        from openspace.tool_runtime.direct_context import build_direct_tool_use_context

        shell_config = getattr(self._config, "shell", None)
        cwd = (
            self._resolve_direct_invocation_cwd(tool, session_name=session_name)
            or getattr(shell_config, "working_dir", None)
            or os.getcwd()
        )
        state_key = (
            str((backend or getattr(tool, "backend_type", None) or BackendType.NOT_SET).value),
            str(session_name or ""),
            str(server or ""),
        )
        read_file_state = self._direct_read_file_states.setdefault(state_key, {})

        return build_direct_tool_use_context(
            tools=[tool],
            all_tools=[tool],
            model="grounding-client",
            cwd=str(cwd),
            agent_id="grounding-client",
            recording_manager=self._recording_manager,
            quality_manager=self._quality_manager,
            read_file_state=read_file_state,
            tui_available=False,
        )

    def _resolve_direct_invocation_cwd(
        self,
        tool: BaseTool,
        *,
        session_name: str | None = None,
    ) -> str | None:
        candidates: list[Any] = []
        if session_name:
            candidates.append(self._sessions.get(session_name))
        candidates.extend([
            getattr(tool, "_session", None),
            tool,
            getattr(tool, "connector", None),
        ])

        for obj in candidates:
            if obj is None:
                continue
            for attr in (
                "default_working_dir",
                "_default_working_dir",
                "workspace_dir",
                "working_dir",
                "cwd",
            ):
                value = getattr(obj, attr, None)
                if hasattr(value, "__fspath__"):
                    return str(value)
                if isinstance(value, str) and value:
                    return value
        return None

    def _tool_call_result_to_tool_result(self, result) -> ToolResult:
        from openspace.tool_runtime.pipeline.execution import tool_call_result_to_tool_result

        return tool_call_result_to_tool_result(result)

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
        Invoke a tool through the full run_tool_use pipeline.
        """
        (
            tool_name,
            params,
            runtime_backend,
            runtime_session,
            runtime_server,
            resolved_tool,
            from_tool_name,
        ) = await self._resolve_tool_invocation(
            tool,
            parameters,
            backend=backend,
            session_name=session_name,
            server=server,
            kwargs=kwargs,
        )

        if runtime_backend != BackendType.META:
            runtime_session = await self._ensure_invocation_session(
                runtime_backend,
                runtime_session,
                runtime_server,
            )

        try:
            pipeline_tool = await self._resolve_pipeline_tool(
                tool_name=tool_name,
                resolved_tool=resolved_tool,
                from_tool_name=from_tool_name,
                runtime_backend=runtime_backend,
                runtime_session=runtime_session,
                runtime_server=runtime_server,
            )
            context = self._make_direct_tool_use_context(
                pipeline_tool,
                backend=runtime_backend,
                session_name=runtime_session,
                server=runtime_server,
            )
            tool_call = {
                "id": f"call-{time.time_ns()}",
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": params,
                },
            }
            tool_executor = getattr(self, "_tool_executor", None)
            if tool_executor is None:
                tool_executor = get_default_tool_executor()
                self._tool_executor = tool_executor

            tool_call_result = await tool_executor.run_tool_use(
                tool_call,
                {pipeline_tool.name: pipeline_tool},
                context,
            )

            if runtime_backend != BackendType.META and runtime_session and runtime_session in self._session_info:
                async with self._lock:
                    old_info = self._session_info[runtime_session]
                    self._session_info[runtime_session] = old_info.model_copy(
                        update={"last_activity": datetime.utcnow()}
                    )

            return self._tool_call_result_to_tool_result(tool_call_result)
        finally:
            if runtime_backend != BackendType.META and not keep_session and runtime_session:
                if runtime_server or runtime_session.startswith(runtime_backend.value):
                    await self.close_session(runtime_session)
