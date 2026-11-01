from __future__ import annotations

import asyncio
import contextvars
import inspect
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:
    from openspace.core.tui_bridge import TUIBridge
    from openspace.persistence import SessionStorage
    from openspace.services.runtime_support.cost import CostTracker

from openspace.protocol import StreamEvent
from openspace.runtime import (
    ExecutionRequest,
    ExecutionResult,
    OpenSpaceRuntime,
    RuntimeEventBus,
)
from openspace.llm.effort import (
    convert_effort_value_to_level,
    parse_effort_value,
)
from openspace.services.lsp import diagnostic_tracker
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

_BRIDGE_DISPATCH_SUPPRESSED: contextvars.ContextVar[bool] = (
    contextvars.ContextVar("openspace_bridge_dispatch_suppressed", default=False)
)

def _configure_logging_from_config(config: "OpenSpaceConfig") -> None:
    log_to_file = config.log_file_path or "auto" if config.log_to_file else None
    Logger.configure(
        level=config.log_level,
        log_to_console=config.log_to_console,
        log_to_file=log_to_file,
        force=True,
        attach_to_root=True,
    )


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _format_manual_dream_result(result: Any) -> str:
    files = list(getattr(result, "files_touched", []) or [])
    sessions = int(getattr(result, "sessions_reviewed", 0) or 0)
    turns = int(getattr(result, "turn_count", 0) or 0)
    if not files:
        return (
            f"Dream completed. Reviewed {sessions} session(s) in {turns} turn(s); "
            "no memory files needed changes."
        )
    rendered = "\n".join(f"- {path}" for path in files)
    return (
        f"Dream completed. Reviewed {sessions} session(s) in {turns} turn(s).\n"
        f"Improved memory files:\n{rendered}"
    )


def _format_manual_dream_skip(reason: str | None) -> str:
    messages = {
        "subagent": "Dream can only run from the main agent.",
        "auto_memory_disabled": "Auto memory is disabled.",
        "missing_llm_client": "LLM client is not available.",
        "missing_tools": "No tools are available for memory consolidation.",
        "lock_busy": "A memory dream is already running for this memory directory.",
        "read_last_failed": "Could not read the memory consolidation lock.",
        "session_scan_failed": "Could not scan session transcripts for dream context.",
        "no_daily_log_entries": "No unconsolidated daily memory log entries were found.",
    }
    return messages.get(reason or "", f"Dream skipped: {reason or 'unknown reason'}.")


def _format_manual_summary_result(result: Any) -> str:
    memory_path = getattr(result, "memory_path", None)
    turn_count = int(getattr(result, "turn_count", 0) or 0)
    edited = bool(getattr(result, "edited", False))
    path_text = f" at {memory_path}" if memory_path else ""
    if edited:
        return f"Session memory updated{path_text} in {turn_count} turn(s)."
    return f"Session memory checked{path_text}; no changes were needed."


def _format_manual_summary_skip(reason: str | None) -> str:
    messages = {
        "no_active_session": "No active session to summarize.",
        "no_messages": "No messages to summarize.",
        "missing_llm_client": "LLM client is not available.",
        "missing_tools": "No edit tool is available for session-memory extraction.",
        "disabled": "Session memory is disabled or unavailable for this session.",
        "coalesced": "A session-memory extraction is already running; queued the latest context.",
        "missing_session": "No active session to summarize.",
    }
    return messages.get(reason or "", f"Summary skipped: {reason or 'unknown reason'}.")


class _EventDispatcherProxy:
    """Bridge-compatible proxy that routes events through OpenSpace."""

    def __init__(self, dispatch: Callable[[str, Dict[str, Any]], Any]) -> None:
        self._dispatch = dispatch

    async def send(self, event_type: str, data: Dict[str, Any]) -> None:
        result = self._dispatch(event_type, data)
        if inspect.isawaitable(result):
            await result


