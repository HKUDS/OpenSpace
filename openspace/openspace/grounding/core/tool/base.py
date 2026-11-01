"""
BaseTool — abstract base for all grounding tool implementations.

Tool contract note (Implementation: Tool.ts, os: this file):
  OpenSpace ``Tool`` is a structural type with 47 members.  The subset relevant to
  the engine (not React UI) is migrated here.  See DIFFERENCES section at the
  bottom for the exhaustive mapping.
"""
import asyncio
import inspect
import math
import os
import time
from abc import ABC, abstractmethod
from functools import lru_cache
from typing import Any, ClassVar, Dict, List, Optional, Union, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, create_model

from ..types import BackendType, ToolResult, ToolSchema, ToolStatus
from ..exceptions import GroundingError, ErrorCode
from openspace.utils.logging import Logger
import jsonschema

if TYPE_CHECKING:
    from ..grounding_client import GroundingClient

logger = Logger.get_logger(__name__)


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        logger.warning("Invalid %s=%r; using default %s", name, raw, default)
        return default


DEFAULT_MAX_RESULT_SIZE_CHARS: int = _env_int(
    "OPENSPACE_DEFAULT_MAX_RESULT_SIZE_CHARS",
    50_000,
    minimum=1_000,
)
"""Per-tool default ceiling for result content before persistence to disk.

Identical to OpenSpace ``DEFAULT_MAX_RESULT_SIZE_CHARS`` in ``constants/toolLimits.ts``.
When a tool result exceeds this, the full content is written to disk and the
model receives a ``<persisted-output>`` preview with a file path.
Individual tools may declare a *lower* ``max_result_size_chars`` (e.g.
BashTool 30 000, GrepTool 20 000); the effective threshold is always
``min(declared, DEFAULT_MAX_RESULT_SIZE_CHARS)`` — matching OpenSpace's
``getPersistenceThreshold()``.
"""

TOOL_RESULT_NO_LIMIT: float = math.inf
"""Sentinel for ``max_result_size_chars`` — never persist/truncate result.

Equivalent to OpenSpace ``maxResultSizeChars: Infinity`` (e.g. FileReadTool, which
self-limits via its own offset/limit parameters and where persisting creates
a circular Read → file → Read loop).
"""


