"""
BaseTool.
All pre-defined grounding atomic operations will inherit this tool class.
RemoteTool needs to pass in connector.
"""
import asyncio, time, inspect
from abc import ABC, abstractmethod
from functools import lru_cache
from typing import Any, ClassVar, Dict, Optional, TYPE_CHECKING
from pydantic import BaseModel, ConfigDict, Field, create_model

from ..types import BackendType, ToolResult, ToolSchema, ToolStatus
from ..exceptions import GroundingError, ErrorCode
from openspace.utils.logging import Logger
import jsonschema

if TYPE_CHECKING:
    from ..grounding_client import GroundingClient

logger = Logger.get_logger(__name__)


class ToolRuntimeInfo:
    """Runtime information for a tool instance"""
    def __init__(
        self,
        backend: BackendType,
        session_name: str,
        server_name: Optional[str] = None,
        grounding_client: Optional['GroundingClient'] = None,
    ):
        self.backend = backend
        self.session_name = session_name
        self.server_name = server_name
        self.grounding_client = grounding_client
    
    def __repr__(self):
        return f"<ToolRuntimeInfo backend={self.backend.value} session={self.session_name}>"
    

class BaseTool(ABC):
    _name: ClassVar[str] = ""
    _description: ClassVar[str] = ""
    backend_type: ClassVar[BackendType] = BackendType.NOT_SET

    def __init__(self,
                 schema: Optional[ToolSchema] = None,
                 *,
                 verbose: bool = False,
                 handle_errors: bool = True) -> None:
        self.verbose = verbose
        self.handle_errors = handle_errors
        self.schema: ToolSchema = schema or ToolSchema(
            name=self._name or self.__class__.__name__.lower(),
            description=self._description,
            parameters=self.get_parameters_schema(),
            backend_type=self.backend_type,
        )
        
        self._runtime_info: Optional[ToolRuntimeInfo] = None
        self._disable_outer_recording = True
    
    @property
    def name(self) -> str:
        """Get tool name from schema (supports both class-defined and runtime-injected names)"""
        return self.schema.name if hasattr(self, 'schema') and self.schema else self._name
    
    @property
    def description(self) -> str:
        """Get tool description from schema (supports both class-defined and runtime-injected descriptions)"""
        return self.schema.description if hasattr(self, 'schema') and self.schema else self._description

    @classmethod
    @lru_cache
    def get_parameters_schema(cls) -> Dict[str, Any]:
        """Auto-generate JSON-schema from _run() or _arun() signature.
        
        Returns empty dict for tools with no parameters.
        Priority: prefer _arun if overridden, otherwise use _run.
        """
        # Priority: prefer _arun if it's overridden by subclass, else use _run
        # This allows async-first tools to define their signature via _arun
        sig_src = None
        
        # Check if _arun is overridden (not from BaseTool)
        if cls._arun is not BaseTool._arun:
            sig_src = cls._arun
        # Otherwise check if _run is overridden
        elif cls._run is not BaseTool._run:
            sig_src = cls._run
        # If neither is overridden, raise error
        else:
            raise ValueError(
                f"{cls.__name__} must implement _run() or _arun() to define its parameters schema"
            )
        
        sig = inspect.signature(sig_src)
        fields: dict[str, Any] = {}
        for name, p in sig.parameters.items():
            # Skip 'self' and **kwargs / *args
            if name == "self" or p.kind in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL):
                continue
            typ = p.annotation if p.annotation is not inspect._empty else str
            default = p.default if p.default is not inspect._empty else ...
            fields[name] = (typ, Field(default))
        
        if not fields:
            return {}
        
        PModel: type[BaseModel] = create_model(
            f"{cls.__name__}Params",
            __config__=ConfigDict(arbitrary_types_allowed=True),
            **fields
        )
        return PModel.model_json_schema()

    def validate_parameters(self, params: Dict[str, Any]) -> None:
        try:
            self.schema.validate_parameters(params, raise_exc=True)
        except jsonschema.ValidationError as ve:
            raise GroundingError(
                f"Invalid parameters: {ve.message}",
                code=ErrorCode.TOOL_EXECUTION_FAIL,
                tool_name=self.schema.name,
            ) from ve

    def run(self, **kwargs):
        try:
            return asyncio.run(self.invoke(**kwargs))
        except RuntimeError:                     # already in running loop
            loop = asyncio.get_running_loop()
            return loop.create_task(self.invoke(**kwargs))

    def __call__(self, **kwargs):
        return self.run(**kwargs)

    async def __acall__(self, **kwargs):
        return await self.arun(**kwargs)
    
    async def arun(self, **kwargs) -> ToolResult:
        start = time.time()
        try:
            self.validate_parameters(kwargs)
            raw = await self._arun(**kwargs)
            result = self._wrap_result(raw, time.time() - start)
        except Exception as e:
            if self.handle_errors:
                result = ToolResult(
                    status=ToolStatus.ERROR,
                    error=str(e),
                    metadata={"tool": self.schema.name},
                )
            else:
                raise
        
        await self._auto_record_execution(kwargs, result, time.time() - start)
        return result

    # to be implemented by subclasses
    @abstractmethod
    async def _arun(self, **kwargs): ...
    
    def bind_runtime_info(
        self,
        backend: BackendType,
        session_name: str,
        server_name: Optional[str] = None,
        grounding_client: Optional['GroundingClient'] = None,
    ) -> 'BaseTool':
        """
        Bind runtime information to the tool instance.
        Allow the tool to be invoked directly without specifying backend/session/server.
        
        Args:
            backend: Backend type
            session_name: Session name
            server_name: Server name (for MCP)
            grounding_client: Optional reference to GroundingClient for direct invocation
        """
        self._runtime_info = ToolRuntimeInfo(
            backend=backend,
            session_name=session_name,
            server_name=server_name,
            grounding_client=grounding_client,
        )
        return self
    
    @property
    def runtime_info(self) -> Optional['ToolRuntimeInfo']:
        """Get runtime information if bound"""
        return self._runtime_info
    
    @property
    def is_bound(self) -> bool:
        """Check if tool has runtime information bound"""
        return self._runtime_info is not None
    
    async def invoke(
        self, 
        parameters: Dict[str, Any] | None = None, 
        keep_session: bool = True,
        **kwargs
    ) -> ToolResult:
        """
        Invoke this tool using bound runtime information.
        Requires runtime info to be bound via bind_runtime_info().
        If no runtime info is bound, the tool will be executed locally.   
        """
        params = parameters or kwargs

        if self.is_bound and self._runtime_info.grounding_client:
            return await self._runtime_info.grounding_client.invoke_tool(
                tool=self,
                parameters=params,
                keep_session=keep_session,
            )

        return await self.arun(**params)

    def _wrap_result(self, obj: Any, elapsed: float) -> ToolResult:
        if isinstance(obj, ToolResult):
            obj.execution_time = elapsed
            return obj
        if self.verbose:
            logger.debug("[%s] done in %.2f s", self.schema.name, elapsed)
        if isinstance(obj, (bytes, bytearray)):
            obj = obj.decode("utf-8", errors="replace")
        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=str(obj),
            execution_time=elapsed,
            metadata={"tool": self.schema.name},
        )
    
    async def _auto_record_execution(
        self,
        parameters: Dict[str, Any],
        result: ToolResult,
        execution_time: float,
    ):
        """Auto-record tool execution to recording manager and quality manager."""
        # Record to quality manager (for quality tracking)
        await self._record_to_quality_manager(result, execution_time * 1000)
        
        # Record to recording manager (for trajectory recording)
        try:
            from openspace.recording import RecordingManager
            
            if not RecordingManager.is_recording():
                return
            
            # Check if tool has disabled outer recording (e.g., GUI agent with intermediate steps)
            if hasattr(self, '_disable_outer_recording') and self._disable_outer_recording:
                logger.debug(f"Skipping outer recording for {self.schema.name} (intermediate steps recorded)")
                return
            
            # Get backend and server_name from runtime_info (if bound)
            backend = self.backend_type.value
            server_name = None
            
            if self.is_bound and self._runtime_info:
                # Prefer runtime_info information (more accurate)
                backend = self._runtime_info.backend.value
                server_name = self._runtime_info.server_name
            
            # Get screenshot (if GUI backend)
            screenshot = None
            if self.backend_type == BackendType.GUI and hasattr(self, 'connector'):
                try:
                    screenshot = await self.connector.get_screenshot()
                except Exception as e:
                    logger.debug(f"Failed to capture screenshot: {e}")
            
            # Record tool execution with complete runtime information
            await RecordingManager.record_tool_execution(
                tool_name=self.schema.name,
                backend=backend,
                parameters=parameters,
                result=result.content,
                server_name=server_name,
                is_success=result.is_success,  # Pass actual success status from ToolResult
            )
        except Exception as e:
            logger.warning(f"Failed to auto-record tool execution for {self.schema.name}: {e}")
    
    async def _record_to_quality_manager(
        self,
        result: ToolResult,
        execution_time_ms: float,
    ):
        """Record execution result to quality manager for quality tracking."""
        try:
            from openspace.grounding.core.quality import get_quality_manager
            
            manager = get_quality_manager()
            if manager:
                await manager.record_execution(self, result, execution_time_ms)
        except Exception as e:
            # Quality recording failure should not affect tool execution
            logger.debug(f"Failed to record to quality manager: {e}")

    # keep _run for backward-compatibility / thread-pool fallback
    def _run(self, **kwargs):
        raise NotImplementedError

    def __repr__(self):
        base = f"<Tool {self.schema.name} ({self.backend_type.value})"
        if self.is_bound:
            base += f" @ {self._runtime_info.session_name}"
        return base + ">"

    def __init_subclass__(cls, **kwargs):
        """
        - at least implement _run or _arun
        - backend_type is NOT_SET, only give a warning, allow RemoteTool to inject at runtime
        """
        super().__init_subclass__(**kwargs)

        if cls._arun is BaseTool._arun and cls._run is BaseTool._run:
            raise ValueError(f"{cls.__name__} must implement _run() or _arun()")

        if cls.backend_type is BackendType.NOT_SET:
            logger.debug(
                "%s.backend_type is NOT_SET; remember to override or set at runtime.",
                cls.__name__,
            )