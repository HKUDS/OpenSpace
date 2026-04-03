from __future__ import annotations

import asyncio
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from openspace.agents import GroundingAgent
from openspace.llm import LLMClient
from openspace.grounding.core.grounding_client import GroundingClient
from openspace.config import get_config, load_config
from openspace.config.loader import get_agent_config
from openspace.recording import RecordingManager
from openspace.skill_engine import SkillRegistry, ExecutionAnalyzer, SkillStore
from openspace.skill_engine.evolver import SkillEvolver
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


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
    visual_analysis_model: Optional[str] = None  # Model for visual analysis
    
    # Skill Engine Models — names map to class names (None = use llm_model)
    skill_registry_model: Optional[str] = None        # SkillRegistry: skill selection
    execution_analyzer_model: Optional[str] = None    # ExecutionAnalyzer: post-execution analysis
    skill_evolver_model: Optional[str] = None         # (future) SkillEvolver: skill evolution
    
    # Grounding Configuration
    grounding_config_path: Optional[str] = None
    grounding_max_iterations: int = 20
    grounding_system_prompt: Optional[str] = None
    
    # Backend Configuration
    backend_scope: Optional[List[str]] = None  # None = All backends ["shell", "gui", "mcp", "web", "system"]
    use_clawwork_productivity: bool = False  # If True, add ClawWork productivity tools (search_web, create_file, etc.) for fair comparison with ClawWork; requires livebench installed.
    
    # Workspace Configuration
    workspace_dir: Optional[str] = None
    
    # Recording Configuration
    enable_recording: bool = True
    recording_backends: Optional[List[str]] = None
    recording_log_dir: str = "./logs/recordings"
    enable_screenshot: bool = False
    enable_video: bool = False
    enable_conversation_log: bool = True  # Save LLM conversations to conversations.jsonl
    
    # Skill Evolution
    evolution_max_concurrent: int = 3        # Max parallel evolutions per trigger
    
    # Logging Configuration
    log_level: str = "INFO"
    log_to_file: bool = False
    log_file_path: Optional[str] = None
    
    def __post_init__(self):
        """Validate configuration"""
        if not self.llm_model:
            raise ValueError("llm_model is required")
        
        logger.debug(f"OpenSpaceConfig initialized with model: {self.llm_model}")


