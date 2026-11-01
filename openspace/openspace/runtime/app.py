from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .event_bus import RuntimeEventBus
from .execution_lifecycle import ExecutionLifecycle
from .execution_request import ExecutionRequest, ExecutionResult
from .session_runtime import SessionRuntime
from .turn_runner import TurnRunner
from .workspace_runtime import WorkspaceRuntime

from openspace.llm.types import TokenUsage
from openspace.persistence import FileHistory, SessionStorage
from openspace.services.runtime_support.cost import CostTracker
from openspace.services.lsp import shutdown_lsp_server_manager
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

_CLEANUP_MEMORY_DRAIN_TIMEOUT_S = 10.0
_QUALITY_SIGNAL_CUTOVER_CHECKPOINT = "quality_signal_cutover:last_watermark"
_GENERAL_EVOLUTION_TRIGGER_TYPES = ("ANALYSIS", "MANUAL", "QUALITY_SIGNAL")


def _default_evolution_replay_command(*, docker_image: str | None = None) -> list[str]:
    executable = "python" if docker_image else (sys.executable or "python")
    return [executable, "-m", "openspace.skill_engine.evolution.eval_worker"]


def _runtime_source_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _evolution_replay_sandbox_manager(workspace_dir: str | Path | None) -> Any | None:
    try:
        from openspace.services.sandbox import get_process_sandbox_manager

        return get_process_sandbox_manager(cwd=workspace_dir)
    except Exception:
        logger.debug("Evolution replay sandbox manager unavailable", exc_info=True)
        return None


def _new_set_event() -> asyncio.Event:
    event = asyncio.Event()
    event.set()
    return event


def _derive_session_title(messages: list[dict[str, Any]]) -> str:
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()[:80]
        text = message.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()[:80]
    return ""


def _runtime_skill_store_db_path(config: Any, storage_root: Path | None) -> Path | None:
    from openspace.skill_engine.evidence import resolve_skill_store_db_path

    explicit = (
        getattr(config, "skill_store_db_path", None)
        or os.environ.get("OPENSPACE_SKILL_STORE_DB_PATH")
    )
    return resolve_skill_store_db_path(
        explicit_db_path=explicit,
        storage_root=storage_root,
        workspace_dir=getattr(config, "workspace_dir", None),
    )


def _env_evolution_allowed_read_roots() -> list[Path]:
    roots: list[Path] = []
    raw = os.environ.get("OPENSPACE_EVOLUTION_ALLOWED_READ_ROOTS", "")
    for item in raw.split(os.pathsep):
        text = item.strip()
        if not text:
            continue
        try:
            path = Path(text).expanduser()
        except (TypeError, ValueError):
            continue
        roots.append(path)
    return roots


@dataclass(slots=True)
class OpenSpaceRuntimeState:
    """Mutable state for a single OpenSpace runtime/session."""

    llm_client: Any | None = None
    grounding_client: Any | None = None
    grounding_config: Any | None = None
    grounding_agent: Any | None = None
    multi_agent: Any | None = None
    recording_manager: Any | None = None
    skill_registry: Any | None = None
    skill_store: Any | None = None
    execution_analyzer: Any | None = None
    skill_evolver: Any | None = None
    evidence_store: Any | None = None
    evidence_runtime_adapter: Any | None = None
    trigger_engine: Any | None = None
    packet_builder: Any | None = None
    decision_engine: Any | None = None
    evolution_engine: Any | None = None
    behavior_evaluator: Any | None = None
    candidate_store: Any | None = None
    evolution_storage_root: Path | None = None
    diagnostic_tracker: Any | None = None
    reasoning_effort: str | None = None
    event_proxy: Any | None = None
    tui_bridge: Any | None = None
    warm_core: Any | None = None
    cost_tracker: CostTracker = field(default_factory=CostTracker)
    session_storage: SessionStorage | None = None
    file_history: FileHistory | None = None
    scheduler: Any | None = None
    current_session_id: str | None = None
    current_session_metadata: dict[str, Any] | None = None
    memory_cleanup_context: dict[str, Any] | None = None
    event_sinks: list[Callable[[str, dict[str, Any]], Any]] = field(default_factory=list)
    post_execution_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    execution_count: int = 0
    last_evolved_skills: list[dict[str, Any]] = field(default_factory=list)
    capture_skill_dir: str | None = None
    initialized: bool = False
    running: bool = False
    task_done: asyncio.Event = field(default_factory=_new_set_event)