# ---------------------------------------------------------------------------
# Permission types
# ---------------------------------------------------------------------------
#
# As of permission engine, the full OpenSpace-aligned permission union lives in
# :mod:`openspace.grounding.core.permissions.types`.  The symbols below are
# re-exported from there to preserve the legacy ``from base import
# PermissionCheckResult, PERMISSION_ALLOW`` import path.  Call sites should
# prefer importing from :mod:`openspace.grounding.core.permissions` directly
# and checking ``behavior`` with ``isinstance`` or the literal string.
from openspace.grounding.core.permissions.types import (  # noqa: E402
    PermissionAllow as PermissionAllow,
    PermissionAsk as PermissionAsk,
    PermissionDeny as PermissionDeny,
    PermissionPassthrough as PermissionPassthrough,
    PermissionResult as PermissionResult,
    PermissionCheckResult as PermissionCheckResult,
    PERMISSION_ALLOW as PERMISSION_ALLOW,
)


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
    """Abstract base for every tool in the Grounding framework.

    OpenSpace ``Tool`` type equivalence (Implementation: ``Tool.ts``, ``buildTool``):

    Input-dependent methods (OpenSpace functions, override for dynamic tools):
      ``is_read_only(input)``, ``is_concurrency_safe(input)``,
      ``is_destructive(input)`` — default returns ``_is_read_only`` /
      ``_is_concurrency_safe`` / ``_is_destructive`` class booleans.

    Class-level attributes (set at class definition):
      ``should_defer``, ``always_load``, ``search_hint``,
      ``max_result_size_chars``, ``aliases``

    Derived property:
      ``is_deferred`` — ``True`` when deferred from initial prompt.

    Methods:
      ``check_permissions(input, ctx)`` — tool-specific permission check.
    """

    # --- identity (ClassVar, immutable per-class) --------------------------
    _name: ClassVar[str] = ""
    _description: ClassVar[str] = ""
    backend_type: ClassVar[BackendType] = BackendType.NOT_SET

    # --- OpenSpace-aligned tool properties (DEC-004) ------------------------------
    #
    # OpenSpace ``isReadOnly(input)``, ``isConcurrencySafe(input)``, and
    # ``isDestructive(input)`` are *functions* that receive the tool input.
    # In os they are implemented as **overridable methods** that default to
    # returning the ``_is_read_only`` / ``_is_concurrency_safe`` /
    # ``_is_destructive`` class-level booleans.  Simple tools set the class
    # attribute; dynamic tools (e.g. BashTool) override the method.

    _is_read_only: bool = False
    """Internal default for ``is_read_only()``.  Set at class level for
    simple tools (e.g. ``_is_read_only = True`` on ReadFileTool)."""

    _is_concurrency_safe: bool = False
    """Internal default for ``is_concurrency_safe()``."""

    _is_destructive: bool = False
    """Internal default for ``is_destructive()``."""

    should_defer: bool = False
    """OpenSpace ``shouldDefer``.  When True and ``always_load`` is False, the tool is
    deferred (schema not included in the initial prompt).
    May be set per-instance by ``bind_runtime_info`` for MCP/GUI backends."""

    always_load: bool = False
    """OpenSpace ``alwaysLoad``.  When True, tool is never deferred regardless of
    ``should_defer``.  Set via MCP ``_meta['anthropic/alwaysLoad']`` or user
    configuration.  Consumed by: ``is_deferred`` property."""

    search_hint: str = ""
    """OpenSpace ``searchHint``.  3-10 word phrase for keyword matching in
    ToolSearch.  Prefer terms not already in the tool name.
    Consumed by lightweight tool keyword search scoring (+4 points)."""

    max_result_size_chars: Union[int, float] = DEFAULT_MAX_RESULT_SIZE_CHARS
    """OpenSpace ``maxResultSizeChars``.  Per-tool persistence threshold.
    Effective threshold = ``min(this, DEFAULT_MAX_RESULT_SIZE_CHARS)``.
    Set to ``TOOL_RESULT_NO_LIMIT`` (``math.inf``) for tools whose output
    must never be persisted (e.g. ReadFileTool).
    Consumed by the ``run_tool_use()`` execution pipeline."""

    aliases: List[str] = []
    """OpenSpace ``aliases``.  Alternative names for backward-compatible tool lookup.
    Consumed by: tool-by-name resolution in GroundingClient (step 11.1)."""

    parameter_descriptions: ClassVar[Dict[str, str]] = {}
    """Optional model-facing parameter descriptions.

    Python signatures provide names, types and defaults, but not the richer
    ``zod.describe(...)`` text that OpenSpace includes in tool schemas.  Tools can set
    this mapping at class level to annotate generated JSON Schema properties.
    """

    requires_user_interaction: bool = False
    """OpenSpace ``requiresUserInteraction`` (permission engine).

    When ``True`` the tool **must** surface a user prompt even in
    ``bypassPermissions`` / ``acceptEdits`` modes — the engine treats such
    tools as intrinsically interactive (e.g. ``ask_user_question``) and
    cannot auto-approve them.  Also used by the tool execution pipeline
    to mark the tool call as ``should_defer`` so the agent loop waits for
    the user response before proceeding.

    Consumed by:
      - ``openspace.grounding.core.permissions.engine.has_permissions_to_use_tool``
      - ``tool_execution._resolve_permissions`` (step 5.1)
    """

    def __init__(
        self,
        schema: Optional[ToolSchema] = None,
        *,
        verbose: bool = False,
        handle_errors: bool = True,
    ) -> None:
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
        self._should_defer_override: Optional[bool] = None
    
    # --- derived properties --------------------------------------------------

    @property
    def is_deferred(self) -> bool:
        """Whether this tool should be deferred from the initial prompt.

        Implementation: ``isDeferredTool(tool)`` in ``ToolSearchTool/prompt.ts``.

        Resolution order:
        1. ``always_load == True``  →  never deferred
        2. ``_should_defer_override`` (set by ``bind_runtime_info``)
        3. class-level ``should_defer``
        """
        if self.always_load:
            return False
        if self._should_defer_override is not None:
            return self._should_defer_override
        return self.should_defer

    @property
    def name(self) -> str:
        """Get tool name from schema (supports both class-defined and runtime-injected names)"""
        return self.schema.name if hasattr(self, 'schema') and self.schema else self._name

    @property
    def description(self) -> str:
        """Get tool description from schema (supports both class-defined and runtime-injected descriptions)"""
        return self.schema.description if hasattr(self, 'schema') and self.schema else self._description

    def get_prompt(self, context: Any = None) -> str:
        """Return the model-facing tool prompt.

        OpenSpace separates short ``description(input, opts)`` text from the longer
        ``prompt(opts)`` used as the API tool description.  OS keeps
        ``schema.description`` as the short fallback and lets tools override
        this method when they have richer instructions.
        """
        return self.description or ""

    # --- input-dependent tool property methods ----------------
    # Each method receives the tool input and returns a bool. The default
    # implementation returns the class-level ``_is_*`` attribute; dynamic tools
    # override the method itself.

    def is_read_only(self, input: Optional[Dict[str, Any]] = None) -> bool:
        """OpenSpace ``isReadOnly(input)``.  Default ``False`` (assume writes).

        Override for tools with dynamic read-only semantics.  OpenSpace example::

            // BashTool
            isReadOnly(input) {
                return checkReadOnlyConstraints(input).behavior === 'allow';
            }

        Consumed by: explore agent tool filter, plan mode tool pruning
        (step 12), ``partition_tool_calls`` (step 5.2).
        """
        return self._is_read_only

    def is_concurrency_safe(self, input: Optional[Dict[str, Any]] = None) -> bool:
        """OpenSpace ``isConcurrencySafe(input)``.  Default ``False`` (assume unsafe).

        Called by ``partition_tool_calls`` (step 5.2) to decide whether a
        specific tool call can run in parallel.  OpenSpace example::

            // BashTool — concurrency-safe iff read-only
            isConcurrencySafe(input) {
                return this.isReadOnly?.(input) ?? false;
            }
        """
        return self._is_concurrency_safe

    def is_destructive(self, input: Optional[Dict[str, Any]] = None) -> bool:
        """OpenSpace ``isDestructive(input)``.  Default ``False``.

        Only ``True`` for irreversible operations (delete, overwrite, send).
        Consumed by: permission system destructive-command warnings (permission engine).
        """
        return self._is_destructive

    # --- input validation ---------------------------------------------------

    async def validate_input(
        self,
        input: Dict[str, Any],
        context: Any = None,
    ) -> Optional[str]:
        """Tool-specific input validation beyond JSON Schema.

        Implementation: ``tool.validateInput(input, context)``.

        Called by the tool execution pipeline (step 5.1) *after* JSON Schema
        validation passes.  Returns ``None`` if valid, or an error message
        string if the input should be rejected.

        OpenSpace examples:
        - BashTool: blocks ``sleep``/``wait``/``monitor`` commands
        - FileEditTool: validates ``old_string`` uniqueness, file existence
        - GrepTool: validates regex syntax
        - WebFetchTool: validates URL format

        Override in subclasses.  Steps 8.x will add specific implementations.
        """
        return None

    # --- permission check --------------------------------------------------

    async def check_permissions(
        self,
        input: Dict[str, Any],
        context: Any = None,
    ) -> PermissionResult:
        """Tool-specific permission check.

        Implementation: ``tool.checkPermissions(input, context)``.

        The default implementation allows everything — matching OpenSpace's
        ``buildTool`` default::

            checkPermissions: (input) =>
                Promise.resolve({ behavior: 'allow', updatedInput: input })

        Override in subclasses to add tool-specific checks (e.g. BashTool
        delegates to ``bash_tool_has_permission``).  ``context`` is a
        :class:`openspace.services.tooling.context.ToolUseContext` once
        the tool execution pipeline (step 5.1) is wired up; permission engine adds
        ``context.permission_context`` carrying the
        :class:`openspace.grounding.core.permissions.ToolPermissionContext`.
        """
        return PermissionAllow(updated_input=input)

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
        schema = PModel.model_json_schema()
        descriptions = getattr(cls, "parameter_descriptions", {}) or {}
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for param_name, description in descriptions.items():
                prop = properties.get(param_name)
                if prop is None:
                    prop = {}
                    properties[param_name] = prop
                if isinstance(prop, dict) and description:
                    prop.setdefault("description", description)
        return schema

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
        return await self.invoke(**kwargs)

    async def _execute_raw(self, **kwargs) -> ToolResult:
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
        """Bind runtime information to the tool instance.

        Also applies **auto-defer** logic (DEC-004 option B):
        If ``should_defer`` is still the class default ``False`` and
        ``always_load`` is ``False``, MCP and GUI backend tools are
        automatically deferred so their schemas don't bloat the initial
        prompt.  This mirrors OpenSpace's ``isDeferredTool`` which returns ``True``
        for all MCP tools.
        """
        self._runtime_info = ToolRuntimeInfo(
            backend=backend,
            session_name=session_name,
            server_name=server_name,
            grounding_client=grounding_client,
        )

        # Auto-defer MCP/GUI tools (Implementation: isDeferredTool returns true for isMcp)
        if not self.always_load and not self.should_defer:
            if backend in (BackendType.MCP, BackendType.GUI):
                self._should_defer_override = True

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
        parameters: Optional[Dict[str, Any]] = None, 
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

        from openspace.tool_runtime.direct_context import build_direct_tool_use_context
        from openspace.tool_runtime.pipeline.execution import (
            run_tool_use,
            tool_call_result_to_tool_result,
        )

        tool_call = {
            "id": f"direct-tool-{time.time_ns()}",
            "type": "function",
            "function": {"name": self.name, "arguments": params},
        }
        context = build_direct_tool_use_context(
            tools=[self],
            all_tools=[self],
            model="base-tool",
            agent_id=f"tool:{self.name}",
            tui_available=False,
        )
        result = await run_tool_use(tool_call, {self.name: self}, context)
        return tool_call_result_to_tool_result(result)

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
        """Auto-record legacy trajectory data for raw tool execution.

        Quality tracking is owned by the PostToolUse ``quality_tracking`` hook
        in the migrated ``run_tool_use()`` pipeline.  Public tool calls route
        through that pipeline before reaching this raw executor.
        """
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

    # keep _run for backward-compatibility / thread-pool fallback
    def _run(self, **kwargs):
        raise NotImplementedError

    def __repr__(self):
        base = f"<Tool {self.schema.name} ({self.backend_type.value})"
        if self.is_bound:
            base += f" @ {self._runtime_info.session_name}"
        return base + ">"

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

        if cls._arun is BaseTool._arun and cls._run is BaseTool._run:
            raise ValueError(f"{cls.__name__} must implement _run() or _arun()")

        if cls.backend_type is BackendType.NOT_SET:
            logger.debug(
                "%s.backend_type is NOT_SET; remember to override or set at runtime.",
                cls.__name__,
            )