class OpenSpace:
    def __init__(self, config: Optional[OpenSpaceConfig] = None):
        self.config = config or OpenSpaceConfig()
        
        self._llm_client: Optional[LLMClient] = None
        self._grounding_client: Optional[GroundingClient] = None
        self._grounding_config = None  # GroundingConfig reference for skill settings
        self._grounding_agent: Optional[GroundingAgent] = None
        self._recording_manager: Optional[RecordingManager] = None
        self._skill_registry: Optional[SkillRegistry] = None
        self._skill_store: Optional[SkillStore] = None
        self._execution_analyzer: Optional[ExecutionAnalyzer] = None
        self._skill_evolver: Optional[SkillEvolver] = None
        self._execution_count: int = 0  # For periodic metric-based evolution
        self._last_evolved_skills: List[Dict[str, Any]] = []  # Tracks skills evolved during last execute()
        
        self._initialized = False
        self._running = False
        self._task_done = asyncio.Event()
        self._task_done.set()  # Initially not running, so "done"
        
        logger.debug("OpenSpace instance created")
    
    async def initialize(self) -> None:
        if self._initialized:
            logger.warning("OpenSpace already initialized")
            return
        
        logger.info("Initializing OpenSpace...")
        
        try:
            self._llm_client = LLMClient(
                model=self.config.llm_model,
                enable_thinking=self.config.llm_enable_thinking,
                rate_limit_delay=self.config.llm_rate_limit_delay,
                max_retries=self.config.llm_max_retries,
                timeout=self.config.llm_timeout,
                **self.config.llm_kwargs
            )
            logger.info(f"✓ LLM Client: {self.config.llm_model}")
            
            # Load grounding config
            # If custom config is provided, merge it with default configs
            # load_config supports multiple files and deep merges them (later files override earlier ones)
            if self.config.grounding_config_path:
                from openspace.config.loader import CONFIG_DIR
                from openspace.config.constants import CONFIG_GROUNDING, CONFIG_SECURITY
                # Load default configs + custom config (custom values will override defaults)
                grounding_config = load_config(
                    CONFIG_DIR / CONFIG_GROUNDING,
                    CONFIG_DIR / CONFIG_SECURITY,
                    self.config.grounding_config_path
                )
                logger.info(f"Merged custom grounding config: {self.config.grounding_config_path}")
            else:
                # Load default configs only
                grounding_config = get_config()
            
            # Optional: enable ClawWork productivity tools for fair benchmark comparison
            if getattr(self.config, "use_clawwork_productivity", False):
                shell_cfg = grounding_config.shell.model_copy(
                    update={
                        "use_clawwork_productivity": True,
                        "working_dir": self.config.workspace_dir or grounding_config.shell.working_dir,
                    }
                )
                grounding_config = grounding_config.model_copy(update={"shell": shell_cfg})
                logger.info("ClawWork productivity tools enabled (shell.working_dir used as sandbox root)")
            
            # Resolve backend_scope early so we can skip initializing
            # providers that are not in scope (e.g. web when only shell is needed).
            agent_config = get_agent_config("GroundingAgent")
            _cli_max_iter = self.config.grounding_max_iterations
            _default_max_iter = OpenSpaceConfig().grounding_max_iterations  # dataclass default (20)
            if agent_config:
                cfg_max_iter = agent_config.get("max_iterations", _default_max_iter)
                if _cli_max_iter != _default_max_iter:
                    max_iterations = _cli_max_iter
                else:
                    max_iterations = cfg_max_iter
                backend_scope = self.config.backend_scope or agent_config.get("backend_scope") or ["gui", "shell", "mcp", "web", "system"]
                visual_analysis_timeout = agent_config.get("visual_analysis_timeout", 30.0)
                self.config.grounding_max_iterations = max_iterations
                logger.info(f"Loaded GroundingAgent config from config_agents.json (max_iterations={max_iterations}, visual_analysis_timeout={visual_analysis_timeout}s)")
            else:
                max_iterations = self.config.grounding_max_iterations
                backend_scope = self.config.backend_scope or ["gui", "shell", "mcp", "web", "system"]
                visual_analysis_timeout = 30.0
                logger.warning(f"config_agents.json not found, using default config (max_iterations={max_iterations})")

            # Filter enabled_backends in grounding config to only those in scope,
            # so providers outside scope (e.g. web) are never registered/initialized.
            if grounding_config.enabled_backends:
                scope_set = set(backend_scope)
                filtered = [
                    entry for entry in grounding_config.enabled_backends
                    if entry.get("name", "").lower() in scope_set
                ]
                if len(filtered) != len(grounding_config.enabled_backends):
                    skipped = [
                        entry.get("name") for entry in grounding_config.enabled_backends
                        if entry.get("name", "").lower() not in scope_set
                    ]
                    logger.info(f"Skipping backends not in scope: {skipped}")
                    grounding_config = grounding_config.model_copy(
                        update={"enabled_backends": filtered}
                    )

            self._grounding_config = grounding_config
            self._grounding_client = GroundingClient(config=grounding_config)
            await self._grounding_client.initialize_all_providers()
            
            backends = list(self._grounding_client.list_providers().keys())
            logger.info(f"✓ Grounding Client: {len(backends)} backends")
            logger.debug(f"  Available backends: {[b.value for b in backends]}")
            
            if self.config.enable_recording:
                self._recording_manager = RecordingManager(
                    enabled=True,
                    task_id="",
                    log_dir=self.config.recording_log_dir,
                    backends=self.config.recording_backends,
                    enable_screenshot=self.config.enable_screenshot,
                    enable_video=self.config.enable_video,
                    enable_conversation_log=self.config.enable_conversation_log,
                    agent_name="OpenSpace",
                )
                # Inject recording_manager to grounding_client for GUI intermediate steps
                self._grounding_client.recording_manager = self._recording_manager
                self._recording_manager.register_to_llm(self._llm_client)
                logger.info(f"✓ Recording enabled: {len(self._recording_manager.backends or [])} backends")
            
            # Create separate LLM client for tool retrieval if configured
            # Inherits llm_kwargs (api_key, api_base, etc.) so credentials
            # from the host agent are shared across all internal LLM clients.
            tool_retrieval_llm = None
            if self.config.tool_retrieval_model:
                tool_retrieval_llm = LLMClient(
                    model=self.config.tool_retrieval_model,
                    timeout=self.config.llm_timeout,
                    max_retries=self.config.llm_max_retries,
                    **self.config.llm_kwargs,
                )
                logger.info(f"✓ Tool retrieval LLM: {self.config.tool_retrieval_model}")
            
            self._grounding_agent = GroundingAgent(
                name="OpenSpace-GroundingAgent",
                backend_scope=backend_scope,
                llm_client=self._llm_client,
                grounding_client=self._grounding_client,
                recording_manager=self._recording_manager,
                system_prompt=self.config.grounding_system_prompt,
                max_iterations=max_iterations,
                visual_analysis_timeout=visual_analysis_timeout,
                tool_retrieval_llm=tool_retrieval_llm,
                visual_analysis_model=self.config.visual_analysis_model,
            )
            logger.info(f"✓ GroundingAgent: {', '.join(backend_scope)}")
            
            # Initialize SkillRegistry (settings from config_grounding.json → skills)
            if self._grounding_config and self._grounding_config.skills.enabled:
                self._skill_registry = self._init_skill_registry()
                if self._skill_registry:
                    skills = self._skill_registry.list_skills()
                    logger.info(f"✓ Skills: {len(skills)} discovered")
                    self._grounding_agent.set_skill_registry(self._skill_registry)

            # Initialize ExecutionAnalyzer (requires recording + skills)
            if self.config.enable_recording and self._skill_registry:
                try:
                    skill_store = SkillStore()
                    self._skill_store = skill_store  # Expose for MCP server reuse

                    # Sync filesystem skills → DB (creates initial records
                    # for newly discovered skills so that analysis stats
                    # can be recorded against them from the very first run).
                    await skill_store.sync_from_registry(
                        self._skill_registry.list_skills()
                    )

                    # Bridge: pass quality_manager so analysis can feed back
                    # LLM-identified tool issues to the tool quality system.
                    quality_mgr = (
                        self._grounding_client.quality_manager
                        if self._grounding_client else None
                    )
                    self._execution_analyzer = ExecutionAnalyzer(
                        store=skill_store,
                        llm_client=self._llm_client,
                        model=self.config.execution_analyzer_model,
                        skill_registry=self._skill_registry,
                        quality_manager=quality_mgr,
                    )
                    logger.info("✓ Execution analysis enabled")

                    # Share store with GroundingAgent so retrieve_skill
                    # can access quality metrics for LLM selection.
                    self._grounding_agent._skill_store = skill_store

                    # Initialize SkillEvolver (reuses the same store & registry)
                    # available_tools will be updated before each evolution cycle
                    self._skill_evolver = SkillEvolver(
                        store=skill_store,
                        registry=self._skill_registry,
                        llm_client=self._llm_client,
                        model=self.config.skill_evolver_model,
                        max_concurrent=self.config.evolution_max_concurrent,
                    )
                    logger.info(
                        f"✓ Skill evolution enabled "
                        f"(concurrent={self.config.evolution_max_concurrent})"
                    )
                except Exception as e:
                    logger.warning(f"Execution analyzer init failed (non-fatal): {e}")
            
            self._initialized = True
            logger.info("="*60)
            logger.info("OpenSpace ready to use!")
            logger.info("="*60)
            
        except Exception as e:
            logger.error(f"Failed to initialize OpenSpace: {e}")
            await self.cleanup()
            raise
    
    async def execute(
        self,
        task: str,
        context: Optional[Dict[str, Any]] = None,
        workspace_dir: Optional[str] = None,
        max_iterations: Optional[int] = None,
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Execute a task with OpenSpace.
        
        Args:
            task: Task instruction
            context: Additional context
            workspace_dir: Working directory
            max_iterations: Max iterations override
            task_id: External task ID for recording/logging. If None, generates a random one.
                     This allows external callers (e.g., OSWorld) to specify their own task ID
                     so recordings can be easily matched with benchmark results.
        """
        if not self._initialized:
            raise RuntimeError(
                "OpenSpace not initialized. "
                "Call await tool_layer.initialize() first or use async with."
            )
        
        _TASK_WAIT_TIMEOUT = 660  # slightly longer than MCP tool timeout (600s)
        if self._running:
            logger.info(
                "OpenSpace is busy — waiting up to %ds for the current task to finish...",
                _TASK_WAIT_TIMEOUT,
            )
            try:
                await asyncio.wait_for(
                    self._task_done.wait(), timeout=_TASK_WAIT_TIMEOUT
                )
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"OpenSpace is still running after waiting {_TASK_WAIT_TIMEOUT}s. "
                    "Please try again later."
                )
        
        logger.info("="*60)
        logger.info(f"Task: {task[:100]}...")
        logger.info("="*60)
        
        self._running = True
        self._task_done.clear()
        self._last_evolved_skills = []  # Reset per-execution tracking
        start_time = asyncio.get_event_loop().time()
        # Use external task_id if provided, otherwise generate one
        if task_id is None:
            task_id = f"task_{uuid.uuid4().hex[:12]}"
        logger.info(f"Task ID: {task_id}")

        # Populated inside the try block; used by finally for analysis
        result: Dict[str, Any] = {}

        try:
            execution_context = context or {}
            execution_context["task_id"] = task_id
            execution_context["instruction"] = task
            
            if max_iterations is not None:
                execution_context["max_iterations"] = max_iterations
            
            if self._recording_manager:
                if self._recording_manager.recording_status:
                    await self._recording_manager.stop()
                    logger.debug("Stopped previous recording session")
                
                self._recording_manager.task_id = task_id
                await self._recording_manager.start()
                await self._recording_manager.add_metadata("instruction", task)
                logger.info(f"Recording started: {task_id}")
            
            if workspace_dir:
                execution_context["workspace_dir"] = workspace_dir
                logger.info(f"Workspace: {workspace_dir}")
            elif self.config.workspace_dir:
                execution_context["workspace_dir"] = self.config.workspace_dir
                logger.info(f"Workspace: {self.config.workspace_dir}")
            elif self._recording_manager and self._recording_manager.trajectory_dir:
                execution_context["workspace_dir"] = self._recording_manager.trajectory_dir
                logger.info(f"Workspace: {execution_context['workspace_dir']}")
            else:
                import tempfile
                from pathlib import Path
                workspace = Path(tempfile.gettempdir()) / "openspace_workspace" / task_id
                workspace.mkdir(parents=True, exist_ok=True)
                execution_context["workspace_dir"] = str(workspace)
                logger.info(f"Workspace: {execution_context['workspace_dir']}")

            # Update Shell session's default_working_dir so that
            # productivity tools (create_file, create_video) write to the
            # correct task workspace instead of the global CWD.
            resolved_ws = execution_context["workspace_dir"]
            try:
                from openspace.grounding.core.types import BackendType as _BT
                shell_prov = self._grounding_client._registry.get(_BT.SHELL)
                for sess in shell_prov._sessions.values():
                    sess.default_working_dir = resolved_ws
            except Exception:
                pass
            
            # Resolve iteration budget: use the larger of the caller's value
            # and the configured value so external callers can't accidentally
            # starve the agent with a too-low budget.
            configured_max = self.config.grounding_max_iterations
            if max_iterations:
                max_iterations = max(max_iterations, configured_max)
            else:
                max_iterations = configured_max

            # Two-phase execution: Skill-First → Tool-Fallback
            has_skills = False

            # Phase 1: Skill-guided execution
            if self._skill_registry:
                has_skills = await self._select_and_inject_skills(task)

            if has_skills:
                logger.info(
                    f"[Phase 1 — Skill] Executing with skill guidance "
                    f"(max {max_iterations} iterations)..."
                )
                execution_context_p1 = {**execution_context}
                execution_context_p1["max_iterations"] = max_iterations

                # Snapshot workspace files before skill-guided execution
                workspace_path = execution_context.get("workspace_dir", "")
                pre_skill_files: set = set()
                if workspace_path:
                    try:
                        from pathlib import Path as _P
                        pre_skill_files = {
                            f.name for f in _P(workspace_path).iterdir()
                        } if _P(workspace_path).exists() else set()
                    except Exception:
                        pass

                # Capture skill IDs before they get cleared
                injected_skill_ids = list(self._grounding_agent._active_skill_ids)
                skill_phase_result = await self._grounding_agent.process(execution_context_p1)
                skill_status = skill_phase_result.get("status", "unknown")
                skill_iterations = skill_phase_result.get("iterations", 0)

                # Clear skill context regardless of outcome
                self._grounding_agent.clear_skill_context()

                if skill_status == "success":
                    result = skill_phase_result
                    result["active_skills"] = injected_skill_ids
                    logger.info(
                        f"[Phase 1 — Skill] Completed successfully "
                        f"({skill_iterations} iterations)"
                    )
                else:
                    # Skill failed — fall back to pure tool execution.
                    # Fallback gets the full budget because we clean the
                    # workspace below — it starts completely from scratch
                    # with no skill context and no leftover artifacts.
                    logger.warning(
                        f"[Phase 1 — Skill] {skill_status} after {skill_iterations} iterations, "
                        f"falling back to tool-only execution "
                        f"(budget: {max_iterations})"
                    )

                    # Clean up workspace artifacts created by the failed
                    # skill-guided phase so the fallback starts fresh.
                    if workspace_path:
                        try:
                            import shutil
                            from pathlib import Path as _P
                            ws = _P(workspace_path)
                            removed = 0
                            if ws.exists():
                                for f in list(ws.iterdir()):
                                    if f.name not in pre_skill_files:
                                        if f.is_dir():
                                            shutil.rmtree(f, ignore_errors=True)
                                        else:
                                            f.unlink(missing_ok=True)
                                        removed += 1
                            if removed:
                                logger.info(
                                    f"[Phase 2 — Fallback] Cleaned {removed} artifact(s) "
                                    f"from failed skill-guided phase"
                                )
                        except Exception as e:
                            logger.debug(f"Workspace cleanup failed: {e}")

                    execution_context_p2 = {**execution_context}
                    execution_context_p2["max_iterations"] = max_iterations

                    result = await self._grounding_agent.process(execution_context_p2)
                    result["active_skills"] = injected_skill_ids
                    logger.info(
                        f"[Phase 2 — Fallback] {result.get('status', 'unknown')} "
                        f"({result.get('iterations', 0)} iterations)"
                    )
            else:
                # No skills matched — standard tool-only execution
                logger.info(
                    f"Executing with GroundingAgent "
                    f"(max {max_iterations} iterations, no skills)..."
                )
                execution_context["max_iterations"] = max_iterations
                result = await self._grounding_agent.process(execution_context)

            execution_time = asyncio.get_event_loop().time() - start_time
            
            status = result.get('status', 'unknown')
            iterations = result.get('iterations', 0)
            tool_count = len(result.get('tool_executions', []))
            
            logger.info("="*60)
            if status == "success":
                logger.info(
                    f"Task completed successfully! "
                    f"({iterations} iterations, {tool_count} tool calls, {execution_time:.2f}s)"
                )
            elif status == "incomplete":
                logger.warning(
                    f"Task incomplete after {iterations} iterations. "
                    f"Consider increasing max_iterations."
                )
            else:
                logger.error(f"Task failed: {result.get('error', 'Unknown error')}")
            logger.info("="*60)
            
        except Exception as e:
            execution_time = asyncio.get_event_loop().time() - start_time
            tb = traceback.format_exc(limit=10)
            logger.error(f"Task execution failed: {e}", exc_info=True)
            
            result = {
                "status": "error",
                "error": str(e),
                "traceback": tb,
                "response": f"Task execution error: {str(e)}",
                "execution_time": execution_time,
                "task_id": task_id,
                "iterations": 0,
                "tool_executions": [],
            }
        
        finally:
            recording_dir = None
            if self._recording_manager and self._recording_manager.recording_status:
                recording_dir = self._recording_manager.trajectory_dir

                # Persist execution outcome to metadata.json before finalizing
                try:
                    exec_time = asyncio.get_event_loop().time() - start_time
                    await self._recording_manager.save_execution_outcome(
                        status=result.get("status", "unknown"),
                        iterations=result.get("iterations", 0),
                        execution_time=exec_time,
                    )
                except Exception:
                    pass  # best-effort; don't block recording stop

                try:
                    await self._recording_manager.stop()
                    logger.debug(f"Recording stopped: {task_id}")
                except Exception as e:
                    logger.warning(f"Failed to stop recording: {e}")

            # Run execution analysis + evolution BEFORE building the return
            # value, so evolved_skills is populated.
            await self._maybe_analyze_execution(
                task_id, recording_dir, result
            )

            # Trigger quality evolution periodically
            await self._maybe_evolve_quality()

            final_result = {
                **result,
                "task_id": task_id,
                "execution_time": execution_time,
                "skills_used": result.get("active_skills", []),
                "evolved_skills": list(self._last_evolved_skills),
            }
            
            self._running = False
            self._task_done.set()
            
            return final_result
    
    # Skills helpers
    def _init_skill_registry(self) -> Optional[SkillRegistry]:
        """Build and populate the SkillRegistry from configured directories.

        Discovery order (earlier wins on name collision):
          1. ``OPENSPACE_HOST_SKILL_DIRS`` env — host agent skill directories
          2. ``config_grounding.json → skills.skill_dirs`` — user-specified
          3. ``openspace/skills/``       — built-in skills (always present)

        ``OPENSPACE_HOST_SKILL_DIRS`` is also handled by ``mcp_server.py``
        for the MCP transport path, but we process it here too so that
        standalone mode (``python -m openspace``) gets the same skills
        discovered and synced to the DB for quality tracking / evolution.
        """
        skill_paths: List[Path] = []
        skill_cfg = self._grounding_config.skills if self._grounding_config else None

        # 1. Host agent skill directories from env (standalone mode support)
        import os
        host_dirs_raw = os.environ.get("OPENSPACE_HOST_SKILL_DIRS", "")
        if host_dirs_raw:
            for d in host_dirs_raw.split(","):
                d = d.strip()
                if not d:
                    continue
                p = Path(d)
                if p.exists():
                    skill_paths.append(p)
                    logger.info(f"Host skill dir (from env): {p}")
                else:
                    logger.warning(f"Host skill dir does not exist: {d}")

        # 2. User-specified skill directories from config_grounding.json
        if skill_cfg and skill_cfg.skill_dirs:
            for d in skill_cfg.skill_dirs:
                p = Path(d)
                if p in skill_paths:
                    continue  # Already added via OPENSPACE_HOST_SKILL_DIRS
                if p.exists():
                    skill_paths.append(p)
                else:
                    logger.warning(f"Configured skill dir does not exist: {d}")

        # 3. Built-in skills (openspace/skills/)
        builtin_skills = Path(__file__).resolve().parent / "skills"
        if builtin_skills.exists():
            skill_paths.append(builtin_skills)

        if not skill_paths:
            logger.debug("No skill directories found, skills disabled")
            return None

        registry = SkillRegistry(skill_dirs=skill_paths)
        registry.discover()
        return registry

    async def _select_and_inject_skills(
        self,
        task: str,
    ) -> bool:
        """Select skills for task via LLM, inject into GroundingAgent.

        When the registry has many skills, a BM25 + embedding pre-filter
        narrows the candidate set before LLM selection (see
        ``SkillRegistry.select_skills_with_llm``).

        Only selected skills are injected (full SKILL.md content).
        Returns True if at least one active skill was injected.
        """
        if not self._skill_registry or not self._grounding_agent:
            return False

        selection_record = None

        # LLM-based skill selection (preferred)
        skill_cfg = self._grounding_config.skills if self._grounding_config else None
        max_select = skill_cfg.max_select if skill_cfg else 2
        skill_llm = self._get_skill_selection_llm()

        # Fetch quality metrics so the selector can filter/annotate
        skill_quality: Optional[Dict[str, Dict[str, Any]]] = None
        if self._skill_store:
            try:
                rows = self._skill_store.get_summary(active_only=True)
                skill_quality = {
                    r["skill_id"]: {
                        "total_selections": r.get("total_selections", 0),
                        "total_applied": r.get("total_applied", 0),
                        "total_completions": r.get("total_completions", 0),
                        "total_fallbacks": r.get("total_fallbacks", 0),
                    }
                    for r in rows
                }
            except Exception as e:
                logger.debug(f"Could not load skill quality metrics: {e}")

        if skill_llm:
            selected, selection_record = await self._skill_registry.select_skills_with_llm(
                task,
                llm_client=skill_llm,
                max_skills=max_select,
                skill_quality=skill_quality,
            )
        else:
            # No LLM client — skip skill selection entirely
            logger.info("No LLM client available for skill selection — proceeding without skills")
            selected = []
            selection_record = {
                "method": "no_llm",
                "task": task[:500],
                "available_skills": [s.skill_id for s in self._skill_registry.list_skills()],
                "selected": [],
            }

        # Record skill selection to metadata.json
        if self._recording_manager and selection_record:
            # Add model info to the record
            selection_record["model"] = skill_llm.model if skill_llm else "keyword_only"
            await RecordingManager.record_skill_selection(selection_record)

        if not selected:
            self._grounding_agent.clear_skill_context()
            return False

        # Inject active skills (full SKILL.md content, backend-aware)
        agent_backends = self._grounding_agent.backend_scope if self._grounding_agent else None
        context_text = self._skill_registry.build_context_injection(selected, backends=agent_backends)
        skill_ids = [s.skill_id for s in selected]
        self._grounding_agent.set_skill_context(context_text, skill_ids)
        logger.info(f"Injected {len(selected)} active skill(s): {skill_ids}")

        return True

    def _get_skill_selection_llm(self) -> Optional[LLMClient]:
        """Get the LLM client to use for skill selection.

        Priority: config.skill_registry_model > tool_retrieval_model > llm_model.
        """
        # 1. Dedicated skill selection model (OpenSpaceConfig.skill_registry_model)
        if self.config.skill_registry_model:
            return LLMClient(
                model=self.config.skill_registry_model,
                timeout=30.0,  # skill selection should be fast
                max_retries=2,
                **self.config.llm_kwargs,
            )

        # 2. Tool retrieval model
        if hasattr(self._grounding_agent, '_tool_retrieval_llm') and self._grounding_agent._tool_retrieval_llm:
            return self._grounding_agent._tool_retrieval_llm

        # 3. Main LLM client
        return self._llm_client

    async def _maybe_analyze_execution(
        self,
        task_id: str,
        recording_dir: Optional[str],
        execution_result: Dict[str, Any],
    ) -> None:
        """Run post-execution analysis if enabled.

        Trigger 1: if the analysis produces evolution suggestions, the
        SkillEvolver processes them immediately (FIX / DERIVED / CAPTURED).
        Evolved skills are recorded in ``_last_evolved_skills`` so the
        caller (MCP ``execute_task``) can include them in the response.
        """
        if not self._execution_analyzer or not recording_dir:
            return
        try:
            # Pass the agent's tools so the analyzer can reuse them
            # for error reproduction / verification when needed.
            agent_tools = getattr(
                self._grounding_agent, "_last_tools", []
            ) if self._grounding_agent else []

            analysis = await self._execution_analyzer.analyze_execution(
                task_id=task_id,
                recording_dir=recording_dir,
                execution_result=execution_result,
                available_tools=agent_tools,
            )
            if not analysis:
                return

            # Trigger 1: post-analysis evolution
            if analysis.candidate_for_evolution and self._skill_evolver:
                self._skill_evolver.set_available_tools(agent_tools)

                evo_summary = ", ".join(
                    f"{s.evolution_type.value}({'+'.join(s.target_skill_ids) or 'new'})"
                    for s in analysis.evolution_suggestions
                )
                logger.info(f"[Skill Evolution] Suggestions: {evo_summary}")
                evolved_records = await self._skill_evolver.process_analysis(analysis)

                # Track evolved skills for the caller
                for rec in evolved_records:
                    self._last_evolved_skills.append({
                        "skill_id": rec.skill_id,
                        "name": rec.name,
                        "description": rec.description,
                        "path": str(rec.path) if rec.path else "",
                        "origin": rec.lineage.origin.value,
                        "generation": rec.lineage.generation,
                        "parent_skill_ids": rec.lineage.parent_skill_ids,
                        "change_summary": rec.lineage.change_summary,
                    })

        except Exception as e:
            # Analysis failure must never break the main execution flow
            logger.debug(f"Execution analysis skipped: {e}")

    async def _maybe_evolve_quality(self) -> None:
        """Trigger quality evolution based on global execution count.

        Includes three sub-triggers:
        - Tool quality evolution (ToolQualityManager)
        - Trigger 2: tool degradation → fix related skills
        - Trigger 3: periodic skill metric check

        Triggers 2 and 3 are always launched as background tasks so they
        never block the main execute() flow. They are awaited on shutdown
        via ``cleanup() → evolver.wait_background()``.
        """
        self._execution_count += 1
        quality_mgr = (
            self._grounding_client.quality_manager
            if self._grounding_client else None
        )

        # Ensure evolver has up-to-date tools for agent loop
        if self._skill_evolver and self._grounding_agent:
            agent_tools = getattr(self._grounding_agent, "_last_tools", [])
            if agent_tools:
                self._skill_evolver.set_available_tools(agent_tools)

        # Tool quality evolution
        if quality_mgr and quality_mgr.should_evolve():
            try:
                report = await self._grounding_client.evolve_quality()
                if report.get("recommendations"):
                    logger.info(f"Quality evolution: {report['recommendations']}")

                # Trigger 2: tool degradation → fix skills that depend on bad tools
                if self._skill_evolver:
                    problematic = quality_mgr.get_problematic_tools()
                    if problematic:
                        logger.info(
                            f"[Trigger:tool_degradation] {len(problematic)} "
                            f"problematic tool(s) detected"
                        )
                        self._skill_evolver.schedule_background(
                            self._skill_evolver.process_tool_degradation(problematic),
                            label="trigger2_tool_degradation",
                        )

            except Exception as e:
                logger.debug(f"Quality evolution skipped: {e}")

        # Trigger 3: periodic skill metric check (every 5 executions)
        if self._skill_evolver and self._execution_count % 5 == 0:
            try:
                self._skill_evolver.schedule_background(
                    self._skill_evolver.process_metric_check(),
                    label="trigger3_metric_check",
                )
            except Exception as e:
                logger.debug(f"Skill metric check skipped: {e}")
    
    async def cleanup(self) -> None:
        """
        Close all sessions and release resources.
        Automatically called when using context manager.
        """
        logger.info("Cleaning up OpenSpace resources...")
        
        try:
            # Wait for background evolution tasks before tearing down
            if self._skill_evolver:
                await self._skill_evolver.wait_background()

            if self._grounding_client:
                await self._grounding_client.close_all_sessions()
                logger.debug("All grounding sessions closed")
            
            if self._recording_manager and self._recording_manager.recording_status:
                try:
                    await self._recording_manager.stop()
                    logger.debug("Recording manager stopped")
                except Exception as e:
                    logger.warning(f"Failed to stop recording: {e}")

            if self._execution_analyzer:
                try:
                    self._execution_analyzer.close()
                    logger.debug("Execution analyzer closed")
                except Exception as e:
                    logger.debug(f"Failed to close execution analyzer: {e}")
            
            self._initialized = False
            self._running = False
            self._task_done.set()
            
            logger.info("OpenSpace cleanup complete")
            
        except Exception as e:
            logger.error(f"Error during cleanup: {e}", exc_info=True)
    
    def is_initialized(self) -> bool:
        return self._initialized
    
    def is_running(self) -> bool:
        return self._running
    
    def get_config(self) -> OpenSpaceConfig:
        return self.config
    
    def list_backends(self) -> List[str]:
        if not self._initialized:
            raise RuntimeError("OpenSpace not initialized")
        return [backend.value for backend in self._grounding_client.list_providers().keys()]
    
    def list_sessions(self) -> List[str]:
        if not self._initialized:
            raise RuntimeError("OpenSpace not initialized")
        return self._grounding_client.list_sessions()
    
    async def __aenter__(self):
        """Context manager entry"""
        await self.initialize()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        await self.cleanup()
        return False
    
    def __repr__(self) -> str:
        status = "initialized" if self._initialized else "not initialized"
        if self._running:
            status = "running"
        backends = ", ".join(self.config.backend_scope) if self.config.backend_scope else "all"
        return f"<OpenSpace(status={status}, backends={backends}, model={self.config.llm_model})>"