@dataclass
class OpenSpaceConfig:
    # LLM Configuration
    llm_model: str = "openrouter/anthropic/claude-sonnet-4.5"
    llm_enable_thinking: bool = False
    llm_timeout: float = 120.0
    llm_max_retries: int = 3
    llm_rate_limit_delay: float = 0.0
    llm_kwargs: Dict[str, Any] = field(default_factory=dict)
    
    # Separate models for specific tasks (None = use llm_model)
    tool_retrieval_model: Optional[str] = None  # Model for tool retrieval LLM filter
    
    # Skill Engine Models — names map to class names (None = use llm_model)
    skill_registry_model: Optional[str] = None        # SkillRegistry: skill selection
    execution_analyzer_model: Optional[str] = None    # ExecutionAnalyzer: post-execution analysis
    execution_analyzer_max_tokens: Optional[int] = None  # None = inherit LLM client default
    skill_evolver_model: Optional[str] = None         # SkillEvolver: skill evolution
    skill_evolver_max_tokens: Optional[int] = None    # None = inherit LLM client default
    
    # Grounding Configuration
    grounding_config_path: Optional[str] = None
    grounding_max_iterations: int = 20
    grounding_system_prompt: Optional[str] = None
    
    # Backend Configuration
    backend_scope: Optional[List[str]] = None  # None = all backends ["shell", "gui", "mcp", "web", "meta"]
    use_clawwork_productivity: bool = False  # If True, add ClawWork productivity tools (web_search, create_file, etc.) for fair comparison with ClawWork; requires livebench installed.
    
    # Workspace Configuration
    workspace_dir: Optional[str] = None
    capture_skill_dir: Optional[str] = None
    session_storage_dir: Optional[str] = None
    
    # Recording Configuration
    enable_recording: bool = True
    recording_backends: Optional[List[str]] = None
    recording_log_dir: str = "./logs/recordings"
    enable_screenshot: bool = False
    enable_video: bool = False
    enable_conversation_log: bool = True  # Save LLM conversations to conversations.jsonl
    post_execution_mode: str = "inline"  # inline | background | disabled
    post_execution_timeout_s: float = 0.0
    memory_drain_timeout_s: Optional[float] = None

    # Low-latency runtime controls.
    capability_profile: str = "batch_full"
    low_latency_enabled: bool = False
    low_latency_profiler_only: bool = True
    hard_active_tool_limit: int = 500
    max_result_size_chars: Optional[int] = None
    max_tool_results_per_message_chars: Optional[int] = None
    active_tool_names: Optional[List[str]] = None
    policy_deferred_tool_names: Optional[List[str]] = None
    tool_retrieval_query: Optional[str] = None
    skills_disabled: bool = False
    memory_mode: Optional[str] = None
    fast_tool_policy_enabled: bool = False
    disable_fast_auto_preselection: bool = False
    disable_turn0_llm_skill_selector: bool = False
    disable_fast_skill_body_ranking: bool = False
    skill_metadata_only_discovery: bool = False
    tool_schema_cache_telemetry: bool = True
    lsp_sync_start: bool = True
    scheduler_sync_start: bool = True
    scheduler_execute_sync_start: bool = True
    skill_store_sync_start: bool = True
    execution_analysis_sync_start: bool = True
    warm_core: Any | None = None
    
    # Skill Evolution
    evolution_max_concurrent: int = 3        # Max parallel evolutions per trigger
    evolution_storage_root: Optional[str] = None
    skill_store_db_path: Optional[str] = None
    evidence_db_path: Optional[str] = None
    evolution_evidence_enabled: bool = True
    evolution_triggers_enabled: bool = True
    evolution_engine_enabled: bool = True
    evolution_mode: str = "autonomous"  # audit_only | fix_only | autonomous
    evolution_allow_single_observation_capture: bool = True
    skill_trust_promotion_min_independent_successes: int = 2
    evolution_final_drain_limit: int = 0
    evolution_final_drain_rounds: int = 1
    evolution_final_drain_timeout_s: float = 0.0
    evolution_startup_retryable_drain_limit: int = 0
    evolution_startup_retryable_drain_rounds: int = 1
    evolution_startup_retryable_drain_timeout_s: float = 0.0
    evolution_startup_retryable_drain_statuses: str = "failed_retryable"
    evolution_recovery_stale_job_timeout_s: float = 30 * 60
    evolution_behavior_eval_max_revisions: int = 2
    evolution_capture_semantic_validation_enabled: bool = True
    evolution_capture_semantic_validation_model: Optional[str] = None
    evolution_capture_semantic_validation_max_tokens: int = 2048
    evolution_routing_eval_enabled: bool = True
    evolution_routing_eval_required: bool = False
    evolution_behavior_eval_require_replay_runner: bool = True
    evolution_replay_command: Optional[str] = None
    evolution_replay_docker_image: Optional[str] = None
    evolution_replay_timeout_s: float = 600.0
    quality_signal_detector_enabled: bool = True
    quality_signal_trigger_enabled: bool = True
    quality_signal_reconciliation_enabled: bool = True
    
    # Logging Configuration
    log_level: str = "INFO"
    log_to_console: bool = True
    log_to_file: bool = False
    log_file_path: Optional[str] = None
    
    def __post_init__(self):
        """Validate configuration"""
        from openspace.workspace import resolve_workspace_dir

        self.workspace_dir = resolve_workspace_dir(self.workspace_dir)
        if self.workspace_dir:
            recording_dir = Path(self.recording_log_dir).expanduser()
            if not recording_dir.is_absolute():
                recording_dir = Path(self.workspace_dir) / recording_dir
            self.recording_log_dir = str(recording_dir.resolve())

        env_analyzer_max_tokens = os.environ.get(
            "OPENSPACE_EXECUTION_ANALYZER_MAX_TOKENS"
        )
        if env_analyzer_max_tokens is not None:
            try:
                self.execution_analyzer_max_tokens = int(env_analyzer_max_tokens)
            except ValueError:
                raise ValueError(
                    "OPENSPACE_EXECUTION_ANALYZER_MAX_TOKENS must be an integer"
                ) from None
        if self.execution_analyzer_max_tokens is not None:
            self.execution_analyzer_max_tokens = max(
                1,
                int(self.execution_analyzer_max_tokens),
            )

        env_evolver_max_tokens = os.environ.get(
            "OPENSPACE_SKILL_EVOLVER_MAX_TOKENS"
        )
        if env_evolver_max_tokens is not None:
            try:
                self.skill_evolver_max_tokens = int(env_evolver_max_tokens)
            except ValueError:
                raise ValueError(
                    "OPENSPACE_SKILL_EVOLVER_MAX_TOKENS must be an integer"
                ) from None
        if self.skill_evolver_max_tokens is not None:
            self.skill_evolver_max_tokens = max(
                1,
                int(self.skill_evolver_max_tokens),
            )

        env_capture_semantic_tokens = os.environ.get(
            "OPENSPACE_EVOLUTION_CAPTURE_SEMANTIC_VALIDATION_MAX_TOKENS"
        )
        self.evolution_capture_semantic_validation_enabled = _env_bool(
            "OPENSPACE_EVOLUTION_CAPTURE_SEMANTIC_VALIDATION_ENABLED",
            self.evolution_capture_semantic_validation_enabled,
        )
        if env_capture_semantic_tokens is not None:
            try:
                self.evolution_capture_semantic_validation_max_tokens = int(
                    env_capture_semantic_tokens
                )
            except ValueError:
                raise ValueError(
                    "OPENSPACE_EVOLUTION_CAPTURE_SEMANTIC_VALIDATION_MAX_TOKENS "
                    "must be an integer"
                ) from None
        self.evolution_capture_semantic_validation_max_tokens = max(
            256,
            int(self.evolution_capture_semantic_validation_max_tokens),
        )

        env_capture_skill_dir = os.environ.get("OPENSPACE_CAPTURE_SKILL_DIR")
        self.capture_skill_dir = self.capture_skill_dir or env_capture_skill_dir
        if self.capture_skill_dir is not None:
            capture_skill_dir = str(self.capture_skill_dir).strip()
            self.capture_skill_dir = capture_skill_dir or None

        env_post_execution_timeout = os.environ.get("OPENSPACE_POST_EXECUTION_TIMEOUT_S")
        if env_post_execution_timeout is not None:
            try:
                self.post_execution_timeout_s = float(env_post_execution_timeout)
            except ValueError:
                raise ValueError(
                    "OPENSPACE_POST_EXECUTION_TIMEOUT_S must be a number"
                ) from None
        self.post_execution_timeout_s = max(
            0.0,
            float(self.post_execution_timeout_s or 0.0),
        )
        env_max_result_size = os.environ.get("OPENSPACE_DEFAULT_MAX_RESULT_SIZE_CHARS")
        if env_max_result_size is not None:
            try:
                self.max_result_size_chars = int(env_max_result_size)
            except ValueError:
                raise ValueError(
                    "OPENSPACE_DEFAULT_MAX_RESULT_SIZE_CHARS must be an integer"
                ) from None
        if self.max_result_size_chars is not None:
            self.max_result_size_chars = max(1, int(self.max_result_size_chars))
        env_aggregate_tool_results = os.environ.get(
            "OPENSPACE_MAX_TOOL_RESULTS_PER_MESSAGE_CHARS"
        )
        if env_aggregate_tool_results is not None:
            try:
                self.max_tool_results_per_message_chars = int(
                    env_aggregate_tool_results
                )
            except ValueError:
                raise ValueError(
                    "OPENSPACE_MAX_TOOL_RESULTS_PER_MESSAGE_CHARS must be an integer"
                ) from None
        if self.max_tool_results_per_message_chars is not None:
            self.max_tool_results_per_message_chars = max(
                1,
                int(self.max_tool_results_per_message_chars),
            )

        self.evolution_storage_root = (
            self.evolution_storage_root
            or os.environ.get("OPENSPACE_EVOLUTION_STORAGE_ROOT")
        )
        self.skill_store_db_path = (
            self.skill_store_db_path
            or os.environ.get("OPENSPACE_SKILL_STORE_DB_PATH")
        )
        self.evidence_db_path = (
            self.evidence_db_path
            or os.environ.get("OPENSPACE_EVOLUTION_EVIDENCE_DB_PATH")
        )
        self.evolution_evidence_enabled = _env_bool(
            "OPENSPACE_EVOLUTION_EVIDENCE_ENABLED",
            self.evolution_evidence_enabled,
        )
        self.evolution_triggers_enabled = _env_bool(
            "OPENSPACE_EVOLUTION_TRIGGERS_ENABLED",
            self.evolution_triggers_enabled,
        )
        self.quality_signal_detector_enabled = _env_bool(
            "OPENSPACE_QUALITY_SIGNAL_DETECTOR_ENABLED",
            self.quality_signal_detector_enabled,
        )
        self.quality_signal_trigger_enabled = _env_bool(
            "OPENSPACE_QUALITY_SIGNAL_TRIGGER_ENABLED",
            self.quality_signal_trigger_enabled,
        )
        self.quality_signal_reconciliation_enabled = _env_bool(
            "OPENSPACE_QUALITY_SIGNAL_RECONCILIATION_ENABLED",
            self.quality_signal_reconciliation_enabled,
        )
        self.evolution_engine_enabled = _env_bool(
            "OPENSPACE_EVOLUTION_ENGINE_ENABLED",
            self.evolution_engine_enabled,
        )
        self.evolution_allow_single_observation_capture = _env_bool(
            "OPENSPACE_EVOLUTION_ALLOW_SINGLE_OBSERVATION_CAPTURE",
            self.evolution_allow_single_observation_capture,
        )
        env_trust_successes = os.environ.get(
            "OPENSPACE_SKILL_TRUST_PROMOTION_MIN_INDEPENDENT_SUCCESSES"
        )
        if env_trust_successes is not None:
            try:
                self.skill_trust_promotion_min_independent_successes = int(
                    env_trust_successes
                )
            except ValueError:
                raise ValueError(
                    "OPENSPACE_SKILL_TRUST_PROMOTION_MIN_INDEPENDENT_SUCCESSES "
                    "must be an integer"
                ) from None
        self.skill_trust_promotion_min_independent_successes = max(
            1,
            int(self.skill_trust_promotion_min_independent_successes),
        )
        env_evolution_mode = os.environ.get("OPENSPACE_EVOLUTION_MODE")
        if env_evolution_mode:
            self.evolution_mode = env_evolution_mode
        self.evolution_mode = self.evolution_mode.strip().lower()
        if self.evolution_mode not in {"audit_only", "fix_only", "autonomous"}:
            raise ValueError(
                "evolution_mode must be one of: audit_only, fix_only, autonomous"
            )
        env_final_drain_limit = os.environ.get("OPENSPACE_EVOLUTION_FINAL_DRAIN_LIMIT")
        if env_final_drain_limit is not None:
            try:
                self.evolution_final_drain_limit = int(env_final_drain_limit)
            except ValueError:
                raise ValueError(
                    "OPENSPACE_EVOLUTION_FINAL_DRAIN_LIMIT must be an integer"
                ) from None
        self.evolution_final_drain_limit = max(
            0,
            int(self.evolution_final_drain_limit),
        )
        env_final_drain_rounds = os.environ.get("OPENSPACE_EVOLUTION_FINAL_DRAIN_ROUNDS")
        if env_final_drain_rounds is not None:
            try:
                self.evolution_final_drain_rounds = int(env_final_drain_rounds)
            except ValueError:
                raise ValueError(
                    "OPENSPACE_EVOLUTION_FINAL_DRAIN_ROUNDS must be an integer"
                ) from None
        self.evolution_final_drain_rounds = max(
            0,
            int(self.evolution_final_drain_rounds),
        )
        env_final_drain_timeout = os.environ.get(
            "OPENSPACE_EVOLUTION_FINAL_DRAIN_TIMEOUT_S"
        )
        if env_final_drain_timeout is not None:
            try:
                self.evolution_final_drain_timeout_s = float(env_final_drain_timeout)
            except ValueError:
                raise ValueError(
                    "OPENSPACE_EVOLUTION_FINAL_DRAIN_TIMEOUT_S must be a number"
                ) from None
        self.evolution_final_drain_timeout_s = max(
            0.0,
            float(self.evolution_final_drain_timeout_s),
        )
        env_startup_drain_limit = os.environ.get(
            "OPENSPACE_EVOLUTION_STARTUP_RETRYABLE_DRAIN_LIMIT"
        )
        if env_startup_drain_limit is not None:
            try:
                self.evolution_startup_retryable_drain_limit = int(
                    env_startup_drain_limit
                )
            except ValueError:
                raise ValueError(
                    "OPENSPACE_EVOLUTION_STARTUP_RETRYABLE_DRAIN_LIMIT "
                    "must be an integer"
                ) from None
        self.evolution_startup_retryable_drain_limit = max(
            0,
            int(self.evolution_startup_retryable_drain_limit),
        )
        env_startup_drain_rounds = os.environ.get(
            "OPENSPACE_EVOLUTION_STARTUP_RETRYABLE_DRAIN_ROUNDS"
        )
        if env_startup_drain_rounds is not None:
            try:
                self.evolution_startup_retryable_drain_rounds = int(
                    env_startup_drain_rounds
                )
            except ValueError:
                raise ValueError(
                    "OPENSPACE_EVOLUTION_STARTUP_RETRYABLE_DRAIN_ROUNDS "
                    "must be an integer"
                ) from None
        self.evolution_startup_retryable_drain_rounds = max(
            0,
            int(self.evolution_startup_retryable_drain_rounds),
        )
        env_startup_drain_timeout = os.environ.get(
            "OPENSPACE_EVOLUTION_STARTUP_RETRYABLE_DRAIN_TIMEOUT_S"
        )
        if env_startup_drain_timeout is not None:
            try:
                self.evolution_startup_retryable_drain_timeout_s = float(
                    env_startup_drain_timeout
                )
            except ValueError:
                raise ValueError(
                    "OPENSPACE_EVOLUTION_STARTUP_RETRYABLE_DRAIN_TIMEOUT_S "
                    "must be a number"
                ) from None
        self.evolution_startup_retryable_drain_timeout_s = max(
            0.0,
            float(self.evolution_startup_retryable_drain_timeout_s),
        )
        env_startup_drain_statuses = os.environ.get(
            "OPENSPACE_EVOLUTION_STARTUP_RETRYABLE_DRAIN_STATUSES"
        )
        if env_startup_drain_statuses is not None:
            self.evolution_startup_retryable_drain_statuses = (
                env_startup_drain_statuses
            )
        statuses = [
            item.strip()
            for item in str(self.evolution_startup_retryable_drain_statuses).split(",")
            if item.strip()
        ]
        self.evolution_startup_retryable_drain_statuses = (
            ",".join(dict.fromkeys(statuses)) or "failed_retryable"
        )
        env_recovery_stale_timeout = os.environ.get(
            "OPENSPACE_EVOLUTION_RECOVERY_STALE_JOB_TIMEOUT_S"
        )
        if env_recovery_stale_timeout is not None:
            try:
                self.evolution_recovery_stale_job_timeout_s = float(
                    env_recovery_stale_timeout
                )
            except ValueError:
                raise ValueError(
                    "OPENSPACE_EVOLUTION_RECOVERY_STALE_JOB_TIMEOUT_S "
                    "must be a number"
                ) from None
        self.evolution_recovery_stale_job_timeout_s = max(
            0.0,
            float(self.evolution_recovery_stale_job_timeout_s),
        )
        env_behavior_revisions = os.environ.get(
            "OPENSPACE_EVOLUTION_BEHAVIOR_EVAL_MAX_REVISIONS"
        )
        if env_behavior_revisions is not None:
            try:
                self.evolution_behavior_eval_max_revisions = int(env_behavior_revisions)
            except ValueError:
                raise ValueError(
                    "OPENSPACE_EVOLUTION_BEHAVIOR_EVAL_MAX_REVISIONS must be an integer"
                ) from None
        self.evolution_behavior_eval_max_revisions = max(
            0,
            int(self.evolution_behavior_eval_max_revisions),
        )
        self.evolution_behavior_eval_require_replay_runner = _env_bool(
            "OPENSPACE_EVOLUTION_BEHAVIOR_EVAL_REQUIRE_REPLAY_RUNNER",
            self.evolution_behavior_eval_require_replay_runner,
        )
        self.evolution_routing_eval_enabled = _env_bool(
            "OPENSPACE_EVOLUTION_ROUTING_EVAL_ENABLED",
            self.evolution_routing_eval_enabled,
        )
        self.evolution_routing_eval_required = _env_bool(
            "OPENSPACE_EVOLUTION_ROUTING_EVAL_REQUIRED",
            self.evolution_routing_eval_required,
        )
        self.evolution_replay_command = (
            self.evolution_replay_command
            or os.environ.get("OPENSPACE_EVOLUTION_REPLAY_COMMAND")
        )
        self.evolution_replay_docker_image = (
            self.evolution_replay_docker_image
            or os.environ.get("OPENSPACE_EVOLUTION_REPLAY_DOCKER_IMAGE")
        )
        env_replay_timeout = os.environ.get("OPENSPACE_EVOLUTION_REPLAY_TIMEOUT_S")
        if env_replay_timeout is not None:
            try:
                self.evolution_replay_timeout_s = float(env_replay_timeout)
            except ValueError:
                raise ValueError(
                    "OPENSPACE_EVOLUTION_REPLAY_TIMEOUT_S must be a number"
                ) from None
        self.evolution_replay_timeout_s = max(
            1.0,
            float(self.evolution_replay_timeout_s),
        )
        if not self.llm_model:
            raise ValueError("llm_model is required")
        
        logger.debug(f"OpenSpaceConfig initialized with model: {self.llm_model}")