class OpenSpaceRuntime:
    """Runtime orchestration for one OpenSpace execution request.

    This class owns mutable runtime state and the execution lifecycle:
    initialization status, session prepare, workspace resolution, turn
    execution, post-turn draining, and persistence. The top-level ``OpenSpace``
    object remains the public API shell; mutable execution services should be
    injected into ``OpenSpaceRuntimeState`` instead of read back from private
    facade fields.
    """

    def __init__(
        self,
        *,
        config: Any,
        event_bus: RuntimeEventBus | None = None,
        state: OpenSpaceRuntimeState | None = None,
        session_runtime: SessionRuntime | None = None,
        workspace_runtime: WorkspaceRuntime | None = None,
        turn_runner: TurnRunner | None = None,
        execution_lifecycle: ExecutionLifecycle | None = None,
        bridge_dispatch_suppressed: Callable[[], bool] | None = None,
    ) -> None:
        self.config = config
        self.state = state or OpenSpaceRuntimeState()
        self.event_bus = event_bus or RuntimeEventBus()
        self._bridge_dispatch_suppressed = bridge_dispatch_suppressed or (lambda: False)
        self.session_runtime = session_runtime or SessionRuntime.from_runtime(self)
        self.workspace_runtime = (
            workspace_runtime or WorkspaceRuntime.from_config(config)
        )
        self.turn_runner = turn_runner or TurnRunner.from_runtime(self)
        self.execution_lifecycle = execution_lifecycle or ExecutionLifecycle(self)

    @property
    def cost_tracker(self) -> CostTracker:
        return self.state.cost_tracker

    @cost_tracker.setter
    def cost_tracker(self, value: CostTracker) -> None:
        self.state.cost_tracker = value

    @property
    def session_storage(self) -> SessionStorage | None:
        return self.state.session_storage

    @session_storage.setter
    def session_storage(self, value: SessionStorage | None) -> None:
        self.state.session_storage = value
        if value is None:
            return
        adapter = self.state.evidence_runtime_adapter
        set_entry_sink = getattr(value, "set_entry_sink", None)
        if adapter is not None and callable(set_entry_sink):
            set_entry_sink(getattr(adapter, "on_session_entry", None))

    @property
    def file_history(self) -> FileHistory | None:
        return self.state.file_history

    @file_history.setter
    def file_history(self, value: FileHistory | None) -> None:
        self.state.file_history = value

    @property
    def scheduler(self) -> Any | None:
        return self.state.scheduler

    @scheduler.setter
    def scheduler(self, value: Any | None) -> None:
        self.state.scheduler = value

    @property
    def current_session_id(self) -> str | None:
        return self.state.current_session_id

    @current_session_id.setter
    def current_session_id(self, value: str | None) -> None:
        self.state.current_session_id = value

    @property
    def current_session_metadata(self) -> dict[str, Any] | None:
        return self.state.current_session_metadata

    @current_session_metadata.setter
    def current_session_metadata(self, value: dict[str, Any] | None) -> None:
        self.state.current_session_metadata = value
        session_dir = (value or {}).get("session_dir")
        self.register_evidence_read_roots(session_dir)

    def register_evidence_read_roots(self, *roots: Any) -> None:
        store = self.state.evidence_store
        add_many = getattr(store, "add_allowed_read_roots", None)
        if store is None or not callable(add_many):
            return
        cleaned: list[Path] = []
        for root in roots:
            if not root:
                continue
            try:
                path = Path(root).expanduser()
            except (TypeError, ValueError):
                continue
            if path.is_file():
                path = path.parent
            cleaned.append(path)
        if cleaned:
            add_many(cleaned)

    def register_skill_evidence_read_roots(self) -> None:
        roots: list[Path] = []
        registry = self.state.skill_registry
        if registry is not None:
            try:
                skills = registry.list_skills()
            except Exception:
                skills = []
            for skill in skills or []:
                path = getattr(skill, "path", None)
                if path:
                    roots.append(Path(path).expanduser().parent)
        store = self.state.skill_store
        load_active = getattr(store, "load_active", None)
        if callable(load_active):
            try:
                records = load_active()
            except Exception:
                records = {}
            for record in (records or {}).values():
                path = getattr(record, "path", None)
                if path:
                    roots.append(Path(path).expanduser().parent)
        self.register_evidence_read_roots(*roots)

    @property
    def llm_client(self) -> Any | None:
        return self.state.llm_client

    @property
    def grounding_client(self) -> Any | None:
        return self.state.grounding_client

    @property
    def grounding_config(self) -> Any | None:
        return self.state.grounding_config

    @property
    def grounding_agent(self) -> Any | None:
        return self.state.grounding_agent

    @property
    def multi_agent(self) -> Any | None:
        return self.state.multi_agent

    @property
    def recording_manager(self) -> Any | None:
        return self.state.recording_manager

    @property
    def skill_registry(self) -> Any | None:
        return self.state.skill_registry

    @property
    def skill_store(self) -> Any | None:
        return self.state.skill_store

    @property
    def execution_analyzer(self) -> Any | None:
        return self.state.execution_analyzer

    @property
    def diagnostic_tracker(self) -> Any | None:
        return self.state.diagnostic_tracker

    @property
    def reasoning_effort(self) -> str | None:
        return self.state.reasoning_effort

    async def initialize_services(
        self,
        *,
        low_latency_profiler: Any | None = None,
    ) -> None:
        """Initialize runtime-owned services for the public OpenSpace facade."""

        config = self.config
        if self.is_initialized:
            logger.warning("OpenSpace already initialized")
            return

        def _startup_span(name: str, **metadata: Any):
            if low_latency_profiler is None:
                return nullcontext()
            span = getattr(low_latency_profiler, "span", None)
            if not callable(span):
                return nullcontext()
            return span(name, **metadata)

        def _startup_mark(name: str, **metadata: Any) -> None:
            if low_latency_profiler is None:
                return
            marker = getattr(low_latency_profiler, "mark", None)
            if callable(marker):
                marker(name, **metadata)

        logger.info("Initializing OpenSpace...")
        try:
            from openspace.agents.grounding_agent import GroundingAgent
            from openspace.agents.multi_agent_orchestrator import MultiAgentOrchestrator
            from openspace.config import get_config, load_config
            from openspace.config.constants import CONFIG_GROUNDING, CONFIG_SECURITY
            from openspace.config.loader import CONFIG_DIR, get_agent_config
            from openspace.grounding.core.grounding_client import GroundingClient
            from openspace.llm import LLMClient
            from openspace.recording import RecordingManager
            from openspace.services.runtime_support.background import (
                run_startup_evolution_recovery,
                start_background_housekeeping,
            )
            from openspace.services.lsp import initialize_lsp_server_manager
            from openspace.skill_engine import ExecutionAnalyzer, SkillStore
            from openspace.skill_engine.evidence import (
                EvidenceStore,
                PacketBuilder,
                RuntimeEvidenceAdapter,
                resolve_evidence_db_path,
                resolve_evolution_storage_root,
            )
            from openspace.skill_engine.evolution import (
                CaptureContractSemanticReviewer,
                EvolutionAdmission,
                EvolutionCandidateStore,
                EvolutionCommitter,
                EvolutionEngine,
                EvolutionRecovery,
                EvolutionValidator,
                SkillBehaviorEvaluator,
                SkillEvolverAuthoringBackend,
                SubprocessSkillReplayRunner,
            )
            from openspace.skill_engine.decision import DecisionEngine
            from openspace.skill_engine.evolver import SkillEvolver
            from openspace.skill_engine.triggers import TriggerEngine

            self.state.llm_client = LLMClient(
                model=config.llm_model,
                enable_thinking=config.llm_enable_thinking,
                rate_limit_delay=config.llm_rate_limit_delay,
                max_retries=config.llm_max_retries,
                timeout=config.llm_timeout,
                **config.llm_kwargs,
            )
            logger.info("✓ LLM Client: %s", config.llm_model)

            if config.grounding_config_path:
                grounding_config = load_config(
                    CONFIG_DIR / CONFIG_GROUNDING,
                    CONFIG_DIR / CONFIG_SECURITY,
                    config.grounding_config_path,
                )
                logger.info(
                    "Merged custom grounding config: %s",
                    config.grounding_config_path,
                )
            else:
                grounding_config = get_config()

            if getattr(config, "use_clawwork_productivity", False):
                shell_cfg = grounding_config.shell.model_copy(
                    update={
                        "use_clawwork_productivity": True,
                        "working_dir": config.workspace_dir
                        or grounding_config.shell.working_dir,
                    }
                )
                grounding_config = grounding_config.model_copy(
                    update={"shell": shell_cfg}
                )
                logger.info(
                    "ClawWork productivity tools enabled "
                    "(shell.working_dir used as sandbox root)"
                )

            agent_config = get_agent_config("GroundingAgent")
            cli_max_iter = config.grounding_max_iterations
            default_max_iter = type(config)().grounding_max_iterations
            if agent_config:
                cfg_max_iter = agent_config.get("max_iterations", default_max_iter)
                if cli_max_iter != default_max_iter:
                    max_iterations = cli_max_iter
                else:
                    max_iterations = cfg_max_iter
                backend_scope = (
                    config.backend_scope
                    or agent_config.get("backend_scope")
                    or ["gui", "shell", "mcp", "web", "meta"]
                )
                config.grounding_max_iterations = max_iterations
                logger.info(
                    "Loaded GroundingAgent config from config_agents.json "
                    "(max_iterations=%s)",
                    max_iterations,
                )
            else:
                max_iterations = config.grounding_max_iterations
                backend_scope = (
                    config.backend_scope or ["gui", "shell", "mcp", "web", "meta"]
                )
                logger.warning(
                    "config_agents.json not found, using default config "
                    "(max_iterations=%s)",
                    max_iterations,
                )

            if grounding_config.enabled_backends:
                scope_set = set(backend_scope)
                filtered = [
                    entry
                    for entry in grounding_config.enabled_backends
                    if entry.get("name", "").lower() in scope_set
                ]
                if len(filtered) != len(grounding_config.enabled_backends):
                    skipped = [
                        entry.get("name")
                        for entry in grounding_config.enabled_backends
                        if entry.get("name", "").lower() not in scope_set
                    ]
                    logger.info("Skipping backends not in scope: %s", skipped)
                    grounding_config = grounding_config.model_copy(
                        update={"enabled_backends": filtered}
                    )

            self.state.grounding_config = grounding_config
            evolution_storage_root = resolve_evolution_storage_root(
                explicit_root=getattr(config, "evolution_storage_root", None),
                explicit_db_path=(
                    getattr(config, "evidence_db_path", None)
                    or getattr(config, "skill_store_db_path", None)
                ),
                session_storage=self.state.session_storage,
                skill_store=self.state.skill_store,
                workspace_dir=config.workspace_dir,
            )
            self.state.evolution_storage_root = evolution_storage_root
            quality_cfg = getattr(grounding_config, "tool_quality", None)
            quality_db_path = _runtime_skill_store_db_path(
                config,
                evolution_storage_root,
            )
            if (
                quality_cfg is not None
                and quality_db_path is not None
                and not getattr(quality_cfg, "db_path", None)
            ):
                try:
                    grounding_config = grounding_config.model_copy(
                        update={
                            "tool_quality": quality_cfg.model_copy(
                                update={"db_path": str(quality_db_path)}
                            )
                        }
                    )
                    self.state.grounding_config = grounding_config
                except Exception:
                    logger.debug("Failed to align tool quality DB path", exc_info=True)
            with _startup_span(
                "provider.register",
                backend_scope=tuple(str(item) for item in backend_scope),
            ):
                self.state.grounding_client = GroundingClient(config=grounding_config)
            with _startup_span(
                "provider.initialize",
                backend_scope=tuple(str(item) for item in backend_scope),
            ):
                await self.state.grounding_client.initialize_all_providers()

            backends = list(self.state.grounding_client.list_providers().keys())
            logger.info("✓ Grounding Client: %s backends", len(backends))
            logger.debug("  Available backends: %s", [b.value for b in backends])

            if config.enable_recording:
                self.state.recording_manager = RecordingManager(
                    enabled=True,
                    task_id="",
                    log_dir=config.recording_log_dir,
                    backends=config.recording_backends,
                    enable_screenshot=config.enable_screenshot,
                    enable_video=config.enable_video,
                    enable_conversation_log=config.enable_conversation_log,
                    agent_name="OpenSpace",
                )
                self.state.grounding_client.recording_manager = (
                    self.state.recording_manager
                )
                logger.info(
                    "✓ Recording enabled: %s backends",
                    len(self.state.recording_manager.backends or []),
                )

            if getattr(config, "evolution_evidence_enabled", True):
                try:
                    evidence_db_path = resolve_evidence_db_path(
                        explicit_db_path=getattr(config, "evidence_db_path", None),
                        storage_root=evolution_storage_root,
                        session_storage=self.state.session_storage,
                        skill_store=self.state.skill_store,
                        workspace_dir=config.workspace_dir,
                    )
                    self.state.evidence_store = EvidenceStore(
                        db_path=evidence_db_path,
                        allowed_read_roots=(evolution_storage_root,),
                    )
                    session_dir = (self.current_session_metadata or {}).get("session_dir")
                    self.register_evidence_read_roots(
                        config.workspace_dir,
                        session_dir,
                        getattr(config, "recording_log_dir", None),
                        *_env_evolution_allowed_read_roots(),
                    )
                    self.state.packet_builder = PacketBuilder(self.state.evidence_store)
                    self.state.trigger_engine = None
                    if getattr(config, "evolution_triggers_enabled", True):
                        self.state.trigger_engine = TriggerEngine(
                            self.state.evidence_store
                        )
                    self.state.candidate_store = EvolutionCandidateStore(
                        evidence_store=self.state.evidence_store,
                    )
                    replay_command = getattr(config, "evolution_replay_command", None)
                    replay_docker_image = getattr(
                        config,
                        "evolution_replay_docker_image",
                        None,
                    )
                    replay_runner = SubprocessSkillReplayRunner(
                        replay_command
                        or _default_evolution_replay_command(
                            docker_image=replay_docker_image,
                        ),
                        docker_image=replay_docker_image,
                        timeout_s=getattr(
                            config,
                            "evolution_replay_timeout_s",
                            600.0,
                        ),
                        cwd=getattr(config, "workspace_dir", None),
                        sandbox_manager=_evolution_replay_sandbox_manager(
                            getattr(config, "workspace_dir", None),
                        ),
                        pythonpath_roots=(_runtime_source_root(),),
                    )
                    self.state.behavior_evaluator = SkillBehaviorEvaluator(
                        evidence_store=self.state.evidence_store,
                        llm_client=self.state.llm_client,
                        replay_runner=replay_runner,
                        enable_routing_eval=getattr(
                            config,
                            "evolution_routing_eval_enabled",
                            True,
                        ),
                        require_routing_eval=getattr(
                            config,
                            "evolution_routing_eval_required",
                            False,
                        ),
                        require_replay_runner=getattr(
                            config,
                            "evolution_behavior_eval_require_replay_runner",
                            True,
                        ),
                    )
                    self.state.evidence_runtime_adapter = RuntimeEvidenceAdapter(
                        self.state.evidence_store,
                        trigger_engine=self.state.trigger_engine,
                    )
                    storage = self.state.session_storage
                    set_entry_sink = getattr(storage, "set_entry_sink", None)
                    if storage is not None and callable(set_entry_sink):
                        set_entry_sink(
                            getattr(
                                self.state.evidence_runtime_adapter,
                                "on_session_entry",
                                None,
                            )
                        )
                    self.event_bus.register_sink(
                        self.state.evidence_runtime_adapter.on_runtime_event
                    )
                    logger.info("✓ Evolution evidence store: %s", evidence_db_path)
                    logger.info("✓ Evolution candidate store initialized")
                    if self.state.trigger_engine is not None:
                        logger.info("✓ Evolution trigger engine initialized")
                except Exception as exc:
                    logger.warning(
                        "Evolution evidence store init failed (non-fatal): %s",
                        exc,
                    )

            if getattr(config, "evolution_engine_enabled", False):
                self.state.evolution_engine = EvolutionEngine(
                    packet_builder=self.state.packet_builder,
                    decision_engine=self.state.decision_engine,
                    admission_policy=EvolutionAdmission(
                        evidence_store=self.state.evidence_store,
                        skill_store=self.state.skill_store,
                        registry=self.state.skill_registry,
                        allow_single_observation_capture=getattr(
                            config,
                            "evolution_allow_single_observation_capture",
                            True,
                        ),
                    ),
                    candidate_store=self.state.candidate_store,
                    validator=EvolutionValidator(
                        evidence_store=self.state.evidence_store,
                        skill_store=self.state.skill_store,
                        registry=self.state.skill_registry,
                        semantic_validator=CaptureContractSemanticReviewer(
                            self.state.llm_client,
                            model=(
                                getattr(
                                    config,
                                    "evolution_capture_semantic_validation_model",
                                    None,
                                )
                                or config.execution_analyzer_model
                                or config.llm_model
                            ),
                            max_tokens=getattr(
                                config,
                                "evolution_capture_semantic_validation_max_tokens",
                                2048,
                            ),
                        ),
                        semantic_enabled=getattr(
                            config,
                            "evolution_capture_semantic_validation_enabled",
                            True,
                        ),
                    ),
                    behavior_evaluator=self.state.behavior_evaluator,
                    behavior_eval_max_revisions=getattr(
                        config,
                        "evolution_behavior_eval_max_revisions",
                        2,
                    ),
                    evolution_mode=getattr(config, "evolution_mode", "autonomous"),
                )
                logger.info(
                    "✓ Evolution engine enabled (mode=%s)",
                    getattr(config, "evolution_mode", "autonomous"),
                )

            tool_retrieval_llm = None
            if config.tool_retrieval_model:
                tool_retrieval_llm = LLMClient(
                    model=config.tool_retrieval_model,
                    timeout=config.llm_timeout,
                    max_retries=config.llm_max_retries,
                    **config.llm_kwargs,
                )
                logger.info("✓ Tool retrieval LLM: %s", config.tool_retrieval_model)

            skill_selection_llm = None
            if config.skill_registry_model:
                skill_selection_llm = LLMClient(
                    model=config.skill_registry_model,
                    timeout=30.0,
                    max_retries=2,
                    **config.llm_kwargs,
                )
                logger.info("✓ Skill selection LLM: %s", config.skill_registry_model)

            self.state.grounding_agent = GroundingAgent(
                name="OpenSpace-GroundingAgent",
                backend_scope=backend_scope,
                llm_client=self.state.llm_client,
                grounding_client=self.state.grounding_client,
                recording_manager=self.state.recording_manager,
                system_prompt=config.grounding_system_prompt,
                max_iterations=max_iterations,
                tool_retrieval_llm=tool_retrieval_llm,
                skill_selection_llm=skill_selection_llm,
                enable_turn0_llm_skill_selector=not bool(
                    config.disable_turn0_llm_skill_selector
                ),
            )
            logger.info("✓ GroundingAgent: %s", ", ".join(backend_scope))

            self.state.multi_agent = MultiAgentOrchestrator(
                grounding_client=self.state.grounding_client,
                llm_client=self.state.llm_client,
                event_sink=self.emit_runtime_event,
                workspace_dir=config.workspace_dir or Path.cwd(),
            )
            self.state.multi_agent.initialize()
            self.state.multi_agent.bind_agent(self.state.grounding_agent)
            logger.info("✓ Multi-agent orchestrator initialized")

            scheduler_workspace = str(config.workspace_dir or Path.cwd())
            should_start_scheduler = (
                config.scheduler_sync_start
                or self.workspace_has_enabled_schedules(scheduler_workspace)
            )
            if should_start_scheduler:
                with _startup_span("scheduler.ensure", phase="initialize"):
                    await self.ensure_scheduler(scheduler_workspace)
                _startup_mark("scheduler.initialize_started", phase="initialize")
                logger.info("✓ Schedule cron scheduler initialized")
            else:
                _startup_mark(
                    "scheduler.initialize_skipped_by_profile",
                    phase="initialize",
                    profile=config.capability_profile,
                )
                logger.info("Schedule cron scheduler initialization skipped by profile")

            if config.lsp_sync_start:
                try:
                    with _startup_span("lsp.initialize"):
                        initialize_lsp_server_manager(
                            cwd=str(config.workspace_dir or Path.cwd()),
                            bare=False,
                        )
                    logger.info("✓ LSP manager initialized (optional, lazy-start)")
                except Exception:
                    logger.debug("LSP manager init failed", exc_info=True)
            else:
                logger.info("LSP manager initialization skipped by profile")

            if self.state.grounding_config and self.state.grounding_config.skills.enabled:
                with _startup_span("skill.registry.discover"):
                    self.state.skill_registry = self.init_skill_registry()
                if self.state.skill_registry:
                    skills = self.state.skill_registry.list_skills()
                    logger.info("✓ Skills: %s discovered", len(skills))
                    admission_policy = None
                    if self.state.evolution_engine is not None:
                        admission_policy = getattr(
                            self.state.evolution_engine,
                            "admission_policy",
                            None,
                        )
                    if admission_policy is not None and hasattr(
                        admission_policy,
                        "registry",
                    ):
                        admission_policy.registry = self.state.skill_registry
                    validator = None
                    if self.state.evolution_engine is not None:
                        validator = getattr(
                            self.state.evolution_engine,
                            "validator",
                            None,
                        )
                    if validator is not None and hasattr(validator, "registry"):
                        validator.registry = self.state.skill_registry
                    behavior_evaluator = self.state.behavior_evaluator
                    if behavior_evaluator is not None and hasattr(
                        behavior_evaluator,
                        "registry",
                    ):
                        behavior_evaluator.registry = self.state.skill_registry
                    self.state.grounding_agent.set_skill_registry(
                        self.state.skill_registry
                    )
                    skill_cfg = self.state.grounding_config.skills
                    self.state.grounding_agent.set_skill_protocol_settings(
                        listing_enabled=skill_cfg.listing_enabled,
                        discovery_enabled=skill_cfg.discovery_enabled,
                        discovery_max_results=skill_cfg.discovery_max_results,
                        listing_budget_context_percent=skill_cfg.listing_budget_context_percent,
                        listing_max_description_chars=skill_cfg.listing_max_description_chars,
                        post_tool_query_builder_enabled=skill_cfg.post_tool_query_builder_enabled,
                        post_tool_query_builder_model=skill_cfg.post_tool_query_builder_model,
                        post_tool_query_builder_max_chars=skill_cfg.post_tool_query_builder_max_chars,
                    )

            if self.state.skill_registry and config.skill_store_sync_start:
                try:
                    with _startup_span("skill.store.sync"):
                        skill_store_db_path = _runtime_skill_store_db_path(
                            config,
                            self.state.evolution_storage_root,
                        )
                        skill_store = SkillStore(
                            skill_store_db_path,
                            trust_promotion_min_independent_successes=getattr(
                                config,
                                "skill_trust_promotion_min_independent_successes",
                                2,
                            ),
                        )
                        self.state.skill_store = skill_store
                        admission_policy = None
                        if self.state.evolution_engine is not None:
                            admission_policy = getattr(
                                self.state.evolution_engine,
                                "admission_policy",
                                None,
                            )
                        if admission_policy is not None and hasattr(
                            admission_policy,
                            "skill_store",
                        ):
                            admission_policy.skill_store = skill_store
                        validator = None
                        if self.state.evolution_engine is not None:
                            validator = getattr(
                                self.state.evolution_engine,
                                "validator",
                                None,
                            )
                        if validator is not None and hasattr(validator, "skill_store"):
                            validator.skill_store = skill_store
                        behavior_evaluator = self.state.behavior_evaluator
                        if behavior_evaluator is not None and hasattr(
                            behavior_evaluator,
                            "skill_store",
                        ):
                            behavior_evaluator.skill_store = skill_store
                        evidence_adapter = self.state.evidence_runtime_adapter
                        set_evidence_sink = getattr(skill_store, "set_evidence_sink", None)
                        if evidence_adapter is not None and callable(set_evidence_sink):
                            set_evidence_sink(
                                getattr(evidence_adapter, "on_skill_store_event", None)
                            )
                        await skill_store.sync_from_registry(
                            self.state.skill_registry.list_skills()
                        )
                        self.register_skill_evidence_read_roots()

                    self.state.grounding_agent._skill_store = skill_store
                    logger.info("✓ Skill quality store enabled")

                    if config.execution_analysis_sync_start:
                        quality_mgr = (
                            self.state.grounding_client.quality_manager
                            if self.state.grounding_client
                            else None
                        )
                        self.state.execution_analyzer = ExecutionAnalyzer(
                            store=skill_store,
                            llm_client=self.state.llm_client,
                            model=config.execution_analyzer_model,
                            max_tokens=config.execution_analyzer_max_tokens,
                            skill_registry=self.state.skill_registry,
                            quality_manager=quality_mgr,
                        )
                        logger.info("✓ Execution analysis enabled")
                        if self.state.evidence_store is not None:
                            self.state.decision_engine = DecisionEngine(
                                analyzer=self.state.execution_analyzer,
                                evidence_store=self.state.evidence_store,
                            )
                            if self.state.evolution_engine is not None:
                                self.state.evolution_engine.decision_engine = (
                                    self.state.decision_engine
                                )
                            logger.info("✓ Evolution decision engine enabled")

                        self.state.skill_evolver = SkillEvolver(
                            store=skill_store,
                            registry=self.state.skill_registry,
                            llm_client=self.state.llm_client,
                            model=config.skill_evolver_model,
                            max_tokens=config.skill_evolver_max_tokens,
                            max_concurrent=config.evolution_max_concurrent,
                        )
                        if (
                            self.state.evolution_engine is not None
                            and self.state.evidence_store is not None
                        ):
                            evolution_storage_root = resolve_evolution_storage_root(
                                explicit_root=getattr(
                                    config,
                                    "evolution_storage_root",
                                    None,
                                ),
                                explicit_db_path=getattr(
                                    self.state.evidence_store,
                                    "db_path",
                                    getattr(config, "evidence_db_path", None),
                                ),
                                session_storage=self.state.session_storage,
                                skill_store=skill_store,
                                workspace_dir=config.workspace_dir,
                            )
                            self.state.evolution_engine.authoring_backend = (
                                SkillEvolverAuthoringBackend(
                                    self.state.skill_evolver,
                                    evolution_storage_root
                                    / ".openspace"
                                    / "evolution"
                                    / "staging",
                                    self.state.evidence_store,
                                )
                            )
                            self.state.evolution_engine.committer = EvolutionCommitter(
                                evidence_store=self.state.evidence_store,
                                skill_store=skill_store,
                                registry=self.state.skill_registry,
                                trigger_engine=self.state.trigger_engine,
                                backup_root=(
                                    evolution_storage_root
                                    / ".openspace"
                                    / "evolution"
                                    / "backups"
                                ),
                            )
                            self.register_evidence_read_roots(
                                evolution_storage_root / ".openspace" / "evolution",
                            )
                        logger.info(
                            "✓ Skill evolution enabled (concurrent=%s)",
                            config.evolution_max_concurrent,
                        )
                    elif config.enable_recording:
                        logger.info(
                            "Execution analysis and skill evolution skipped by profile"
                        )
                except Exception as exc:
                    logger.warning("Skill quality init failed (non-fatal): %s", exc)
            elif self.state.skill_registry:
                logger.info("Skill quality store sync skipped by profile")

            if self.state.evidence_store is not None:
                recovery = EvolutionRecovery(
                    evidence_store=self.state.evidence_store,
                    skill_store=self.state.skill_store,
                    registry=self.state.skill_registry,
                    trigger_engine=self.state.trigger_engine,
                    stale_job_timeout_s=getattr(
                        config,
                        "evolution_recovery_stale_job_timeout_s",
                        30 * 60,
                    ),
                    staging_retention_s=getattr(
                        config,
                        "evolution_staging_retention_s",
                        7 * 24 * 60 * 60,
                    ),
                )
                result = await run_startup_evolution_recovery(
                    recovery,
                    event_sink=self.emit_runtime_event,
                )
                if result is not None:
                    logger.info("✓ Evolution startup recovery: %s", result.to_dict())
                await self.maybe_drain_startup_retryable_evolution_jobs()

            self.propagate_service_hooks()

            try:
                start_background_housekeeping(
                    {
                        "cwd": config.workspace_dir or str(Path.cwd()),
                        "event_sink": self.emit_runtime_event,
                    },
                    event_sink=self.emit_runtime_event,
                )
                logger.info("✓ Background housekeeping initialized")
            except Exception:
                logger.debug("Background housekeeping init failed", exc_info=True)

            self.mark_initialized()
            logger.info("=" * 60)
            logger.info("OpenSpace ready to use!")
            logger.info("=" * 60)

        except Exception as exc:
            logger.error("Failed to initialize OpenSpace: %s", exc)
            await self.cleanup_resources()
            raise

    async def cleanup_resources(self) -> None:
        """Close runtime-owned services and release process resources."""

        logger.info("Cleaning up OpenSpace resources...")

        try:
            cleanup_context = (
                self.memory_cleanup_context or self.build_memory_cleanup_context()
            )
            try:
                from openspace.services.runtime_support.background import stop_background_housekeeping

                async def _timeout_event_sink(
                    event_type: str,
                    data: dict[str, Any],
                ) -> None:
                    payload = dict(data)
                    payload["reason"] = "cleanup"
                    await self.event_bus.emit(event_type, payload)

                await stop_background_housekeeping(
                    cleanup_context,
                    timeout_s=_CLEANUP_MEMORY_DRAIN_TIMEOUT_S,
                    event_sink=_timeout_event_sink,
                    cancel_pending=True,
                )
            except Exception:
                logger.debug("Background housekeeping stop failed", exc_info=True)

            await self.drain_post_execution_tasks()

            if self.state.skill_evolver:
                await self.state.skill_evolver.wait_background()

            if self.state.multi_agent:
                await self.state.multi_agent.shutdown()

            if self.scheduler:
                await self.scheduler.stop()
                self.scheduler = None

            await shutdown_lsp_server_manager()

            if self.state.grounding_client:
                await self.state.grounding_client.close_all_sessions()
                logger.debug("All grounding sessions closed")

            recording_manager = self.state.recording_manager
            if recording_manager and recording_manager.recording_status:
                try:
                    await recording_manager.stop()
                    logger.debug("Recording manager stopped")
                except Exception as exc:
                    logger.warning("Failed to stop recording: %s", exc)

            if self.state.execution_analyzer:
                try:
                    self.state.execution_analyzer.close()
                    logger.debug("Execution analyzer closed")
                except Exception as exc:
                    logger.debug("Failed to close execution analyzer: %s", exc)

            if self.state.trigger_engine:
                try:
                    close = getattr(self.state.trigger_engine, "close", None)
                    if callable(close):
                        close()
                    logger.debug("Trigger engine closed")
                except Exception as exc:
                    logger.debug("Failed to close trigger engine: %s", exc)

            if self.state.candidate_store:
                try:
                    close = getattr(self.state.candidate_store, "close", None)
                    if callable(close):
                        close()
                    logger.debug("Evolution candidate store closed")
                except Exception as exc:
                    logger.debug("Failed to close candidate store: %s", exc)

            if self.state.evidence_store:
                try:
                    self.state.evidence_store.close()
                    logger.debug("Evidence store closed")
                except Exception as exc:
                    logger.debug("Failed to close evidence store: %s", exc)

            self.mark_uninitialized()

            logger.info("OpenSpace cleanup complete")

        except Exception as exc:
            logger.error("Error during cleanup: %s", exc, exc_info=True)

    def propagate_service_hooks(self) -> None:
        """Pass runtime event hooks to initialized sub-components."""

        if self.state.grounding_agent:
            self.state.grounding_agent.set_tui_bridge(
                self.state.event_proxy
                if self.state.tui_bridge is not None
                else None
            )
            self.state.grounding_agent.set_runtime_event_sink(self.emit_runtime_event)
        if self.state.llm_client:
            self.state.llm_client.set_event_callback(self.emit)
            self.state.llm_client.set_usage_callback(self.record_llm_usage)
        if self.state.multi_agent:
            self.state.multi_agent.set_event_sink(self.emit_runtime_event)
        if self.scheduler:
            self.scheduler.event_sink = self.emit_runtime_event
            self.scheduler.notification_service.event_sink = self.emit_runtime_event
            self.scheduler.approval_service.event_sink = self.emit_runtime_event

    def init_skill_registry(self) -> Any | None:
        """Build and populate the runtime SkillRegistry from configured roots."""

        from openspace.runtime.skill_registry import build_skill_registry

        config = self.config
        skill_cfg = (
            self.state.grounding_config.skills
            if self.state.grounding_config
            else None
        )
        return build_skill_registry(
            workspace_dir=config.workspace_dir,
            configured_skill_dirs=getattr(skill_cfg, "skill_dirs", None),
            metadata_only_discovery=config.skill_metadata_only_discovery,
        )

    def session_storage_config_home(self) -> Path | None:
        configured = getattr(self.config, "session_storage_dir", None)
        if not configured:
            return None
        return Path(configured).expanduser().resolve()

    def session_cwd_from_metadata(
        self,
        metadata: dict[str, Any] | None,
    ) -> str:
        data = metadata or {}
        worktree = data.get("worktree")
        if not isinstance(worktree, dict):
            worktree = {}
        return str(
            data.get("cwd")
            or data.get("project_path")
            or data.get("workspace_dir")
            or worktree.get("workspace_dir")
            or worktree.get("worktree_path")
            or self.config.workspace_dir
            or os.getcwd()
        )

    def ensure_session_storage(
        self,
        session_id: str | None = None,
        *,
        metadata: dict[str, Any] | None = None,
        create: bool = True,
    ) -> SessionStorage:
        sid = session_id or self.current_session_id
        if not sid:
            storage = SessionStorage.create_new(
                cwd=self.session_cwd_from_metadata(metadata),
                model=self.config.llm_model,
                config_home=self.session_storage_config_home(),
                metadata=metadata,
            )
            self.session_storage = storage
            self.current_session_id = storage.session_id
            metadata_dict = storage.metadata.to_dict()
            metadata_dict["session_dir"] = str(storage.session_dir)
            metadata_dict["transcript_path"] = str(storage.transcript_path)
            self.current_session_metadata = metadata_dict
            self.configure_file_history()
            return storage

        if (
            self.session_storage is not None
            and self.session_storage.session_id == str(sid)
        ):
            return self.session_storage

        storage = SessionStorage.for_session(
            str(sid),
            cwd=self.session_cwd_from_metadata(metadata),
            config_home=self.session_storage_config_home(),
            create=create,
        )
        self.session_storage = storage
        return storage

    async def restore_canonical_session(self, session_id: str) -> dict[str, Any]:
        from openspace.services.session.restore import restore_session as restore_runtime_session

        restored_session = await restore_runtime_session(
            session_id,
            cwd=os.getcwd(),
            allow_cross_project=True,
            config_home=self.session_storage_config_home(),
        )
        return restored_session.to_dict()

    async def save_canonical_session_messages(
        self,
        session_id: str,
        metadata: dict[str, Any],
        messages: list[dict[str, Any]],
        *,
        replace: bool = False,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        storage = self.ensure_session_storage(session_id, metadata=metadata)
        metadata_with_cost = dict(metadata)
        metadata_with_cost["cost"] = self.build_cost_snapshot()
        if replace:
            await storage.replace_messages(
                messages,
                model=self.config.llm_model,
                metadata_patch=metadata_with_cost,
            )
        else:
            await storage.save_turn(
                messages,
                model=self.config.llm_model,
                metadata_patch=metadata_with_cost,
            )
        loaded = storage.load()
        self.current_session_id = storage.session_id
        self.current_session_metadata = dict(loaded.metadata)
        return dict(loaded.metadata), list(loaded.messages)

    async def prepare_session(self, execution_context: dict[str, Any]) -> str:
        """Create or restore the runtime session for this execution."""

        requested_session = execution_context.get("session_id")
        self.cost_tracker = CostTracker()

        if requested_session:
            try:
                restored = await self.restore_session(str(requested_session))
                if restored["messages"] and not execution_context.get(
                    "conversation_history"
                ):
                    execution_context["conversation_history"] = restored["messages"]
                self.apply_restored_runtime(restored, execution_context)
                self.remember_memory_cleanup_context(execution_context)
                return str(requested_session)
            except FileNotFoundError:
                logger.warning(
                    "Requested session %s not found, creating a new session",
                    requested_session,
                )

        self.session_storage = SessionStorage.create_new(
            cwd=os.getcwd(),
            model=self.config.llm_model,
            config_home=self.session_storage_config_home(),
            metadata={
                "mode": "default",
                "runtime": {"model": self.config.llm_model},
                "persistence": {"source": "openspace.persistence.SessionStorage"},
            },
        )
        metadata = self.session_storage.metadata.to_dict()
        metadata["session_dir"] = str(self.session_storage.session_dir)
        metadata["transcript_path"] = str(self.session_storage.transcript_path)
        self.current_session_id = self.session_storage.session_id
        self.current_session_metadata = metadata
        self.configure_file_history()
        self.remember_memory_cleanup_context()
        return self.session_storage.session_id

    async def persist_session(
        self,
        final_result: dict[str, Any],
        execution_context: dict[str, Any],
    ) -> None:
        """Persist the latest session snapshot after an execution."""

        if not self.current_session_id or not self.current_session_metadata:
            return

        messages = final_result.get("messages")
        if not isinstance(messages, list):
            messages = execution_context.get("conversation_history", [])
            if not isinstance(messages, list):
                messages = []

        metadata = self.build_session_record(
            dict(self.current_session_metadata),
            [message for message in messages if isinstance(message, dict)],
            execution_context=execution_context,
            final_result=final_result,
        )
        self.current_session_metadata = metadata

        await self.save_canonical_session_messages(
            self.current_session_id,
            metadata,
            [message for message in messages if isinstance(message, dict)],
        )

    async def restore_session(self, session_id: str) -> dict[str, Any]:
        """Restore persisted session state into the active runtime."""

        restored = await self.restore_canonical_session(session_id)
        restored_record = restored.get("session_record")
        restored_metadata = (
            dict(restored_record) if isinstance(restored_record, dict) else {}
        )
        self.session_storage = SessionStorage.for_session(
            str(restored.get("session_id") or session_id),
            cwd=restored_metadata.get("cwd")
            or restored_metadata.get("project_path")
            or os.getcwd(),
            config_home=self.session_storage_config_home(),
            create=True,
        )
        self.current_session_id = str(restored.get("session_id") or session_id)
        self.current_session_metadata = restored_metadata

        self.cost_tracker = CostTracker()
        if restored.get("cost"):
            self.cost_tracker.restore(restored.get("cost"))

        self.configure_file_history(restored.get("file_history_snapshots"))
        self.restore_workspace_from_restored_session(restored)
        self.apply_restored_runtime(restored, None)
        self.remember_memory_cleanup_context()
        return restored

    async def load_session_snapshot(self, session_id: str) -> dict[str, Any]:
        """Load persisted session data without mutating runtime or workspace."""

        return await self.restore_canonical_session(session_id)

    def configure_file_history(self, snapshots: Any | None = None) -> None:
        """Attach the per-file history service to the active SessionStorage."""

        if self.session_storage is None:
            self.file_history = None
            return
        history = FileHistory(session_storage=self.session_storage)
        if snapshots is None:
            try:
                snapshots = self.session_storage.load().file_history_snapshots
            except Exception:
                snapshots = None
        if isinstance(snapshots, list):
            history.restore_state(snapshots)
        self.file_history = history

    async def fork_session(self, session_id: str) -> dict[str, Any]:
        """Fork a session while preserving OpenSpace SessionStorage transcripts."""

        from openspace.services.session.restore import restore_session as restore_runtime_session

        forked = await restore_runtime_session(
            session_id,
            cwd=os.getcwd(),
            fork=True,
            allow_cross_project=True,
            config_home=self.session_storage_config_home(),
        )
        restored = forked.to_dict()
        self.session_storage = SessionStorage.for_session(
            forked.session_id,
            cwd=os.getcwd(),
            config_home=self.session_storage_config_home(),
            create=True,
        )
        self.current_session_id = forked.session_id
        metadata = restored.get("session_record")
        self.current_session_metadata = (
            dict(metadata) if isinstance(metadata, dict) else {}
        )
        self.cost_tracker = CostTracker()
        if restored.get("cost"):
            self.cost_tracker.restore(restored.get("cost"))
        self.configure_file_history(restored.get("file_history_snapshots"))
        self.apply_restored_runtime(restored, None)
        self.remember_memory_cleanup_context()
        return restored

    async def rewind_session(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Replace a session transcript with a rewound message list."""

        if session_id != self.current_session_id or not self.current_session_metadata:
            await self.restore_session(session_id)

        normalized_messages = [
            message for message in messages if isinstance(message, dict)
        ]
        session_record = self.build_session_record(
            dict(self.current_session_metadata or {}),
            normalized_messages,
            execution_context=None,
        )
        session_record["last_task_id"] = None
        session_record["last_status"] = "rewound"
        runtime = dict(session_record.get("runtime", {}))
        runtime.pop("active_task_id", None)
        runtime["phase"] = "rewound"
        session_record["runtime"] = runtime

        from openspace.services.session.restore import rewind_session as rewind_canonical_session

        restored_session = await rewind_canonical_session(
            session_id,
            normalized_messages,
            cwd=(
                session_record.get("cwd")
                or session_record.get("project_path")
                or os.getcwd()
            ),
            config_home=self.session_storage_config_home(),
            model=self.config.llm_model,
            metadata_patch=session_record,
            cost=self.build_cost_snapshot(),
            allow_cross_project=True,
        )
        restored = restored_session.to_dict()
        self.session_storage = SessionStorage.for_session(
            restored_session.session_id,
            cwd=restored_session.session_record.get("cwd")
            or restored_session.session_record.get("project_path")
            or os.getcwd(),
            config_home=self.session_storage_config_home(),
            create=True,
        )
        self.current_session_metadata = dict(restored_session.session_record)
        self.apply_restored_runtime(restored, None)
        return restored

    async def discover_sessions(self, **kwargs: Any) -> dict[str, Any]:
        """Discover resumable canonical sessions."""

        from openspace.services.session.restore import discover_sessions as discover_runtime_sessions

        normalized_page = max(0, int(kwargs.get("page") or 0))
        page_size = max(1, int(kwargs.get("page_size") or 20))

        discovered = await discover_runtime_sessions(
            os.getcwd(),
            page=normalized_page,
            page_size=page_size,
            limit=kwargs.get("limit", 50),
            all_projects=bool(kwargs.get("all_projects", False)),
            config_home=self.session_storage_config_home(),
        )
        return discovered.to_dict()

    async def save_current_session(
        self,
        session_name: str | None = None,
    ) -> dict[str, Any]:
        """Persist the currently active session snapshot."""

        if not self.current_session_id or not self.current_session_metadata:
            storage = self.ensure_session_storage(
                metadata={
                    "mode": "default",
                    "runtime": {"model": self.config.llm_model},
                }
            )
            metadata = storage.metadata.to_dict()
            self.current_session_metadata = dict(metadata)
        else:
            storage = self.ensure_session_storage(
                self.current_session_id,
                metadata=dict(self.current_session_metadata or {}),
            )

        metadata = dict(self.current_session_metadata or {})
        try:
            loaded = storage.load()
            loaded_metadata = dict(loaded.metadata)
            loaded_metadata.update(metadata)
            metadata = loaded_metadata
            messages = list(loaded.messages)
        except FileNotFoundError:
            messages = []
        if not isinstance(messages, list):
            messages = []

        if session_name:
            metadata["title"] = session_name

        session_record = self.build_session_record(
            metadata,
            [message for message in messages if isinstance(message, dict)],
            execution_context=None,
        )
        if self.current_session_id is None:
            self.current_session_id = storage.session_id
        await self.save_canonical_session_messages(
            self.current_session_id,
            session_record,
            [message for message in messages if isinstance(message, dict)],
        )

        return {
            "session_id": self.current_session_id,
            "name": session_record.get("title"),
            "message_count": len(messages),
            "cost_usd": self.cost_tracker.get_total(),
        }

    def remember_memory_cleanup_context(
        self,
        context: dict[str, Any] | None = None,
    ) -> None:
        cleanup_context = self.build_memory_cleanup_context(context)
        if cleanup_context is not None:
            self.state.memory_cleanup_context = cleanup_context

    def build_memory_cleanup_context(
        self,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        source = context or {}
        metadata = self.current_session_metadata or {}

        session_id_value = source.get("session_id") or self.current_session_id
        session_id = (
            str(session_id_value).strip()
            if session_id_value is not None
            else None
        )
        if not session_id:
            session_id = None

        session_dir_value = source.get("session_dir")
        session_dir = (
            str(session_dir_value).strip()
            if session_dir_value is not None
            else None
        )
        if not session_dir and self.session_storage is not None:
            session_dir = str(self.session_storage.session_dir)

        cwd_value = source.get("cwd") or source.get("workspace_dir")
        if cwd_value is None:
            worktree = metadata.get("worktree")
            if not isinstance(worktree, dict):
                worktree = {}
            custom_metadata = metadata.get("metadata")
            if not isinstance(custom_metadata, dict):
                custom_metadata = {}
            cwd_value = (
                metadata.get("workspace_dir")
                or worktree.get("workspace_dir")
                or custom_metadata.get("workspace_dir")
                or self.config.workspace_dir
            )
        cwd = str(cwd_value).strip() if cwd_value is not None else None
        if not cwd:
            cwd = None

        cleanup_context: dict[str, Any] = {}
        if session_id is not None:
            cleanup_context["session_id"] = session_id
        if session_dir is not None:
            cleanup_context["session_dir"] = session_dir
        if cwd is not None:
            cleanup_context["cwd"] = cwd
        return cleanup_context or None

    def build_cost_snapshot(self) -> dict[str, Any]:
        return self.cost_tracker.snapshot()

    def build_session_record(
        self,
        metadata: dict[str, Any],
        messages: list[dict[str, Any]],
        execution_context: dict[str, Any] | None,
        final_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = dict(metadata)
        custom_metadata = record.get("metadata")
        if not isinstance(custom_metadata, dict):
            custom_metadata = {}

        runtime = record.get("runtime")
        if not isinstance(runtime, dict):
            runtime = {}

        worktree = record.get("worktree")
        if not isinstance(worktree, dict):
            worktree = {}

        context = execution_context or {}
        result = final_result or {}
        workspace_dir = context.get("workspace_dir")
        project_path = str(record.get("project_path") or Path.cwd())
        worktree_path = str(record.get("worktree_path") or workspace_dir or project_path)

        record["turn_count"] = int(record.get("turn_count", 0)) + (
            1 if final_result else 0
        )
        record["message_count"] = len(messages)
        record["last_task_id"] = result.get("task_id", record.get("last_task_id"))
        record["last_status"] = result.get("status", record.get("last_status"))
        record["model"] = self.config.llm_model
        record["title"] = (
            record.get("title")
            or record.get("name")
            or _derive_session_title(messages)
        )
        record["project_path"] = project_path
        record["worktree_path"] = worktree_path
        if workspace_dir:
            record["workspace_dir"] = workspace_dir
        record["mode"] = record.get("mode") or custom_metadata.get("mode") or "default"

        custom_metadata.update(
            {
                "workspace_dir": workspace_dir or custom_metadata.get("workspace_dir"),
                "skills_used": result.get("skills_used", []),
            }
        )
        record["metadata"] = custom_metadata

        breakdown = self.cost_tracker.get_breakdown()
        total_input = sum(item["input_tokens"] for item in breakdown.values())
        total_output = sum(item["output_tokens"] for item in breakdown.values())
        total_cache_read = sum(
            item.get("cache_read_input_tokens", 0)
            for item in breakdown.values()
        )
        total_cache_creation = sum(
            item.get("cache_creation_input_tokens", 0)
            for item in breakdown.values()
        )
        total_reasoning = sum(
            item.get("reasoning_tokens", 0)
            for item in breakdown.values()
        )
        runtime.update(
            {
                "session_id": self.current_session_id,
                "model": self.config.llm_model,
                "cost_usd": self.cost_tracker.get_total(),
                "input_tokens": total_input,
                "output_tokens": total_output,
                "cache_read_input_tokens": total_cache_read,
                "cache_creation_input_tokens": total_cache_creation,
                "reasoning_tokens": total_reasoning,
                "unknown_model_cost": self.cost_tracker.has_unknown_model_cost(),
            }
        )
        if result.get("task_id"):
            runtime["active_task_id"] = result["task_id"]
        if context.get("max_iterations") is not None:
            runtime["max_iterations"] = context.get("max_iterations")
        if result.get("status"):
            runtime["phase"] = result["status"]
        record["runtime"] = runtime

        worktree.update(
            {
                "project_path": project_path,
                "worktree_path": worktree_path,
                "workspace_dir": (
                    workspace_dir or worktree.get("workspace_dir") or worktree_path
                ),
            }
        )
        record["worktree"] = worktree
        record.setdefault("file_history_snapshots", [])
        record.setdefault("content_replacements", [])
        return record

    def apply_restored_runtime(
        self,
        restored: dict[str, Any],
        execution_context: dict[str, Any] | None,
    ) -> None:
        session_record = restored.get("session_record")
        if not isinstance(session_record, dict):
            return

        runtime = restored.get("runtime")
        if not isinstance(runtime, dict):
            runtime = {}

        model = runtime.get("model") or session_record.get("model")
        if isinstance(model, str) and model:
            self.config.llm_model = model
            if self.state.llm_client is not None:
                self.state.llm_client.model = model

        workspace_dir = (
            session_record.get("workspace_dir")
            or (session_record.get("worktree") or {}).get("workspace_dir")
            or (session_record.get("metadata") or {}).get("workspace_dir")
        )
        if execution_context is not None and workspace_dir:
            execution_context.setdefault("workspace_dir", workspace_dir)

        agent_type = session_record.get("agent_type")
        if not agent_type and isinstance(session_record.get("agent"), dict):
            agent_type = session_record["agent"].get("type")
        if execution_context is not None and isinstance(agent_type, str) and agent_type:
            execution_context.setdefault("agent_type", agent_type)

        todo_state = runtime.get("todo_state")
        if execution_context is not None and isinstance(todo_state, dict):
            execution_context["todo_state"] = {
                str(key): list(value) if isinstance(value, list) else []
                for key, value in todo_state.items()
            }

    def restore_workspace_from_restored_session(
        self,
        restored: dict[str, Any],
    ) -> None:
        """Canonical recovery uses per-file history; no workspace snapshot path."""

        del restored
        return

    async def record_llm_usage(
        self,
        model: str,
        usage: TokenUsage,
    ) -> None:
        """Track cumulative usage and surface it as status updates."""

        await self.cost_tracker.add_usage(model, usage)
        breakdown = self.cost_tracker.get_breakdown()
        total_input = sum(item["input_tokens"] for item in breakdown.values())
        total_output = sum(item["output_tokens"] for item in breakdown.values())
        total_cache_read = sum(
            item.get("cache_read_input_tokens", 0)
            for item in breakdown.values()
        )
        total_cache_creation = sum(
            item.get("cache_creation_input_tokens", 0)
            for item in breakdown.values()
        )
        total_reasoning = sum(
            item.get("reasoning_tokens", 0)
            for item in breakdown.values()
        )
        await self.emit(
            "status_update",
            {
                "phase": "llm_usage",
                "session_id": self.current_session_id,
                "model": model,
                "input_tokens": total_input,
                "output_tokens": total_output,
                "cache_read_input_tokens": total_cache_read,
                "cache_creation_input_tokens": total_cache_creation,
                "reasoning_tokens": total_reasoning,
                "cost_usd": self.cost_tracker.get_total(),
                "unknown_model_cost": self.cost_tracker.has_unknown_model_cost(),
            },
        )
        await self.emit_runtime_event(
            "background_session_update",
            {
                "session_id": self.current_session_id,
                "status": "running" if self.state.running else "idle",
                "active_agent_id": "primary",
                "metadata": {
                    "model": model,
                    "input_tokens": total_input,
                    "output_tokens": total_output,
                    "cache_read_input_tokens": total_cache_read,
                    "cache_creation_input_tokens": total_cache_creation,
                    "reasoning_tokens": total_reasoning,
                    "cost_usd": self.cost_tracker.get_total(),
                    "unknown_model_cost": self.cost_tracker.has_unknown_model_cost(),
                },
            },
        )

    def get_runtime_status(self) -> dict[str, Any]:
        """Return the current runtime snapshot for TUI status sync."""

        breakdown = self.cost_tracker.get_breakdown()
        total_input = sum(item["input_tokens"] for item in breakdown.values())
        total_output = sum(item["output_tokens"] for item in breakdown.values())
        total_cache_read = sum(
            item.get("cache_read_input_tokens", 0)
            for item in breakdown.values()
        )
        total_cache_creation = sum(
            item.get("cache_creation_input_tokens", 0)
            for item in breakdown.values()
        )
        total_reasoning = sum(
            item.get("reasoning_tokens", 0)
            for item in breakdown.values()
        )
        status = {
            "model": self.config.llm_model,
            "session_id": self.current_session_id,
            "cost_usd": self.cost_tracker.get_total(),
            "input_tokens": total_input,
            "output_tokens": total_output,
            "cache_read_input_tokens": total_cache_read,
            "cache_creation_input_tokens": total_cache_creation,
            "reasoning_tokens": total_reasoning,
            "unknown_model_cost": self.cost_tracker.has_unknown_model_cost(),
            "reasoning_effort": self.reasoning_effort or "auto",
        }
        sandbox = self.get_sandbox_runtime_status()
        if sandbox is not None:
            status["sandbox"] = sandbox
        return status

    def get_sandbox_runtime_status(self) -> dict[str, Any] | None:
        try:
            from openspace.services.sandbox import (
                build_sandbox_status,
                get_process_sandbox_manager,
            )

            cwd = self.config.workspace_dir
            metadata = self.current_session_metadata
            if not cwd and isinstance(metadata, dict):
                worktree = metadata.get("worktree")
                if isinstance(worktree, dict):
                    workspace_dir = worktree.get("workspace_dir")
                    if isinstance(workspace_dir, str) and workspace_dir.strip():
                        cwd = workspace_dir
                if not cwd:
                    for key in ("workspace_dir", "project_path", "worktree_path"):
                        value = metadata.get(key)
                        if isinstance(value, str) and value.strip():
                            cwd = value
                            break
            cwd = cwd or os.getcwd()
            manager = get_process_sandbox_manager(cwd=cwd)
            return build_sandbox_status(manager)
        except Exception as exc:
            logger.debug(f"Unable to build sandbox runtime status: {exc}")
            return None

    def add_post_execution_task(self, task: asyncio.Task[Any]) -> None:
        self.state.post_execution_tasks.add(task)
        task.add_done_callback(self.state.post_execution_tasks.discard)

    async def drain_post_execution_tasks(self) -> None:
        if not self.state.post_execution_tasks:
            return
        tasks = list(self.state.post_execution_tasks)
        drain = asyncio.gather(*tasks, return_exceptions=True)
        timeout_s = self.post_execution_timeout_s()
        if timeout_s > 0:
            try:
                await asyncio.wait_for(drain, timeout=timeout_s)
            except asyncio.TimeoutError:
                logger.warning(
                    "Post-execution background drain timed out after %.2fs; "
                    "cancelling unfinished tasks",
                    timeout_s,
                )
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.wait(tasks, timeout=1.0)
        else:
            await drain
        self.state.post_execution_tasks.clear()

    async def drain_memory_background_tasks(
        self,
        *,
        timeout_s: float,
        reason: str,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Drain already-submitted memory background tasks before persistence."""
        if timeout_s <= 0:
            return

        async def _timeout_event_sink(
            event_type: str,
            data: dict[str, Any],
        ) -> None:
            payload = dict(data)
            payload["reason"] = reason
            if "session_id" not in payload and context is not None:
                session_id = context.get("session_id")
                if session_id:
                    payload["session_id"] = session_id
            await self.event_bus.emit(event_type, payload)

        try:
            from openspace.services.runtime_support.background import drain_background_tasks

            drain_result = await drain_background_tasks(
                timeout_s=timeout_s,
                event_sink=_timeout_event_sink,
                context=context,
            )
            payload = drain_result.as_event_payload()
            payload["reason"] = reason
            payload["timed_out"] = bool(drain_result.timed_out)
            if context is not None:
                if "session_id" not in payload and context.get("session_id"):
                    payload["session_id"] = context.get("session_id")
                if context.get("task_id"):
                    payload["task_id"] = context.get("task_id")
            await self.event_bus.emit("background_drain", payload)
        except Exception:
            logger.debug(
                "Memory background drain failed during %s",
                reason,
                exc_info=True,
            )

    def post_execution_mode(self) -> str:
        mode = str(self.config.post_execution_mode or "inline").strip().lower()
        if mode not in {"inline", "background", "disabled"}:
            logger.warning(
                "Unknown post_execution_mode=%r; falling back to inline",
                self.config.post_execution_mode,
            )
            return "inline"
        return mode

    def post_execution_timeout_s(self) -> float:
        try:
            return max(
                0.0,
                float(getattr(self.config, "post_execution_timeout_s", 0.0) or 0.0),
            )
        except (TypeError, ValueError):
            logger.warning(
                "Invalid post_execution_timeout_s=%r; disabling timeout",
                getattr(self.config, "post_execution_timeout_s", None),
            )
            return 0.0

    async def run_post_execution_tasks(
        self,
        task_id: str,
        recording_dir: str | None,
        result: dict[str, Any],
        *,
        evolved_skills: list[dict[str, Any]] | None = None,
        capture_skill_dir: str | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        task_evolved_skills = evolved_skills if evolved_skills is not None else []
        del capture_skill_dir
        if self.state.evolution_engine is not None and self.state.trigger_engine is not None:
            jobs = self._ensure_analysis_trigger_jobs(task_id, session_id=session_id)
            outcomes = []
            if jobs:
                outcomes = await self.drain_evolution_jobs(
                    job_ids=[job.job_id for job in jobs],
                    limit=len(jobs),
                )
            for outcome in outcomes:
                for record in getattr(outcome, "evolved_skill_records", []) or []:
                    task_evolved_skills.append(
                        self.evolved_skill_record_from_evolution(record)
                    )
        else:
            logger.debug(
                "Post-execution skill evolution skipped: evolution engine unavailable"
            )
            await self._run_legacy_execution_analysis(
                task_id,
                recording_dir=recording_dir,
                result=result,
            )
        quality_outcomes = await self.maybe_evolve_quality()
        for outcome in quality_outcomes:
            for record in getattr(outcome, "evolved_skill_records", []) or []:
                task_evolved_skills.append(
                    self.evolved_skill_record_from_evolution(record)
                )
        final_outcomes = await self.maybe_drain_final_evolution_jobs(
            task_id=task_id,
            session_id=session_id,
        )
        for outcome in final_outcomes:
            for record in getattr(outcome, "evolved_skill_records", []) or []:
                task_evolved_skills.append(
                    self.evolved_skill_record_from_evolution(record)
                )
        return task_evolved_skills

    async def _run_legacy_execution_analysis(
        self,
        task_id: str,
        *,
        recording_dir: str | None,
        result: dict[str, Any],
    ) -> None:
        analyzer = self.state.execution_analyzer
        analyze = getattr(analyzer, "analyze_execution", None)
        if analyzer is None or not callable(analyze):
            return
        if not recording_dir:
            logger.debug(
                "Legacy post-execution analysis skipped: recording_dir unavailable"
            )
            return
        try:
            await analyze(task_id, recording_dir, result)
        except Exception:
            logger.debug(
                "Legacy post-execution analysis failed for %s",
                task_id,
                exc_info=True,
            )

    def _ensure_analysis_trigger_jobs(
        self,
        task_id: str,
        *,
        session_id: str | None = None,
    ) -> list[Any]:
        trigger_engine = self.state.trigger_engine
        if trigger_engine is None:
            return []
        try:
            from openspace.skill_engine.evidence import EvidenceScope

            return list(
                trigger_engine.evaluate_checkpoint(
                    "task_session_persisted",
                    EvidenceScope(
                        session_id=session_id or self.current_session_id,
                        task_id=task_id,
                    ),
                )
                or []
            )
        except Exception:
            logger.debug("Analysis trigger job creation skipped", exc_info=True)
            return []

    async def drain_evolution_jobs(
        self,
        *,
        job_ids: list[str] | None = None,
        trigger_types: tuple[str, ...] | None = None,
        scope: Any | None = None,
        claim_statuses: tuple[str, ...] | None = None,
        limit: int = 1,
    ) -> list[Any]:
        trigger_engine = self.state.trigger_engine
        evolution_engine = self.state.evolution_engine
        if trigger_engine is None or evolution_engine is None:
            return []

        worker_id = f"runtime:{self.current_session_id or 'session'}"
        try:
            if job_ids:
                claim_jobs = getattr(trigger_engine, "claim_jobs", None)
                if not callable(claim_jobs):
                    return []
                jobs = list(claim_jobs(job_ids, worker_id=worker_id) or [])
            else:
                try:
                    jobs = list(
                        trigger_engine.claim_next(
                            limit=limit,
                            worker_id=worker_id,
                            trigger_types=trigger_types,
                            scope=scope,
                            claim_statuses=claim_statuses,
                        )
                        or []
                    )
                except TypeError:
                    if (
                        trigger_types is not None
                        or scope is not None
                        or claim_statuses is not None
                    ):
                        return []
                    jobs = list(
                        trigger_engine.claim_next(
                            limit=limit,
                            worker_id=worker_id,
                        )
                        or []
                    )
        except Exception:
            logger.debug("Evolution trigger job claim failed", exc_info=True)
            return []

        outcomes: list[Any] = []
        for job in jobs:
            outcome = None
            try:
                outcome = await evolution_engine.process_job(job)
            except asyncio.CancelledError:
                self._complete_cancelled_evolution_job(
                    trigger_engine=trigger_engine,
                    job=job,
                    outcome=outcome,
                )
                raise
            except Exception as exc:
                logger.debug(
                    "Evolution job processing failed for %s",
                    getattr(job, "job_id", ""),
                    exc_info=True,
                )
                from openspace.skill_engine.evolution import EvolutionRunResult

                outcome = EvolutionRunResult(
                    job_id=str(getattr(job, "job_id", "") or ""),
                    status="failed",
                    decisions=[],
                    admissions=[],
                    candidates=[],
                    actions=[],
                    evolved_skill_records=[],
                    errors=[str(exc)],
                )
            outcomes.append(outcome)
            from openspace.skill_engine.evolution import (
                completion_after_recovery,
                completion_from_outcome,
            )

            completion = completion_from_outcome(outcome)
            try:
                recovered_actions: list[Any] = []
                if completion.needs_recovery:
                    recovered_actions = await self._recover_committing_actions(
                        reason=f"trigger_job:{getattr(job, 'job_id', '')}",
                    )
                    completion = completion_after_recovery(outcome, recovered_actions)
                if not self._trigger_job_already_terminal(job):
                    trigger_engine.complete(
                        job.job_id,
                        status=completion.status,
                        result_ref=completion.result_ref,
                        error=completion.error,
                    )
            except asyncio.CancelledError:
                self._complete_cancelled_evolution_job(
                    trigger_engine=trigger_engine,
                    job=job,
                    outcome=outcome,
                )
                raise
            except Exception:
                logger.debug(
                    "Evolution trigger job completion failed for %s",
                    getattr(job, "job_id", ""),
                    exc_info=True,
                )
        return outcomes

    def _complete_cancelled_evolution_job(
        self,
        *,
        trigger_engine: Any,
        job: Any,
        outcome: Any | None,
    ) -> None:
        job_id = str(getattr(job, "job_id", "") or "")
        if not job_id or self._trigger_job_already_terminal(job):
            return
        status = "failed_retryable"
        result_ref = None
        error = "evolution job cancelled before completion"
        if outcome is not None:
            try:
                from openspace.skill_engine.evolution import completion_from_outcome

                completion = completion_from_outcome(outcome)
                status = completion.status
                result_ref = completion.result_ref
                error = completion.error or error
            except Exception:
                logger.debug(
                    "Evolution cancellation completion mapping failed for %s",
                    job_id,
                    exc_info=True,
                )
        try:
            trigger_engine.complete(
                job_id,
                status=status,
                result_ref=result_ref,
                error=error,
            )
        except Exception:
            logger.debug(
                "Evolution trigger job cancellation completion failed for %s",
                job_id,
                exc_info=True,
            )

    def _trigger_job_already_terminal(self, job: Any) -> bool:
        job_id = str(getattr(job, "job_id", "") or "")
        if not job_id:
            return False
        trigger_engine = self.state.trigger_engine
        store = getattr(trigger_engine, "store", None)
        get_job = getattr(store, "get_job", None)
        if not callable(get_job):
            return False
        try:
            loaded = get_job(job_id)
        except Exception:
            return False
        status = str(getattr(loaded, "status", "") or "").lower()
        return status in {"completed", "failed", "superseded", "rejected"}

    async def _recover_committing_actions(self, *, reason: str) -> list[Any]:
        evolution_engine = self.state.evolution_engine
        recover = getattr(evolution_engine, "recover_committing_actions", None)
        if not callable(recover):
            return []
        try:
            recovered = await recover()
            if recovered:
                logger.info(
                    "Evolution committing action recovery after %s: %s action(s)",
                    reason,
                    len(recovered),
                )
            return list(recovered or [])
        except Exception:
            logger.debug(
                "Evolution committing action recovery failed after %s",
                reason,
                exc_info=True,
            )
            return []

    def schedule_post_execution_tasks(
        self,
        task_id: str,
        recording_dir: str | None,
        result: dict[str, Any],
        *,
        evolved_skills: list[dict[str, Any]] | None = None,
        capture_skill_dir: str | None = None,
    ) -> None:
        if (
            not self.state.evolution_engine
            and not self.state.trigger_engine
            and not self.state.grounding_client
        ):
            return

        task_result = dict(result)
        task_evolved_skills = evolved_skills if evolved_skills is not None else []
        task_capture_skill_dir = capture_skill_dir
        session_id = self.current_session_id
        workspace_dir = self.config.workspace_dir

        async def _runner() -> None:
            await self.run_post_execution_tasks(
                task_id,
                recording_dir,
                task_result,
                evolved_skills=task_evolved_skills,
                capture_skill_dir=task_capture_skill_dir,
                session_id=session_id,
            )

        from openspace.services.runtime_support.background import get_background_supervisor

        supervisor = (
            getattr(self.state.warm_core, "background_supervisor", None)
            if self.state.warm_core is not None
            else None
        ) or get_background_supervisor()

        task = supervisor.submit(
            source="post_execution",
            name="Post Execution",
            description="Background execution analysis and skill evolution",
            task_type="post_execution",
            task_id=f"post-{task_id}",
            context={
                "event_sink": self.event_bus.emit,
                "session_id": session_id,
                "cwd": workspace_dir,
            },
            coro_factory=_runner,
        )
        self.add_post_execution_task(task)

    async def maybe_evolve_quality(self) -> list[Any]:
        """Trigger quality evolution based on global execution count."""
        self.increment_execution_count()
        if not self._quality_cutover_enabled():
            return []

        created_jobs, _signal_path_failed = self._create_quality_signal_trigger_jobs()
        if created_jobs:
            return await self._drain_created_evolution_jobs(created_jobs)
        return []

    def _create_quality_signal_trigger_jobs(self) -> tuple[list[Any], bool]:
        """Create QUALITY_SIGNAL jobs for the runtime cutover path.

        Finalization only records quality_signal_ref evidence; this method is
        the runtime entry that creates jobs which maybe_evolve_quality drains.
        """
        evidence_store = getattr(self.state, "evidence_store", None)
        trigger_engine = getattr(self.state, "trigger_engine", None)
        if evidence_store is None:
            logger.warning("Quality signal cutover path unavailable: missing evidence store")
            return [], True
        if trigger_engine is None:
            logger.warning("Quality signal cutover path unavailable: missing trigger engine")
            return [], True
        latest_watermark = getattr(evidence_store, "latest_manifest_watermark", None)
        if not callable(latest_watermark):
            logger.warning(
                "Quality signal cutover path unavailable: evidence store has no "
                "latest_manifest_watermark API"
            )
            return [], True

        signal_store = None
        try:
            from openspace.skill_engine.signals import (
                QualitySignalReconciler,
                QualitySignalStore,
            )

            since = self._load_quality_signal_cutover_checkpoint()
            until = int(latest_watermark())
            signal_store = QualitySignalStore(evidence_store)
            if bool(getattr(self.config, "quality_signal_reconciliation_enabled", True)):
                reconciler = QualitySignalReconciler(
                    evidence_store,
                    signal_store=signal_store,
                    trigger_engine=None,
                    enabled=True,
                )
                reconciliation = reconciler.scan_window(
                    since_watermark=since,
                    until_watermark=until,
                )
                if self._quality_signal_reconciliation_failed(
                    evidence_store,
                    reconciliation,
                ):
                    return [], True

            if not bool(getattr(self.config, "quality_signal_trigger_enabled", True)):
                logger.warning(
                    "Quality signal cutover path unavailable: signal trigger disabled"
                )
                return [], True

            from_quality_signals = getattr(trigger_engine, "from_quality_signals", None)
            if not callable(from_quality_signals):
                logger.warning(
                    "Quality signal cutover path unavailable: trigger engine has no "
                    "from_quality_signals API"
                )
                return [], True

            trigger_refs = signal_store.list_triggerable_since(since)
            if not trigger_refs:
                self._mark_quality_signal_cutover_checkpoint(
                    int(latest_watermark())
                )
                return [], False

            manifest_watermark = int(latest_watermark())
            jobs = list(
                from_quality_signals(
                    trigger_refs,
                    manifest_watermark=manifest_watermark,
                )
                or []
            )
            self._mark_quality_signal_cutover_checkpoint(
                int(latest_watermark())
            )
            return jobs, False
        except Exception:
            logger.debug("Quality signal cutover path failed", exc_info=True)
            return [], True
        finally:
            close = getattr(signal_store, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    logger.debug("Quality signal cutover store close failed", exc_info=True)

    @staticmethod
    def _quality_signal_reconciliation_failed(
        evidence_store: Any,
        reconciliation: Any,
    ) -> bool:
        metric_ref_id = str(getattr(reconciliation, "metric_window_ref", "") or "")
        if not metric_ref_id:
            logger.warning(
                "Quality signal cutover path unavailable: reconciliation did not "
                "write a metric_window_ref"
            )
            return True
        get_ref = getattr(evidence_store, "get_ref", None)
        if not callable(get_ref):
            return False
        try:
            metric_ref = get_ref(metric_ref_id)
        except Exception:
            logger.warning(
                "Quality signal cutover path unavailable: failed to read "
                "reconciliation metric_window_ref",
                exc_info=True,
            )
            return True
        status = str(getattr(metric_ref, "metadata", {}).get("status") or "")
        if status == "failed":
            logger.warning(
                "Quality signal cutover path unavailable: reconciliation failed"
            )
            return True
        return False

    async def _drain_created_evolution_jobs(self, jobs: list[Any]) -> list[Any]:
        job_ids = [str(getattr(job, "job_id", "") or "") for job in jobs]
        job_ids = [job_id for job_id in dict.fromkeys(job_ids) if job_id]
        if not job_ids:
            return []
        trigger_engine = self.state.trigger_engine
        claim_jobs = getattr(trigger_engine, "claim_jobs", None)
        if callable(claim_jobs):
            return await self.drain_evolution_jobs(
                job_ids=job_ids,
                limit=len(job_ids),
            )
        trigger_types = tuple(
            sorted(
                {
                    str(getattr(job, "trigger_type", "") or "").upper()
                    for job in jobs
                    if str(getattr(job, "trigger_type", "") or "").strip()
                }
            )
        )
        return await self.drain_evolution_jobs(
            trigger_types=trigger_types or None,
            limit=len(job_ids),
        )

    async def maybe_drain_startup_retryable_evolution_jobs(self) -> list[Any]:
        """Optionally retry persisted failed_retryable jobs during startup."""

        limit = int(
            getattr(
                self.config,
                "evolution_startup_retryable_drain_limit",
                0,
            )
            or 0
        )
        rounds = int(
            getattr(
                self.config,
                "evolution_startup_retryable_drain_rounds",
                1,
            )
            or 0
        )
        timeout_s = float(
            getattr(
                self.config,
                "evolution_startup_retryable_drain_timeout_s",
                0.0,
            )
            or 0.0
        )
        raw_statuses = getattr(
            self.config,
            "evolution_startup_retryable_drain_statuses",
            "failed_retryable",
        )
        if isinstance(raw_statuses, str):
            claim_statuses = tuple(
                item.strip() for item in raw_statuses.split(",") if item.strip()
            )
        else:
            claim_statuses = tuple(
                str(item).strip() for item in raw_statuses if str(item).strip()
            )
        if not claim_statuses:
            claim_statuses = ("failed_retryable",)
        if limit <= 0 or rounds <= 0:
            return []
        if self.state.trigger_engine is None or self.state.evolution_engine is None:
            return []

        outcomes: list[Any] = []
        for _ in range(rounds):
            try:
                drain_coro = self.drain_evolution_jobs(
                    claim_statuses=claim_statuses,
                    trigger_types=_GENERAL_EVOLUTION_TRIGGER_TYPES,
                    limit=limit,
                )
                if timeout_s > 0:
                    batch = await asyncio.wait_for(drain_coro, timeout=timeout_s)
                else:
                    batch = await drain_coro
            except asyncio.TimeoutError:
                logger.warning(
                    "Startup retryable evolution drain timed out after %.2fs",
                    timeout_s,
                )
                break
            except Exception:
                logger.debug("Startup retryable evolution drain failed", exc_info=True)
                break

            if not batch:
                break
            outcomes.extend(batch)
            if len(batch) < limit:
                break

        if outcomes:
            logger.info(
                "Startup retryable evolution drain processed %s job(s)",
                len(outcomes),
            )
        return outcomes

    async def maybe_drain_final_evolution_jobs(
        self,
        *,
        task_id: str,
        session_id: str | None = None,
    ) -> list[Any]:
        """Optionally retry open evolution jobs before short-lived runtimes exit."""

        limit = int(getattr(self.config, "evolution_final_drain_limit", 0) or 0)
        rounds = int(getattr(self.config, "evolution_final_drain_rounds", 1) or 0)
        timeout_s = float(
            getattr(self.config, "evolution_final_drain_timeout_s", 0.0) or 0.0
        )
        if limit <= 0 or rounds <= 0:
            return []
        if self.state.trigger_engine is None or self.state.evolution_engine is None:
            return []

        try:
            from openspace.skill_engine.evidence import EvidenceScope

            scope = EvidenceScope(
                session_id=session_id or self.current_session_id,
                task_id=task_id,
            )
        except Exception:
            logger.debug("Final evolution drain scope unavailable", exc_info=True)
            scope = None

        outcomes: list[Any] = []
        for _ in range(rounds):
            try:
                drain_coro = self.drain_evolution_jobs(
                    scope=scope,
                    trigger_types=_GENERAL_EVOLUTION_TRIGGER_TYPES,
                    limit=limit,
                )
                if timeout_s > 0:
                    batch = await asyncio.wait_for(drain_coro, timeout=timeout_s)
                else:
                    batch = await drain_coro
            except asyncio.TimeoutError:
                logger.warning(
                    "Final evolution drain timed out after %.2fs", timeout_s
                )
                break
            except Exception:
                logger.debug("Final evolution drain failed", exc_info=True)
                break

            if not batch:
                break
            outcomes.extend(batch)
            if len(batch) < limit:
                break

        if outcomes:
            logger.info("Final evolution drain processed %s job(s)", len(outcomes))
        return outcomes

    def _load_quality_signal_cutover_checkpoint(self) -> int:
        trigger_engine = self.state.trigger_engine
        for owner in (
            trigger_engine,
            getattr(trigger_engine, "store", None),
        ):
            loader = getattr(owner, "load_checkpoint", None)
            if not callable(loader):
                continue
            try:
                value = loader(_QUALITY_SIGNAL_CUTOVER_CHECKPOINT)
                if value is not None:
                    return max(0, int(value))
            except (TypeError, ValueError):
                return 0
            except Exception:
                logger.debug("Quality signal cutover checkpoint lookup failed", exc_info=True)
                return 0
        return 0

    def _mark_quality_signal_cutover_checkpoint(self, watermark: int) -> None:
        value = max(0, int(watermark))
        trigger_engine = self.state.trigger_engine
        for owner in (
            trigger_engine,
            getattr(trigger_engine, "store", None),
        ):
            saver = getattr(owner, "save_checkpoint", None)
            if not callable(saver):
                continue
            try:
                saver(_QUALITY_SIGNAL_CUTOVER_CHECKPOINT, value)
                return
            except Exception:
                logger.debug("Quality signal cutover checkpoint save failed", exc_info=True)
                return

    def _quality_cutover_enabled(self) -> bool:
        trigger_engine = self.state.trigger_engine
        evolution_engine = self.state.evolution_engine
        if trigger_engine is None or evolution_engine is None:
            return False

        mode = self._quality_evolution_mode()
        if mode not in {"audit_only", "fix_only", "autonomous"}:
            return False

        if self._evolution_component(evolution_engine, "packet_builder", self.state.packet_builder) is None:
            return False
        if self._evolution_component(evolution_engine, "decision_engine", self.state.decision_engine) is None:
            return False
        if self._evolution_component(evolution_engine, "admission_policy", None) is None:
            return False

        if mode == "audit_only":
            return True

        for name in ("authoring_backend", "validator", "behavior_evaluator", "committer"):
            if self._evolution_component(evolution_engine, name, None) is None:
                return False
        return True

    @staticmethod
    def _evolution_component(engine: Any, name: str, fallback: Any = None) -> Any:
        value = getattr(engine, name, None)
        return value if value is not None else fallback

    def _quality_evolution_mode(self) -> str:
        evolution_engine = self.state.evolution_engine
        mode = str(
            getattr(evolution_engine, "evolution_mode", None)
            or getattr(self.config, "evolution_mode", "autonomous")
            or "autonomous"
        ).strip().lower()
        return mode if mode in {"audit_only", "fix_only", "autonomous"} else "autonomous"

    async def ensure_scheduler(
        self,
        workspace_dir: str,
        *,
        task_manager: Any | None = None,
    ) -> Any:
        from openspace.services.scheduler import (
            create_scheduler_for_workspace,
            get_default_schedule_path,
        )

        target_path = get_default_schedule_path(workspace_dir)
        current_path = None
        scheduler = self.scheduler
        if scheduler is not None:
            current_path = getattr(getattr(scheduler, "store", None), "path", None)
        if scheduler is None or current_path != target_path:
            if scheduler is not None:
                await scheduler.stop()
            scheduler = create_scheduler_for_workspace(
                workspace_dir,
                event_sink=self.emit_runtime_event,
                task_manager=task_manager,
            )
            self.scheduler = scheduler
            await scheduler.start()
        elif task_manager is not None:
            scheduler.task_manager = task_manager
        return scheduler

    def should_start_scheduler_for_execute(
        self,
        task: str,
        context: dict[str, Any],
    ) -> bool:
        del task
        config = self.config
        if getattr(config, "scheduler_execute_sync_start", False):
            return True
        if context.get("force_scheduler") or context.get("scheduler_intent"):
            return True
        if self.scheduler is not None:
            return True
        return context.get("scheduler") is not None

    def workspace_has_enabled_schedules(self, workspace_dir: str) -> bool:
        try:
            from openspace.services.scheduler import has_enabled_schedules

            return has_enabled_schedules(workspace_dir)
        except Exception:
            logger.debug("Failed to inspect scheduled tasks", exc_info=True)
            return False

    def append_evolved_skill(self, record: dict[str, Any]) -> None:
        self.state.last_evolved_skills.append(record)

    @staticmethod
    def evolved_skill_record_from_evolution(rec: Any) -> dict[str, Any]:
        return {
            "skill_id": rec.skill_id,
            "name": rec.name,
            "description": rec.description,
            "path": str(rec.path) if rec.path else "",
            "origin": rec.lineage.origin.value,
            "trust_state": rec.trust_state.value,
            "enabled": bool(rec.enabled),
            "trust_successes": rec.trust_successes,
            "trust_failures": rec.trust_failures,
            "generation": rec.lineage.generation,
            "parent_local_skill_ids": rec.lineage.parent_skill_ids,
            "change_summary": rec.lineage.change_summary,
        }

    def increment_execution_count(self) -> int:
        self.state.execution_count += 1
        return self.state.execution_count

    def mark_idle(self) -> None:
        self.state.running = False
        self.state.task_done.set()

    def mark_initialized(self) -> None:
        self.state.initialized = True

    def mark_uninitialized(self) -> None:
        self.state.initialized = False
        self.mark_idle()

    def is_bridge_dispatch_suppressed(self) -> bool:
        try:
            return bool(self._bridge_dispatch_suppressed())
        except Exception:
            return False

    async def emit(self, event_type: str, data: dict[str, Any]) -> None:
        """Emit an event through the runtime event bus."""
        if event_type == "status_update" and "sandbox" not in data:
            sandbox = self.get_sandbox_runtime_status()
            if sandbox is not None:
                data = {**data, "sandbox": sandbox}
        await self.event_bus.emit(event_type, data)

    async def emit_runtime_event(
        self,
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        await self.event_bus.emit(event_type, data)

    def register_event_sink(
        self,
        sink: Callable[[str, dict[str, Any]], Any],
    ) -> None:
        self.state.event_sinks.append(sink)

    def unregister_event_sink(
        self,
        sink: Callable[[str, dict[str, Any]], Any],
    ) -> None:
        if sink in self.state.event_sinks:
            self.state.event_sinks.remove(sink)

    def iter_event_sinks(self) -> list[Callable[[str, dict[str, Any]], Any]]:
        return list(self.state.event_sinks)

    @property
    def capture_skill_dir(self) -> str | None:
        return self.state.capture_skill_dir

    @property
    def memory_cleanup_context(self) -> dict[str, Any] | None:
        return self.state.memory_cleanup_context

    @property
    def is_initialized(self) -> bool:
        return self.state.initialized

    @property
    def is_running(self) -> bool:
        return self.state.running

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        return await self.execution_lifecycle.execute(request)