class OpenSpace:
    __slots__ = ("_runtime",)

    def __init__(self, config: Optional[OpenSpaceConfig] = None):
        config = config or OpenSpaceConfig()
        _configure_logging_from_config(config)
        self._runtime = OpenSpaceRuntime(
            config=config,
            event_bus=RuntimeEventBus(dispatcher=self._dispatch_event),
            bridge_dispatch_suppressed=self._is_bridge_dispatch_suppressed,
        )
        self._runtime.state.warm_core = config.warm_core
        self._runtime.state.diagnostic_tracker = diagnostic_tracker
        self._runtime.state.reasoning_effort = config.llm_kwargs.get(
            "reasoning_effort"
        )
        self._runtime.state.event_proxy = _EventDispatcherProxy(self._dispatch_event)
        
        logger.debug("OpenSpace instance created")

    @property
    def config(self) -> OpenSpaceConfig:
        """Return the runtime-owned configuration."""

        return self._runtime.config

    @property
    def runtime(self) -> OpenSpaceRuntime:
        """Return the runtime object that owns mutable execution/session state."""

        return self._runtime

    @property
    def current_session_id(self) -> str | None:
        return self._runtime.current_session_id

    @property
    def current_session_metadata(self) -> Dict[str, Any] | None:
        return self._runtime.current_session_metadata

    @property
    def current_session_storage(self) -> SessionStorage | None:
        return self._runtime.session_storage

    @property
    def cost_tracker(self) -> CostTracker:
        return self._runtime.cost_tracker

    def get_llm_client(self) -> Any | None:
        """Return the initialized LLM client owned by the runtime."""
        return self._runtime.llm_client

    def get_grounding_client(self) -> Any | None:
        """Return the initialized grounding client owned by the runtime."""
        return self._runtime.grounding_client

    def get_grounding_config(self) -> Any | None:
        """Return the active grounding configuration owned by the runtime."""
        return self._runtime.grounding_config

    def get_skill_registry(self) -> Any | None:
        """Return the initialized skill registry owned by the runtime."""
        return self._runtime.skill_registry

    def get_skill_store(self) -> Any | None:
        """Return the initialized skill store owned by the runtime."""
        return self._runtime.skill_store

    def get_trigger_engine(self) -> Any | None:
        """Return the initialized evolution trigger engine owned by the runtime."""
        return self._runtime.state.trigger_engine

    def get_evolution_engine(self) -> Any | None:
        """Return the initialized evolution engine owned by the runtime."""
        return self._runtime.state.evolution_engine

    def get_grounding_agent(self) -> Any | None:
        """Return the initialized grounding agent owned by the runtime."""
        return self._runtime.grounding_agent

    def get_recording_manager(self) -> Any | None:
        """Return the initialized recording manager owned by the runtime."""
        return self._runtime.recording_manager

    def get_execution_analyzer(self) -> Any | None:
        """Return the initialized execution analyzer owned by the runtime."""
        return self._runtime.execution_analyzer
    
    def set_tui_bridge(self, bridge: "TUIBridge") -> None:
        """Attach a TUI bridge for event streaming."""
        self._runtime.state.tui_bridge = bridge
        self._runtime.propagate_service_hooks()

    def register_event_sink(
        self,
        sink: Callable[[str, Dict[str, Any]], Any],
    ) -> None:
        """Attach a runtime event observer."""
        self._runtime.register_event_sink(sink)

    def unregister_event_sink(
        self,
        sink: Callable[[str, Dict[str, Any]], Any],
    ) -> None:
        """Remove a runtime event observer."""
        self._runtime.unregister_event_sink(sink)

    async def background_all_foreground_tasks(self) -> list[str]:
        """Background active foreground shell tasks for TUI Ctrl+B."""

        multi_agent = self._runtime.multi_agent
        if multi_agent is None:
            return []
        return await multi_agent.background_all_foreground_tasks()

    @asynccontextmanager
    async def suppress_bridge_dispatch(self):
        """Temporarily prevent `_dispatch_event()` from forwarding to the TUI bridge."""
        token = _BRIDGE_DISPATCH_SUPPRESSED.set(True)
        try:
            yield
        finally:
            _BRIDGE_DISPATCH_SUPPRESSED.reset(token)

    def _is_bridge_dispatch_suppressed(self) -> bool:
        return _BRIDGE_DISPATCH_SUPPRESSED.get()

    async def _dispatch_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """Dispatch a runtime event to the bridge and all local sinks."""
        tui_bridge = self._runtime.state.tui_bridge
        if tui_bridge is not None and not _BRIDGE_DISPATCH_SUPPRESSED.get():
            try:
                await tui_bridge.send(event_type, data)
            except Exception:
                pass

        for sink in self._runtime.iter_event_sinks():
            try:
                result = sink(event_type, data)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.debug("Event sink failed for %s", event_type, exc_info=True)

    async def initialize(self, *, low_latency_profiler: Any | None = None) -> None:
        await self._runtime.initialize_services(
            low_latency_profiler=low_latency_profiler,
        )

    async def execute_streaming(
        self,
        request: ExecutionRequest,
    ):
        """Execute a task and yield runtime events as ``StreamEvent`` objects."""
        if not isinstance(request, ExecutionRequest):
            raise TypeError("OpenSpace.execute_streaming() requires an ExecutionRequest")
        queue: asyncio.Queue[StreamEvent] = asyncio.Queue()
        event_bus = RuntimeEventBus()

        async def _queue_sink(event_type: str, data: Dict[str, Any]) -> None:
            await queue.put(StreamEvent(type=event_type, data=dict(data)))

        async def _sink(event_type: str, data: Dict[str, Any]) -> None:
            await event_bus.emit(event_type, data)

        event_bus.register_sink(_queue_sink)
        self._runtime.register_event_sink(_sink)
        task_future = asyncio.create_task(self.execute(request))

        try:
            while True:
                if task_future.done() and queue.empty():
                    break

                queue_get = asyncio.create_task(queue.get())
                done, pending = await asyncio.wait(
                    {task_future, queue_get},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if queue_get in done:
                    yield queue_get.result()
                else:
                    queue_get.cancel()
                    try:
                        await queue_get
                    except asyncio.CancelledError:
                        pass

                if task_future in done and queue.empty():
                    exc = task_future.exception()
                    if exc is not None:
                        raise exc
                    break
        finally:
            self._runtime.unregister_event_sink(_sink)
            if not task_future.done():
                task_future.cancel()
                try:
                    await task_future
                except asyncio.CancelledError:
                    pass
    
    async def execute(
        self,
        request: ExecutionRequest,
    ) -> "ExecutionResult":
        """Execute a normalized runtime request."""
        if not isinstance(request, ExecutionRequest):
            raise TypeError("OpenSpace.execute() requires an ExecutionRequest")
        if not self._runtime.is_initialized:
            raise RuntimeError(
                "OpenSpace not initialized. "
                "Call await initialize() before execute() or use async with."
            )

        return await self._runtime.execute(request)

    async def restore_session(self, session_id: str) -> Dict[str, Any]:
        """Restore persisted session state into the active runtime."""
        return await self._runtime.restore_session(session_id)

    async def load_session_snapshot(self, session_id: str) -> Dict[str, Any]:
        """Load persisted session data without mutating runtime or workspace."""
        return await self._runtime.load_session_snapshot(session_id)

    async def discover_sessions(
        self,
        *,
        page: int = 0,
        page_size: int = 20,
        limit: int = 50,
        all_projects: bool = False,
    ) -> Dict[str, Any]:
        """Discover resumable canonical sessions."""
        return await self._runtime.discover_sessions(
            page=page,
            page_size=page_size,
            limit=limit,
            all_projects=all_projects,
        )

    async def fork_session(self, session_id: str) -> Dict[str, Any]:
        """Fork a session while preserving OpenSpace SessionStorage transcripts."""
        return await self._runtime.fork_session(session_id)

    async def rewind_session(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Replace a session transcript with a rewound message list."""
        return await self._runtime.rewind_session(session_id, messages)

    async def save_current_session(
        self,
        session_name: str | None = None,
    ) -> Dict[str, Any]:
        """Persist the currently active session snapshot.

        This is a best-effort slash-command oriented save path. It reuses the
        latest persisted messages for the active session and updates metadata
        plus cost snapshot.
        """
        return await self._runtime.save_current_session(session_name)

    async def save_compacted_session(
        self,
        session_id: str,
        compacted_messages: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Replace a session transcript with compacted messages."""

        runtime = self._runtime
        session_record = runtime.build_session_record(
            dict(self.current_session_metadata or {}),
            compacted_messages,
            execution_context=None,
        )
        session_record["last_status"] = "compacted"
        runtime_metadata = dict(session_record.get("runtime", {}))
        runtime_metadata["phase"] = "compacted"
        session_record["runtime"] = runtime_metadata

        metadata, messages = await runtime.save_canonical_session_messages(
            session_id,
            session_record,
            compacted_messages,
            replace=True,
        )
        return {
            "session_id": session_id,
            "metadata": metadata,
            "messages": messages,
            "record": session_record,
        }

    async def run_manual_dream(
        self,
        extra_context: str = "",
        *,
        logs_mode: bool = False,
    ) -> Dict[str, Any]:
        """Run user-triggered memory consolidation for the active workspace."""

        runtime = self._runtime
        llm_client = runtime.llm_client
        grounding_client = runtime.grounding_client
        if llm_client is None:
            return {
                "status": "error",
                "message": "LLM client is not available.",
            }

        metadata = dict(runtime.current_session_metadata or {})
        workspace_dir = (
            metadata.get("project_path")
            or metadata.get("workspace_dir")
            or self.config.workspace_dir
            or Path.cwd()
        )
        cwd = str(Path(str(workspace_dir)).expanduser().resolve())
        session_id = runtime.current_session_id
        messages: List[Dict[str, Any]] = []
        if session_id:
            try:
                restored = await self.load_session_snapshot(session_id)
                restored_messages = restored.get("messages", [])
                if isinstance(restored_messages, list):
                    messages = [
                        message
                        for message in restored_messages
                        if isinstance(message, dict)
                    ]
                restored_record = restored.get("session_record")
                if isinstance(restored_record, dict):
                    metadata = dict(restored_record)
            except FileNotFoundError:
                session_id = None
            except Exception:
                logger.debug("Could not load active session before /dream", exc_info=True)

        async def append_system_message(message: Dict[str, Any]) -> None:
            nonlocal metadata, messages
            messages.append(message)
            if not session_id:
                return
            record = runtime.build_session_record(
                dict(metadata),
                messages,
                execution_context=None,
            )
            metadata, messages = await runtime.save_canonical_session_messages(
                session_id,
                record,
                messages,
            )

        tools: List[Any] = []
        if grounding_client is not None:
            try:
                tools = await grounding_client.list_tools(use_cache=True)
            except Exception:
                logger.debug("Could not list tools before /dream", exc_info=True)

        try:
            from openspace.services.memory.dream import execute_manual_auto_dream
            from openspace.services.memory.daily_log import get_memory_mode
            from openspace.services.tooling.context import ToolUseContext
        except Exception as exc:
            return {
                "status": "error",
                "message": f"Dream runtime is not available: {exc}",
            }

        context = ToolUseContext(
            tools=list(tools),
            all_tools=list(tools),
            model=str(getattr(llm_client, "model", self.config.llm_model)),
            llm_client=llm_client,
            cwd=cwd,
            agent_id="primary",
            messages=messages,
            event_sink=self._dispatch_event,
            tui_available=runtime.state.tui_bridge is not None,
            session_id=session_id,
            session_dir=(
                str(runtime.session_storage.session_dir)
                if runtime.session_storage is not None
                else None
            ),
            tool_results_dir=(
                str(runtime.session_storage.tool_results_dir)
                if runtime.session_storage is not None
                else None
            ),
            session_storage=runtime.session_storage,
            memory_mode=get_memory_mode(),
            append_system_message=append_system_message,
            backend_scope=tuple(self.config.backend_scope or ()),
        )
        result = await execute_manual_auto_dream(
            context,
            append_system_message,
            extra_context=extra_context,
            logs_mode=logs_mode,
        )
        if result.error:
            return {
                "status": "error",
                "message": result.error,
                "result": result,
            }
        if result.ran:
            return {
                "status": "completed",
                "message": _format_manual_dream_result(result),
                "result": result,
            }
        return {
            "status": "skipped",
            "message": _format_manual_dream_skip(result.skipped_reason),
            "result": result,
        }

    async def run_manual_summary(self) -> Dict[str, Any]:
        """Force a OpenSpace Session Memory extraction for the active session."""

        runtime = self._runtime
        llm_client = runtime.llm_client
        grounding_client = runtime.grounding_client
        if llm_client is None:
            return {
                "status": "error",
                "message": "LLM client is not available.",
                "error": "missing_llm_client",
            }

        session_id = runtime.current_session_id
        if not session_id:
            return {
                "status": "skipped",
                "message": _format_manual_summary_skip("no_active_session"),
                "skipped_reason": "no_active_session",
            }

        try:
            restored = await self.load_session_snapshot(session_id)
        except FileNotFoundError:
            return {
                "status": "skipped",
                "message": _format_manual_summary_skip("no_active_session"),
                "session_id": session_id,
                "skipped_reason": "no_active_session",
            }
        except Exception as exc:
            logger.debug("Could not load active session before /summary", exc_info=True)
            return {
                "status": "error",
                "message": f"Could not load active session: {exc}",
                "session_id": session_id,
                "error": str(exc),
            }

        restored_messages = restored.get("messages", [])
        messages: List[Dict[str, Any]] = [
            message for message in restored_messages if isinstance(message, dict)
        ] if isinstance(restored_messages, list) else []
        if not messages:
            return {
                "status": "skipped",
                "message": _format_manual_summary_skip("no_messages"),
                "session_id": session_id,
                "skipped_reason": "no_messages",
            }

        metadata = dict(runtime.current_session_metadata or {})
        restored_record = restored.get("session_record")
        if isinstance(restored_record, dict):
            metadata = dict(restored_record)

        workspace_dir = (
            metadata.get("project_path")
            or metadata.get("workspace_dir")
            or self.config.workspace_dir
            or Path.cwd()
        )
        cwd = str(Path(str(workspace_dir)).expanduser().resolve())

        async def append_system_message(message: Dict[str, Any]) -> None:
            nonlocal metadata, messages
            messages.append(message)
            record = runtime.build_session_record(
                dict(metadata),
                messages,
                execution_context=None,
            )
            metadata, messages = await runtime.save_canonical_session_messages(
                session_id,
                record,
                messages,
            )

        tools: List[Any] = []
        if grounding_client is not None:
            try:
                tools = await grounding_client.list_tools(use_cache=True)
            except Exception:
                logger.debug("Could not list tools before /summary", exc_info=True)

        try:
            from openspace.services.memory.session_memory import (
                get_session_memory_path_for_context,
                manually_extract_session_memory,
                wait_for_session_memory_extraction,
            )
            from openspace.services.tooling.context import ToolUseContext
        except Exception as exc:
            return {
                "status": "error",
                "message": f"Session memory runtime is not available: {exc}",
                "session_id": session_id,
                "error": str(exc),
            }

        context = ToolUseContext(
            tools=list(tools),
            all_tools=list(tools),
            model=str(getattr(llm_client, "model", self.config.llm_model)),
            llm_client=llm_client,
            cwd=cwd,
            agent_id="primary",
            messages=messages,
            event_sink=self._dispatch_event,
            tui_available=runtime.state.tui_bridge is not None,
            session_id=session_id,
            session_dir=(
                str(runtime.session_storage.session_dir)
                if runtime.session_storage is not None
                else None
            ),
            tool_results_dir=(
                str(runtime.session_storage.tool_results_dir)
                if runtime.session_storage is not None
                else None
            ),
            session_storage=runtime.session_storage,
            append_system_message=append_system_message,
            backend_scope=tuple(self.config.backend_scope or ()),
        )

        await wait_for_session_memory_extraction(context)
        result = await manually_extract_session_memory(messages, context)
        await wait_for_session_memory_extraction(context)

        memory_path = result.memory_path
        if not memory_path:
            try:
                memory_path = str(get_session_memory_path_for_context(context))
            except Exception:
                memory_path = None

        base: Dict[str, Any] = {
            "session_id": session_id,
            "memory_path": memory_path,
            "result": result,
        }
        if result.error:
            return {
                **base,
                "status": "error",
                "message": result.error,
                "error": result.error,
            }
        if result.ran:
            return {
                **base,
                "status": "completed",
                "message": _format_manual_summary_result(result),
            }
        return {
            **base,
            "status": "skipped",
            "message": _format_manual_summary_skip(result.skipped_reason),
            "skipped_reason": result.skipped_reason,
        }

    def update_main_loop_model(self, model: str) -> None:
        """Update the active runtime model."""
        self.config.llm_model = model
        if self._runtime.llm_client is not None:
            self._runtime.llm_client.model = model

    def update_thinking_enabled(self, enabled: bool) -> None:
        """Update the active runtime extended-thinking switch."""
        self.config.llm_enable_thinking = bool(enabled)
        if self._runtime.llm_client is not None:
            self._runtime.llm_client.enable_thinking = bool(enabled)

    def update_reasoning_effort(self, effort: str | None) -> None:
        """Update the default reasoning effort for future main-loop calls."""

        normalized_raw = str(effort).strip().lower() if effort is not None else ""
        if normalized_raw in {"", "auto", "unset", "none"}:
            self._runtime.state.reasoning_effort = None
            self.config.llm_kwargs.pop("reasoning_effort", None)
            return
        parsed = parse_effort_value(effort)
        if parsed is None:
            raise ValueError(f"Unsupported reasoning effort: {effort}")
        normalized = convert_effort_value_to_level(parsed).value
        self._runtime.state.reasoning_effort = normalized
        self.config.llm_kwargs["reasoning_effort"] = normalized

    def get_reasoning_effort(self) -> str | None:
        """Return the configured default reasoning effort, or ``None`` for auto."""

        return self._runtime.reasoning_effort

    def get_runtime_status(self) -> Dict[str, Any]:
        """Return the current runtime snapshot for TUI status sync."""
        return self._runtime.get_runtime_status()

    def _get_sandbox_runtime_status(self) -> Dict[str, Any] | None:
        return self._runtime.get_sandbox_runtime_status()

    async def cleanup(self) -> None:
        """
        Close all sessions and release resources.
        Automatically called when using context manager.
        """
        await self._runtime.cleanup_resources()
    
    def is_initialized(self) -> bool:
        return self._runtime.is_initialized
    
    def is_running(self) -> bool:
        return self._runtime.is_running
    
    def get_config(self) -> OpenSpaceConfig:
        return self.config
    
    def list_backends(self) -> List[str]:
        if not self._runtime.is_initialized:
            raise RuntimeError("OpenSpace not initialized")
        grounding_client = self._runtime.grounding_client
        if grounding_client is None:
            return []
        return [backend.value for backend in grounding_client.list_providers().keys()]
    
    def list_sessions(self) -> List[str]:
        if not self._runtime.is_initialized:
            raise RuntimeError("OpenSpace not initialized")
        grounding_client = self._runtime.grounding_client
        if grounding_client is None:
            return []
        return grounding_client.list_sessions()
    
    async def __aenter__(self):
        """Context manager entry"""
        await self.initialize()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        await self.cleanup()
        return False
    
    def __repr__(self) -> str:
        status = "initialized" if self._runtime.is_initialized else "not initialized"
        if self._runtime.is_running:
            status = "running"
        backends = ", ".join(self.config.backend_scope) if self.config.backend_scope else "all"
        return f"<OpenSpace(status={status}, backends={backends}, model={self.config.llm_model})>"
