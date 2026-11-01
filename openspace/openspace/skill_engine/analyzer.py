"""ExecutionAnalyzer — post-execution analysis and skill quality tracking.

Responsibilities:
  1. Analyze an EvidencePacket for a completed TriggerJob.
  2. Build an LLM prompt and obtain an ``ExecutionAnalysis``.
  3. Persist the analysis and update ``SkillRecord`` counters via ``SkillStore``.
  4. Surface proposal data for DecisionRationale generation.

Integration:
  Instantiated once during ``OpenSpace.initialize()``.
  Runtime evolution uses ``analyze_packet()`` through ``DecisionEngine``.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from openspace.grounding.core.tool import BaseTool
from openspace.tool_runtime.orchestration import run_tools

from .types import (
    CaptureContract,
    EvolutionSuggestion,
    EvolutionType,
    ExecutionAnalysis,
    SkillCategory,
    SkillJudgment,
)
from .store import SkillStore
from openspace.prompts import SkillEnginePrompts
from openspace.telemetry.call_source import reset_call_source, set_call_source
from openspace.utils.logging import Logger
from .conversation_formatter import format_conversations

if TYPE_CHECKING:
    from openspace.llm import LLMClient
    from openspace.grounding.core.quality import ToolQualityManager
    from openspace.skill_engine.evidence import EvidencePacket
    from .registry import SkillRegistry

logger = Logger.get_logger(__name__)


def _make_skill_quality_reporter() -> Any:
    from openspace.cloud.skill_quality_reporter import CloudSkillQualityReporter

    return CloudSkillQualityReporter()


# Maximum characters of conversation log to include in the analysis prompt.
_MAX_CONVERSATION_CHARS = 80_000

# Per-section truncation limits
_TOOL_ERROR_MAX_CHARS = 1000      # Errors: keep key info, no full stack traces
_TOOL_SUCCESS_MAX_CHARS = 800     # Success results
_TOOL_ARGS_MAX_CHARS = 500        # Tool call arguments
_TOOL_SUMMARY_MAX_CHARS = 1500    # Embedded execution summaries from inner agents

# Skill & analysis-agent constants
_SKILL_CONTENT_MAX_CHARS = 8000   # Max chars per skill SKILL.md in prompt
_ANALYSIS_MAX_ITERATIONS = 5      # Max tool-calling rounds for analysis agent
_MAX_ANALYSIS_LENGTH_RECOVERIES = 1
_MAX_ANALYSIS_INVALID_JSON_RECOVERIES = 1


def _build_analysis_length_recovery_message(attempt: int) -> Dict[str, Any]:
    return {
        "role": "user",
        "content": (
            "Your previous analysis hit the maximum output-token limit. "
            "Return one compact JSON object only, with no prose, markdown, or "
            "tool calls. Include task_completed, execution_note, tool_issues, "
            "skill_judgments, skill_phase_failed_skill_ids, "
            "evolution_suggestions, and analyzed_by. Keep notes and suggestions "
            "concise. "
            f"(recovery attempt {attempt}/{_MAX_ANALYSIS_LENGTH_RECOVERIES})"
        ),
        "_meta": {"type": "analysis_max_output_tokens_recovery"},
    }


def _build_analysis_invalid_json_recovery_message(attempt: int) -> Dict[str, Any]:
    return {
        "role": "user",
        "content": (
            "Your previous response was not a valid analysis JSON object. "
            "Return one compact JSON object only, with no prose, markdown, or "
            "tool calls. Include task_completed, execution_note, tool_issues, "
            "skill_judgments, skill_phase_failed_skill_ids, "
            "evolution_suggestions, and analyzed_by. Keep all fields concise. "
            f"(recovery attempt {attempt}/{_MAX_ANALYSIS_INVALID_JSON_RECOVERIES})"
        ),
        "_meta": {"type": "analysis_invalid_json_recovery"},
    }


def _correct_skill_ids(
    ids: List[str], known_ids: set,
) -> List[str]:
    """Best-effort correction of LLM-hallucinated skill IDs.

    LLMs frequently garble the hex suffix of generated IDs (e.g. swap
    ``cb`` → ``bc``).  For each *id* not in *known_ids*, find the closest
    known ID sharing the same name prefix (before ``__``) and within
    edit-distance ≤ 3.  If a unique match is found, silently replace it.
    """
    if not known_ids:
        return ids

    corrected: List[str] = []
    for raw_id in ids:
        if raw_id in known_ids:
            corrected.append(raw_id)
            continue

        # Extract name prefix (everything before the first "__")
        prefix = raw_id.split("__")[0] if "__" in raw_id else ""

        # Candidates: known IDs sharing the same name prefix
        candidates = [
            k for k in known_ids
            if prefix and k.split("__")[0] == prefix
        ]

        # Adaptive threshold: tighten when many candidates share the prefix
        max_dist = 2 if len(candidates) > 20 else 4  # ≤1 or ≤3
        best, best_dist, ambiguous = None, max_dist, False
        for cand in candidates:
            d = _edit_distance(raw_id, cand)
            if d < best_dist:
                best, best_dist, ambiguous = cand, d, False
            elif d == best_dist and cand != best:
                ambiguous = True  # multiple candidates at same distance

        if best is not None and not ambiguous:
            logger.info(
                f"Corrected LLM skill ID: {raw_id!r} → {best!r} "
                f"(edit_distance={best_dist})"
            )
            corrected.append(best)
        else:
            corrected.append(raw_id)  # keep as-is; evolver will warn

    return corrected


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein edit distance (compact DP, O(min(m,n)) space)."""
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            curr[j] = min(
                prev[j] + 1,
                curr[j - 1] + 1,
                prev[j - 1] + (0 if ca == cb else 1),
            )
        prev = curr
    return prev[-1]


def _parse_analysis_bool(value: Any) -> bool:
    """Parse booleans from analyzer JSON without treating "false" as truthy."""

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0", ""}:
            return False
    return False


def _coerce_id_list(value: Any) -> List[str]:
    """Return a compact string list from analyzer/metadata JSON values."""

    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    ids: List[str] = []
    for raw in value:
        if raw is None:
            continue
        item = str(raw).strip()
        if item:
            ids.append(item)
    return ids


def _first_present(mapping: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping.get(key)
    return None


def _extract_skill_phase_failed_skill_ids(
    data: Dict[str, Any],
    context: Dict[str, Any],
    known_skill_ids: set,
) -> List[str]:
    """Extract factual skill-phase fallback failures for quality accounting."""

    llm_ids: List[str] = []
    llm_ids.extend(_coerce_id_list(data.get("skill_phase_failed_skill_ids")))

    metadata_ids: List[str] = []
    metadata_ids.extend(_coerce_id_list(context.get("skill_phase_failed_skill_ids")))
    explicit_no_phase_failure = False

    skill_execution = context.get("skill_execution") or {}
    if isinstance(skill_execution, dict):
        failure_keys = (
            "skill_phase_failed_skill_ids",
            "phase_failed_skill_ids",
            "failed_skill_ids",
            "failed_skills",
        )
        failure_keys_present = any(key in skill_execution for key in failure_keys)
        for key in failure_keys:
            metadata_ids.extend(_coerce_id_list(skill_execution.get(key)))

        selected_ids = _coerce_id_list(skill_execution.get("selected"))
        phase_status = str(
            skill_execution.get("skill_phase_status")
            or skill_execution.get("skill_guided_status")
            or ""
        ).lower()
        fallback_status = str(
            skill_execution.get("tool_only_fallback_status")
            or skill_execution.get("fallback_status")
            or ""
        ).lower()
        fallback_ran_value = _first_present(
            skill_execution,
            "tool_only_fallback_ran",
            "fallback_ran",
        )
        fallback_ran = _parse_analysis_bool(fallback_ran_value)
        if (
            selected_ids
            and phase_status in {"failed", "error", "incomplete"}
            and (fallback_ran or fallback_status in {"success", "completed"})
        ):
            metadata_ids.extend(selected_ids)
        if selected_ids and (
            phase_status in {"success", "completed"}
            or fallback_ran_value is False
            or fallback_status in {"not_run", "skipped", "none"}
        ):
            explicit_no_phase_failure = True

        for phase_key in ("skill_phase", "skill_guided_phase"):
            phase = skill_execution.get(phase_key)
            if not isinstance(phase, dict):
                continue
            phase_ids = (
                _coerce_id_list(phase.get("skill_ids"))
                or _coerce_id_list(phase.get("selected"))
            )
            phase_failed_ids = (
                _coerce_id_list(phase.get("failed_skill_ids"))
                or _coerce_id_list(phase.get("skill_phase_failed_skill_ids"))
            )
            metadata_ids.extend(phase_failed_ids)
            nested_phase_status = str(phase.get("status") or "").lower()
            phase_failed = nested_phase_status in {
                "failed",
                "error",
                "incomplete",
            }
            fallback = skill_execution.get("tool_only_fallback") or skill_execution.get(
                "fallback_phase"
            )
            fallback_succeeded = (
                isinstance(fallback, dict)
                and str(fallback.get("status") or "").lower()
                in {"success", "completed"}
            )
            if phase_ids and phase_failed and fallback_succeeded:
                metadata_ids.extend(phase_ids)
            if phase_ids and nested_phase_status in {"success", "completed"}:
                explicit_no_phase_failure = True

        if failure_keys_present and not metadata_ids:
            explicit_no_phase_failure = True

    raw_ids = metadata_ids if explicit_no_phase_failure else [*llm_ids, *metadata_ids]
    corrected_ids = _correct_skill_ids(raw_ids, known_skill_ids)
    return [sid for sid in dict.fromkeys(corrected_ids) if sid in known_skill_ids]


class ExecutionAnalyzer:
    """Analyzes task execution results and tracks skill quality.

    Args:
        store: Persistence layer for skill records and analyses.
        llm_client: LLM client used for the analysis call.
        model: Override model for analysis. If None, uses ``llm_client``'s default model.
        enabled: Set False to skip analysis entirely.
    """

    def __init__(
        self,
        store: SkillStore,
        llm_client: "LLMClient",
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        enabled: bool = True,
        skill_registry: Optional["SkillRegistry"] = None,
        quality_manager: Optional["ToolQualityManager"] = None,
    ) -> None:
        self._store = store
        self._llm_client = llm_client
        self._model = model
        self._max_tokens = (
            max(1, int(max_tokens)) if max_tokens is not None else None
        )
        self.enabled = enabled
        self._skill_registry = skill_registry
        self._quality_manager = quality_manager

    async def _call_analysis_model(
        self,
        *,
        messages: List[Dict[str, Any]],
        tools: Optional[List[BaseTool]],
        model: str,
        disable_thinking: bool = False,
    ) -> Any:
        kwargs: Dict[str, Any] = {
            "messages": messages,
            "tools": tools,
            "model": model,
        }
        if self._max_tokens is not None:
            kwargs["max_tokens"] = self._max_tokens
        if disable_thinking:
            kwargs["enable_thinking"] = False
        return await self._llm_client.call_model_with_fallback(**kwargs)

    async def analyze_execution(
        self,
        task_id: str,
        recording_dir: str,
        execution_result: Dict[str, Any],
        available_tools: Optional[List[BaseTool]] = None,
    ) -> Optional[ExecutionAnalysis]:
        """Run LLM analysis on a completed task and persist the result.

        Args:
            task_id: Unique identifier for the task.
            recording_dir: Path to the recording directory containing metadata.json,
                conversations.jsonl, etc.
            execution_result: The return value of ``OpenSpace.execute()`` — contains status,
                iterations, skills_used, etc.
            available_tools: BaseTool instances from the execution (shell tools,
                MCP tools, etc.). Passed through to the analysis agent loop so
                it can reproduce errors or verify results when trace data is
                ambiguous. A lightweight ``bash`` tool is always appended.
        """
        if not self.enabled:
            return None

        rec_path = Path(recording_dir)
        if not rec_path.is_dir():
            logger.warning(
                f"Recording directory not found, skipping analysis: {recording_dir}"
            )
            return None

        # Check for duplicate — one analysis per task
        existing = self._store.load_analyses_for_task(task_id)
        if existing is not None:
            logger.debug(f"Analysis already exists for task {task_id}, skipping")
            return existing

        _src_tok = set_call_source("analyzer")

        try:
            # 1. Load recording artifacts
            context = self._load_recording_context(rec_path, execution_result)
            if context is None:
                return None
            if self._should_skip_low_signal_analysis(context, execution_result):
                logger.info(
                    "Skipping execution analysis for low-signal successful task: %s",
                    task_id,
                )
                return None

            # 2. Build prompt
            prompt = self._build_analysis_prompt(context)

            # 3. Run analysis (agent loop with optional tool use)
            raw_json = await self._run_analysis_loop(
                prompt, available_tools=available_tools or [],
            )
            if raw_json is None:
                return None

            # 4. Parse into ExecutionAnalysis
            analysis = self._parse_analysis(task_id, raw_json, context)
            if analysis is None:
                return None

            # 5. Persist
            await self._store.record_analysis(
                analysis,
                observed_tool_keys=context.get("used_tool_keys", set()),
            )
            await self._report_skill_quality_after_record_analysis(
                analysis,
                context,
                execution_result=execution_result,
            )
            evo_types = [s.evolution_type.value for s in analysis.evolution_suggestions]
            logger.info(
                f"Execution analysis saved for task {task_id}: "
                f"completed={analysis.task_completed}, "
                f"skills_judged={len(analysis.skill_judgments)}, "
                f"evolution_suggestions={evo_types or 'none'}"
            )

            return analysis

        except Exception as e:
            logger.error(f"Execution analysis failed for task {task_id}: {e}")
            return None
        finally:
            reset_call_source(_src_tok)

    @staticmethod
    def _should_skip_low_signal_analysis(
        context: Dict[str, Any],
        execution_result: Dict[str, Any],
    ) -> bool:
        """Skip analysis that cannot produce reliable skill-quality signal.

        A successful turn with no tool trajectory and no invoked skills is
        usually chat/reasoning. Running the analyzer on that shape has no tool
        evidence and can hallucinate CAPTURED/FIX suggestions from ordinary
        conversation.
        """
        status = str(
            execution_result.get("status") or context.get("execution_status") or ""
        ).lower()
        if status != "success":
            return False
        if context.get("traj_records"):
            return False
        if context.get("used_tool_keys"):
            return False
        if context.get("selected_skills"):
            return False
        if context.get("skill_contents"):
            return False
        tool_executions = execution_result.get("tool_executions")
        if isinstance(tool_executions, list) and tool_executions:
            return False
        return True

    async def _report_skill_quality_after_record_analysis(
        self,
        analysis: ExecutionAnalysis,
        context: Dict[str, Any],
        *,
        execution_result: Dict[str, Any] | None = None,
    ) -> None:
        try:
            reporter = _make_skill_quality_reporter()
            outcome = await reporter.maybe_report_analysis(
                analysis,
                session_id=_analysis_session_id(
                    context,
                    execution_result=execution_result,
                ),
            )
            logger.debug(
                "Skill quality telemetry outcome for task %s: %s",
                analysis.task_id,
                outcome.get("status") if isinstance(outcome, dict) else outcome,
            )
        except Exception as exc:
            logger.warning(
                "Skill quality telemetry skipped after analyzer persistence for task %s: %s",
                analysis.task_id,
                exc,
            )

    async def get_evolution_candidates(
        self, limit: int = 20
    ) -> List[ExecutionAnalysis]:
        """Return recent analyses flagged as evolution candidates."""
        return self._store.load_evolution_candidates(limit=limit)

    async def analyze_packet(
        self,
        packet: "EvidencePacket",
        available_tools: Optional[List[BaseTool]] = None,
    ) -> Optional[ExecutionAnalysis]:
        """Run post-execution analysis from an EvidencePacket.

        The packet is the factual scope for analysis. Recording refs, when
        present, are only fallback/debug context in the prompt.
        """
        if not self.enabled:
            return None
        if getattr(packet, "packet_type", "") != "analysis":
            logger.debug(
                "Skipping execution analysis for non-analysis packet: %s",
                getattr(packet, "packet_id", ""),
            )
            return None

        task_id = packet.scope.task_id or packet.packet_id
        existing = self._store.load_analyses_for_task(task_id)
        if existing is not None:
            logger.debug("Analysis already exists for task %s, skipping", task_id)
            return existing

        _src_tok = set_call_source("analyzer")
        try:
            context = self._load_packet_context(packet, task_id=task_id)
            execution_result = {
                "status": context.get("execution_status", ""),
                "iterations": context.get("iterations", 0),
                "tool_executions": context.get("packet_tool_records", []),
            }
            if self._should_skip_low_signal_analysis(context, execution_result):
                logger.info(
                    "Skipping packet analysis for low-signal successful task: %s",
                    task_id,
                )
                return None

            prompt = self._build_packet_analysis_prompt(packet, context)
            raw_json = await self._run_analysis_loop(
                prompt,
                available_tools=available_tools or [],
            )
            if raw_json is None:
                return None

            analysis = self._parse_analysis(task_id, raw_json, context)
            if analysis is None:
                return None

            await self._store.record_analysis(
                analysis,
                observed_tool_keys=context.get("used_tool_keys", set()),
            )
            await self._report_skill_quality_after_record_analysis(analysis, context)
            evo_types = [s.evolution_type.value for s in analysis.evolution_suggestions]
            logger.info(
                "Execution packet analysis saved for task %s: completed=%s, "
                "skills_judged=%s, evolution_suggestions=%s",
                task_id,
                analysis.task_completed,
                len(analysis.skill_judgments),
                evo_types or "none",
            )
            return analysis
        except Exception as exc:
            logger.error("Execution packet analysis failed for task %s: %s", task_id, exc)
            return None
        finally:
            reset_call_source(_src_tok)

    def _load_packet_context(
        self,
        packet: "EvidencePacket",
        *,
        task_id: str,
    ) -> Dict[str, Any]:
        runtime_refs = packet.selected_refs.get("runtime_snapshot", [])
        runtime_meta = dict(runtime_refs[-1].metadata) if runtime_refs else {}
        selected_skills = _packet_selected_skill_ids(packet, runtime_meta)
        packet_tool_records = _packet_tool_records(packet)
        used_tool_keys = {
            str(item.get("tool_key") or "")
            for item in packet_tool_records
            if str(item.get("tool_key") or "")
        }
        task_description = (
            str(runtime_meta.get("instruction_preview") or "").strip()
            or _first_user_packet_preview(packet)
            or f"Evidence packet {packet.packet_id}"
        )
        return {
            "task_id": task_id,
            "task_description": task_description,
            "selected_skills": selected_skills,
            "skill_selection": {
                "selected": selected_skills,
                "available_skills": selected_skills,
                "source": "evidence_packet",
            },
            "skill_execution": _packet_skill_execution(packet),
            "skill_contents": _packet_skill_contents(packet),
            "tool_names": sorted(used_tool_keys),
            "tool_defs": _packet_tool_defs(runtime_meta, used_tool_keys),
            "used_tool_keys": used_tool_keys,
            "conversations": [],
            "traj_records": packet_tool_records,
            "packet_tool_records": packet_tool_records,
            "execution_status": str(runtime_meta.get("status") or "unknown"),
            "iterations": int(runtime_meta.get("iterations") or 0),
            "session_id": _packet_session_id(packet),
            "recording_dir": _packet_recording_dir(packet),
            "packet": packet,
        }

    def _build_packet_analysis_prompt(
        self,
        packet: "EvidencePacket",
        context: Dict[str, Any],
    ) -> str:
        selected_skill_ids: List[str] = context["selected_skills"]
        skill_section = _packet_skill_section(packet)
        tool_list = self._format_tool_list(
            context.get("tool_defs", []),
            context.get("used_tool_keys", set()),
        )
        conversation_log = _packet_snippet_section(
            packet,
            {
                "transcript_message",
                "transcript_segment",
                "compact_summary",
                "manual_request_ref",
            },
            fallback_label="(no transcript snippets selected)",
        )
        traj_summary = _packet_snippet_section(
            packet,
            {"tool_event", "tool_result", "tool_incident"},
            fallback_label="(no tool evidence selected)",
        )
        resource_info = _packet_resource_info(packet)
        local_taxonomy_info = self._format_local_taxonomy_info()
        if local_taxonomy_info:
            resource_info = (
                f"{resource_info}\n\n{local_taxonomy_info}"
                if resource_info
                else local_taxonomy_info
            )

        return SkillEnginePrompts.execution_analysis(
            task_description=context["task_description"],
            execution_status=context["execution_status"],
            iterations=context["iterations"],
            tool_list=tool_list,
            skill_section=skill_section,
            conversation_log=conversation_log,
            traj_summary=traj_summary,
            selected_skill_ids_json=json.dumps(selected_skill_ids),
            resource_info=resource_info,
        )

    def _load_recording_context(
        self,
        rec_path: Path,
        execution_result: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Load and structure all recording artifacts needed for analysis.

        Returns a dict with keys used by ``_build_analysis_prompt()``,
        or None if critical files are missing.
        """
        # metadata.json (always present)
        metadata_file = rec_path / "metadata.json"
        if not metadata_file.exists():
            logger.warning(f"metadata.json not found in {rec_path}")
            return None
        try:
            metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Failed to read metadata.json: {e}")
            return None

        # conversations.jsonl (primary analysis source)
        conv_file = rec_path / "conversations.jsonl"
        conversations: List[Dict[str, Any]] = []
        if conv_file.exists():
            try:
                for line in conv_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line:
                        conversations.append(json.loads(line))
            except Exception as e:
                logger.warning(f"Failed to read conversations.jsonl: {e}")

        if not conversations:
            logger.warning(f"No conversations found in {rec_path}, skipping analysis")
            return None

        # traj.jsonl (structured tool execution records)
        traj_records = self._load_traj_data(rec_path)

        task_id = str(metadata.get("task_id") or "")

        # Extract key fields from metadata
        task_description = metadata.get(
            "task_description",
            (metadata.get("skill_selection") or {}).get("task", ""),
        )
        if not task_description:
            task_description = execution_result.get("instruction", "")

        skill_selection = metadata.get("skill_selection", {})
        initial_selected_skills = skill_selection.get("selected", [])
        if not isinstance(initial_selected_skills, list):
            initial_selected_skills = []

        event_invoked_skill_ids = self._load_invoked_skill_event_ids(task_id)
        invoked_skill_ids, invoked_skill_contents = (
            self._extract_invoked_skills_from_conversations(conversations)
        )
        result_skill_ids = [
            sid for sid in (
                execution_result.get("active_skills")
                or execution_result.get("skills_used")
                or []
            )
            if sid
        ]
        skill_execution = metadata.get("skill_execution") or execution_result.get(
            "skill_execution",
            {},
        )
        if not isinstance(skill_execution, dict):
            skill_execution = {}
        skill_execution_selected_ids = [
            sid for sid in skill_execution.get("selected", []) or [] if sid
        ]

        selected_skills = list(
            dict.fromkeys([
                *initial_selected_skills,
                *event_invoked_skill_ids,
                *invoked_skill_ids,
                *result_skill_ids,
                *skill_execution_selected_ids,
            ])
        )

        retrieved_tools = metadata.get("retrieved_tools", {})
        tool_defs = retrieved_tools.get("tools", [])
        tool_names = [t.get("name", "") for t in tool_defs]

        # Extract skill content from conversations setup message
        # selected_skills contains skill_ids stored in runtime metadata.
        skill_contents: Dict[str, str] = {}
        for conv in conversations:
            if conv.get("type") == "setup":
                for msg in conv.get("messages", []):
                    content = msg.get("content", "")
                    if isinstance(content, str) and "# Active Skills" in content:
                        skill_contents = self._extract_skill_contents(
                            content, selected_skills
                        )
                        break
        skill_contents.update(invoked_skill_contents)

        # Execution status — prefer runtime result, fall back to persisted metadata
        status = execution_result.get("status", "")
        iterations = execution_result.get("iterations", 0)
        if not status:
            outcome = metadata.get("execution_outcome", {})
            status = outcome.get("status", "unknown")
            iterations = iterations or outcome.get("iterations", 0)

        # Derive actually-used tools from traj.jsonl
        # traj_records tells us exactly which tools were invoked; retrieved_tools
        # is the broader set that was *available* to the agent.
        used_tool_keys: set = set()
        for entry in traj_records:
            backend = entry.get("backend", "")
            tool = entry.get("tool", "")
            server = entry.get("server", "")
            if tool:
                used_tool_keys.add(f"{backend}:{tool}")
                if server:
                    used_tool_keys.add(f"{backend}:{server}:{tool}")

        return {
            "task_id": task_id,
            "task_description": task_description,
            "selected_skills": selected_skills,
            "skill_selection": skill_selection,
            "skill_execution": skill_execution,
            "skill_contents": skill_contents,
            "tool_names": tool_names,
            "tool_defs": tool_defs,
            "used_tool_keys": used_tool_keys,
            "conversations": conversations,
            "traj_records": traj_records,
            "execution_status": status,
            "iterations": iterations,
            "recording_dir": str(rec_path),
        }

    @staticmethod
    def _load_traj_data(rec_path: Path) -> List[Dict[str, Any]]:
        """Load traj.jsonl and return structured tool execution records.

        Each record contains: step, timestamp, backend, tool, command,
        result (status, output/stderr), parameters, extra.
        """
        traj_file = rec_path / "traj.jsonl"
        records: List[Dict[str, Any]] = []
        if not traj_file.exists():
            return records
        try:
            for line in traj_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        except Exception as e:
            logger.warning(f"Failed to read traj.jsonl: {e}")
        return records

    def _load_invoked_skill_event_ids(self, task_id: str) -> List[str]:
        if not task_id:
            return []
        try:
            events = self._store.load_skill_events(
                event_type="invoked",
                task_id=task_id,
                limit=500,
            )
        except Exception as exc:
            logger.debug("Failed to load invoked skill events: %s", exc)
            return []
        return list(
            dict.fromkeys(
                str(event.get("skill_id") or "")
                for event in events
                if str(event.get("skill_id") or "").strip()
            )
        )

    @staticmethod
    def _extract_invoked_skills_from_conversations(
        conversations: List[Dict[str, Any]],
    ) -> tuple[List[str], Dict[str, str]]:
        """Extract OpenSpace SkillTool invocations from recorded attachment messages."""

        skill_ids: List[str] = []
        skill_contents: Dict[str, str] = {}
        for conv in conversations:
            for msg in ExecutionAnalyzer._conversation_messages(conv):
                if not isinstance(msg, dict):
                    continue
                meta = msg.get("_meta") or {}
                attachment = meta.get("attachment") if isinstance(meta, dict) else None
                if isinstance(attachment, dict):
                    if attachment.get("type") == "invoked_skill_content":
                        sid = str(attachment.get("skill_id") or "")
                        content = str(attachment.get("content") or "")
                        if sid:
                            skill_ids.append(sid)
                            if content:
                                skill_contents[sid] = content
                    elif attachment.get("type") == "invoked_skills":
                        for item in attachment.get("skills", []) or []:
                            if not isinstance(item, dict):
                                continue
                            sid = str(item.get("skill_id") or "")
                            content = str(item.get("content") or "")
                            if sid:
                                skill_ids.append(sid)
                                if content:
                                    skill_contents[sid] = content
                tr_meta = meta.get("tool_result_metadata") if isinstance(meta, dict) else None
                if isinstance(tr_meta, dict) and tr_meta.get("tool") == "Skill":
                    sid = str(tr_meta.get("skill_id") or "")
                    if sid:
                        skill_ids.append(sid)

        return list(dict.fromkeys(skill_ids)), skill_contents

    @staticmethod
    def _conversation_messages(conv: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Return setup and per-iteration messages from a conversation record."""

        messages: List[Dict[str, Any]] = []
        for key in ("messages", "delta_messages"):
            value = conv.get(key)
            if isinstance(value, list):
                messages.extend(item for item in value if isinstance(item, dict))
        return messages

    @staticmethod
    def _extract_skill_contents(
        injection_text: str,
        selected_skill_ids: List[str],
    ) -> Dict[str, str]:
        """Parse the injected skill context to extract per-skill content.

        The injection text uses ``### Skill: {skill_id}`` headers, so
        we split by that pattern and match against the provided skill_ids.
        """
        contents: Dict[str, str] = {}
        id_set = set(selected_skill_ids)
        parts = re.split(r"###\s+Skill:\s+", injection_text)
        for part in parts[1:]:  # skip preamble
            lines = part.split("\n", 1)
            sid = lines[0].strip()
            body = lines[1] if len(lines) > 1 else ""
            if sid in id_set:
                contents[sid] = body[:5000]
        return contents

    def _load_skill_contents_from_disk(
        self, skill_ids: List[str],
    ) -> Dict[str, Dict[str, str]]:
        """Load skill SKILL.md from disk via SkillRegistry.

        Returns dict mapping ``skill_id`` → ``{"content", "dir", "description", "name"}``.
        Falls back gracefully if registry is unavailable.
        """
        result: Dict[str, Dict[str, str]] = {}
        if not self._skill_registry or not skill_ids:
            return result
        for sid in skill_ids:
            meta = self._skill_registry.get_skill(sid)
            if not meta:
                continue
            content = self._skill_registry.load_skill_content(sid)
            if not content:
                continue
            skill_dir = str(meta.path.parent)
            if len(content) > _SKILL_CONTENT_MAX_CHARS:
                content = (
                    content[:_SKILL_CONTENT_MAX_CHARS]
                    + f"\n\n... [truncated at {_SKILL_CONTENT_MAX_CHARS} chars — "
                    f"use read(\"{meta.path}\") to see full content]"
                )
            result[sid] = {
                "content": content,
                "dir": skill_dir,
                "description": meta.description,
                "name": meta.name,
            }
        return result

    def _build_analysis_prompt(self, context: Dict[str, Any]) -> str:
        """Build the LLM prompt for execution analysis.

        ``context["selected_skills"]`` contains true ``skill_id`` values.
        """
        # Format conversation log (priority-based truncation)
        conv_text = self._format_conversations(context["conversations"])

        # Format traj.jsonl tool execution summary
        traj_section = self._format_traj_summary(context["traj_records"])

        # Skill section — keyed by skill_id throughout
        selected_skill_ids: List[str] = context["selected_skills"]
        skill_data = self._load_skill_contents_from_disk(selected_skill_ids)

        if not skill_data and selected_skill_ids:
            # Fallback: use content extracted from conversation injection text
            for sid in selected_skill_ids:
                content = context["skill_contents"].get(sid)
                if content:
                    skill_data[sid] = {
                        "content": content,
                        "dir": "(unknown)",
                        "description": "",
                        "name": sid,
                    }

        skill_section = ""
        if skill_data:
            parts = []
            for sid, info in skill_data.items():
                desc_line = (
                    f"\n**Description**: {info['description']}"
                    if info.get("description") else ""
                )
                display_name = info.get("name", sid)
                parts.append(
                    f"### {sid}\n"
                    f"**Name**: {display_name}\n"
                    f"**Directory**: `{info['dir']}`{desc_line}\n\n"
                    f"{info['content']}"
                )
            skill_section = "## Selected Skills\n\n" + "\n\n---\n\n".join(parts)
        # If no skills selected → skill_section stays "" (omitted from prompt)

        # Tool list
        tool_list = self._format_tool_list(
            context.get("tool_defs", []),
            context.get("used_tool_keys", set()),
        )

        # Resource info (recording dir + skill dirs)
        rec_dir = context.get("recording_dir", "")
        resource_lines: List[str] = []
        if rec_dir:
            resource_lines.append(f"**Recording directory**: `{rec_dir}`")
            rec_path = Path(rec_dir)
            if rec_path.is_dir():
                files = [f.name for f in sorted(rec_path.iterdir()) if f.is_file()]
                dirs = [f"{f.name}/" for f in sorted(rec_path.iterdir()) if f.is_dir()]
                if files:
                    resource_lines.append(f"  Files: {', '.join(files)}")
                if dirs:
                    resource_lines.append(f"  Directories: {', '.join(dirs)}")

        skill_dirs = {
            sid: info["dir"]
            for sid, info in skill_data.items()
            if info.get("dir") and info["dir"] != "(unknown)"
        }
        if skill_dirs:
            resource_lines.append("**Skill directories**:")
            for sid, d in skill_dirs.items():
                resource_lines.append(f"  - {sid}: `{d}`")

        skill_execution = context.get("skill_execution") or {}
        if skill_execution:
            resource_lines.append(
                "**Skill execution phases**:\n"
                + json.dumps(skill_execution, ensure_ascii=False, indent=2)
            )

        local_taxonomy_info = self._format_local_taxonomy_info()
        if local_taxonomy_info:
            resource_lines.append(local_taxonomy_info)

        resource_lines.append(
            "\nYou have `read`, `ls`, and `bash` tools for deeper "
            "investigation.\n**In most cases the trace above is sufficient** — only "
            "use tools when evidence is ambiguous or you need to verify specific details."
        )
        resource_info = "\n".join(resource_lines)

        return SkillEnginePrompts.execution_analysis(
            task_description=context["task_description"],
            execution_status=context["execution_status"],
            iterations=context["iterations"],
            tool_list=tool_list,
            skill_section=skill_section,
            conversation_log=conv_text,
            traj_summary=traj_section,
            selected_skill_ids_json=json.dumps(selected_skill_ids),
            resource_info=resource_info,
        )

    def _format_local_taxonomy_info(self) -> str:
        try:
            from openspace.cloud.local_mapping import CloudLocalMappingStore
            from openspace.cloud.skill_classification import (
                build_local_taxonomy_snapshot,
                initialize_local_skill_taxonomy,
            )

            db_path = getattr(self._store, "db_path", None)
            if db_path is None and getattr(self._store, "base", None) is not None:
                db_path = getattr(self._store.base, "db_path", None)
            mapping_store = CloudLocalMappingStore(db_path) if db_path is not None else None
            try:
                skills = self._skill_registry.list_skills() if self._skill_registry else []
                if mapping_store is not None:
                    initialize_local_skill_taxonomy(
                        mapping_store=mapping_store,
                        skills=skills,
                    )
                snapshot = build_local_taxonomy_snapshot(
                    mapping_store=mapping_store,
                    skills=skills,
                    max_paths=12,
                    max_examples_per_path=1,
                    include_sample_paths=True,
                )
            finally:
                if mapping_store is not None:
                    mapping_store.close()
        except Exception as exc:
            logger.debug("Local taxonomy snapshot unavailable for analyzer: %s", exc)
            return ""

        lines = [
            "**Local taxonomy tree (package-style skill placement)**:",
            f"Path format: `{snapshot.get('path_format', 'domain/sub-domain/package')}`",
            "Use this tree when choosing `local_category_path` for DERIVED or CAPTURED suggestions.",
        ]
        for category in snapshot.get("categories", []) or []:
            if isinstance(category, dict):
                lines.append(
                    f"  - {category.get('category')}: {category.get('skill_count', 0)} skill(s)"
                )
        roots = snapshot.get("roots") or []
        if roots:
            lines.append("Top-level local taxonomy roots:")
            for item in roots[:12]:
                if isinstance(item, dict):
                    lines.append(
                        f"  - {item.get('local_category_path')} "
                        f"({item.get('skill_count', 0)} skill(s))"
                    )
        paths = snapshot.get("paths") or snapshot.get("sample_paths") or []
        if paths:
            lines.append("Sample existing local paths:")
            for item in paths[:40]:
                if not isinstance(item, dict):
                    continue
                examples = item.get("examples") or []
                names = [
                    str(example.get("name") or example.get("local_skill_id") or "")
                    for example in examples
                    if isinstance(example, dict)
                ]
                shown_names = ", ".join(name for name in names if name)
                suffix = f" examples={shown_names}" if shown_names else ""
                lines.append(
                    f"  - {item.get('local_category_path')} "
                    f"({item.get('skill_count', 0)} skill(s)){suffix}"
                )
        else:
            lines.append("Existing local paths: (none yet)")
        return "\n".join(lines)

    @staticmethod
    def _format_tool_list(
        tool_defs: List[Dict[str, Any]],
        used_tool_keys: set = None,
    ) -> str:
        """Format tool definitions with usage annotation.

        Tools that appear in ``used_tool_keys`` (derived from traj.jsonl)
        are marked as "Actually used".  This lets the analysis LLM focus
        on what actually happened without being distracted by unused tools.

        Args:
            tool_defs: Tool definitions from ``metadata.retrieved_tools.tools``.
                Backend should be correctly recorded (mcp, shell, etc.) now
                that the recording layer prefers ``runtime_info.backend``.
            used_tool_keys: Set of ``"backend:tool_name"`` or ``"backend:server:tool_name"``
                strings derived from traj.jsonl.
        """
        if not tool_defs:
            return "none"
        if used_tool_keys is None:
            used_tool_keys = set()

        used_parts = []
        available_parts = []
        for t in tool_defs:
            name = t.get("name", "?")
            backend = t.get("backend", "?")
            server = t.get("server_name")
            label = f"{name} ({backend}/{server})" if server else f"{name} ({backend})"

            # Match by backend:tool or backend:server:tool
            key = f"{backend}:{name}"
            key_with_server = f"{backend}:{server}:{name}" if server else ""
            if key in used_tool_keys or key_with_server in used_tool_keys:
                used_parts.append(label)
            else:
                available_parts.append(label)

        sections = []
        if used_parts:
            sections.append(f"Actually used: {', '.join(used_parts)}")
        if available_parts:
            sections.append(f"Available but unused: {', '.join(available_parts)}")
        return "\n".join(sections) if sections else "none"

    @staticmethod
    def _format_traj_summary(traj_records: List[Dict[str, Any]]) -> str:
        """Format traj.jsonl records into a concise tool execution timeline.

        This provides the LLM with a structured view of every tool invocation
        and its outcome, complementing the conversation log which shows the
        agent's reasoning.
        """
        if not traj_records:
            return "(no traj.jsonl data available)"

        lines = [f"Total tool invocations: {len(traj_records)}"]
        error_count = sum(
            1 for r in traj_records
            if r.get("result", {}).get("status") == "error"
        )
        if error_count:
            lines.append(f"Errors: {error_count}/{len(traj_records)}")

        lines.append("")  # blank line before timeline

        for entry in traj_records:
            step = entry.get("step", "?")
            backend = entry.get("backend", "?")
            tool = entry.get("tool", "?")
            server = entry.get("server", "")
            result = entry.get("result", {})
            status = result.get("status", "?")

            # Build compact one-line summary
            command = entry.get("command", "")
            if isinstance(command, str) and len(command) > 150:
                command = command[:150] + "..."

            # Include server for MCP tools so key is unambiguous
            if server:
                tool_label = f"{backend}:{server}:{tool}"
            else:
                tool_label = f"{backend}:{tool}"
            line = f"  Step {step} [{tool_label}] → {status}"

            # Add error details for failed steps
            if status == "error":
                stderr = result.get("stderr", result.get("output", ""))
                if isinstance(stderr, str) and stderr:
                    # Extract first meaningful line of error
                    error_first_line = stderr.strip().split("\n")[0][:200]
                    line += f" | {error_first_line}"

            evidence = result.get("evidence")
            if isinstance(evidence, dict) and evidence:
                line += (
                    " | evidence: "
                    + json.dumps(evidence, ensure_ascii=False, sort_keys=True)
                )

            # Add brief command context
            if command and not command.startswith("```"):
                line += f" | cmd: {command[:100]}"

            lines.append(line)

        return "\n".join(lines)

    @staticmethod
    def _format_conversations(conversations: List[Dict[str, Any]]) -> str:
        """Format conversations.jsonl into a readable text block for the LLM.

        Delegates to :func:`conversation_formatter.format_conversations`.
        """
        return format_conversations(conversations, _MAX_CONVERSATION_CHARS)

    async def _run_analysis_loop(
        self,
        prompt: str,
        available_tools: Optional[List[BaseTool]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Run analysis as an agent loop with optional tool use.

        Most analyses complete in a single pass (LLM outputs JSON directly).
        When the trace is ambiguous, the LLM may call the execution's own
        tools (``read``, ``ls``, ``bash``, MCP tools, etc.) for deeper
        investigation or error reproduction.

        Uses ``LLMClient.call_model()`` for each model turn and
        ``run_tools()`` for explicit tool execution so the analyzer follows
        the main permission/hook/event flow.

        Conversations are recorded to ``conversations.jsonl`` via
        ``RecordingManager`` (agent_name="ExecutionAnalyzer") so the full
        analysis dialogue is preserved alongside the grounding trace.
        """
        from openspace.recording import RecordingManager

        model = self._model or self._llm_client.model
        analysis_tools: List[BaseTool] = list(available_tools or [])

        messages: List[Dict[str, Any]] = [
            {"role": "user", "content": prompt},
        ]

        # Record initial conversation setup
        await RecordingManager.record_conversation_setup(
            setup_messages=copy.deepcopy(messages),
            tools=analysis_tools if analysis_tools else None,
            agent_name="ExecutionAnalyzer",
        )

        length_recovery_count = 0
        invalid_json_recovery_count = 0
        force_tool_free = False
        for iteration in range(_ANALYSIS_MAX_ITERATIONS):
            is_last = iteration == _ANALYSIS_MAX_ITERATIONS - 1

            # Snapshot message count before any additions + LLM call
            msg_count_before = len(messages)

            # On the final iteration, force JSON output (no tools).
            if is_last:
                messages.append({
                    "role": "system",
                    "content": (
                        "This is your FINAL round — no more tool calls allowed. "
                        "You MUST output the JSON analysis object now based on "
                        "all information gathered so far."
                    ),
                })

            try:
                model_response = await self._call_analysis_model(
                    messages=messages,
                    tools=(
                        analysis_tools
                        if analysis_tools and not is_last and not force_tool_free
                        else None
                    ),
                    model=model,
                    disable_thinking=force_tool_free,
                )
            except Exception as e:
                logger.error(f"Analysis LLM call failed (iter {iteration}): {e}")
                return None
            # Keep fallback state local to this analysis run.  The helper
            # exposes the model that produced the response without mutating
            # the shared client defaults.
            model = model_response.effective_model or model

            assistant_message = model_response.assistant_message
            messages.append(assistant_message)
            raw_content = assistant_message.get("content", "")
            content = (
                raw_content
                if isinstance(raw_content, str)
                else str(raw_content or "")
            )
            has_tool_calls = bool(model_response.tool_calls)
            followup_messages = self._llm_client.get_model_response_followup_messages(
                model_response
            )
            has_api_error = self._llm_client.model_response_has_api_error(
                model_response
            )
            recover_length = bool(
                has_api_error
                and model_response.stop_reason == "length"
                and not has_tool_calls
                and not is_last
                and length_recovery_count < _MAX_ANALYSIS_LENGTH_RECOVERIES
            )
            if recover_length:
                length_recovery_count += 1
                force_tool_free = True
                messages.append(
                    _build_analysis_length_recovery_message(length_recovery_count)
                )
            elif followup_messages:
                messages.extend(followup_messages)
            tool_results: list[dict[str, Any]] = []
            prevent_continuation = False
            tool_stop_reason: str | None = None

            if not has_api_error and has_tool_calls and analysis_tools and not is_last:
                tool_context = self._llm_client.build_auxiliary_tool_use_context(
                    tools=analysis_tools,
                    messages=messages,
                    model=model,
                    agent_id="execution_analyzer",
                    agent_type=self.__class__.__name__,
                    quality_manager=self._quality_manager,
                    task_description=prompt,
                    current_iteration=iteration + 1,
                    max_iterations=_ANALYSIS_MAX_ITERATIONS,
                )
                tools_result = await run_tools(
                    model_response.tool_calls,
                    model_response.tool_map,
                    tool_context,
                    assistant_message=assistant_message,
                )
                messages.extend(tools_result.messages)
                prevent_continuation = tools_result.prevent_continuation
                tool_stop_reason = tools_result.stop_reason
                tool_results = self._llm_client.collect_tool_results(
                    model_response.tool_calls,
                    model_response.tool_map,
                    tools_result.messages,
                )

            # Record iteration delta
            updated_messages = messages
            delta = updated_messages[msg_count_before:]
            await RecordingManager.record_iteration_context(
                iteration=iteration + 1,
                delta_messages=copy.deepcopy(delta),
                response_metadata={
                    "has_tool_calls": has_tool_calls,
                    "tool_calls_count": len(tool_results),
                    "is_final": not has_tool_calls,
                    "prevent_continuation": prevent_continuation,
                    "stop_reason": model_response.stop_reason,
                    "tool_stop_reason": tool_stop_reason,
                    "content_length": len(content),
                    "output_tokens": int(
                        getattr(model_response.usage, "output_tokens", 0) or 0
                    ),
                    "effective_model": model_response.effective_model or model,
                    "has_api_error": has_api_error,
                    "api_error_followup_count": len(followup_messages),
                    "length_recovery_attempt": (
                        length_recovery_count if recover_length else 0
                    ),
                },
                agent_name="ExecutionAnalyzer",
            )

            if has_api_error:
                if recover_length:
                    logger.warning(
                        "Analysis LLM response was truncated; retrying with "
                        "compact JSON (iter=%s stop_reason=%s output_tokens=%s "
                        "content_chars=%s recovery=%s/%s)",
                        iteration + 1,
                        model_response.stop_reason or "unknown",
                        int(getattr(model_response.usage, "output_tokens", 0) or 0),
                        len(content),
                        length_recovery_count,
                        _MAX_ANALYSIS_LENGTH_RECOVERIES,
                    )
                    continue
                logger.warning(
                    "Analysis LLM returned a non-recoverable API error followup "
                    "(iter=%s stop_reason=%s output_tokens=%s content_chars=%s)",
                    iteration + 1,
                    model_response.stop_reason or "unknown",
                    int(getattr(model_response.usage, "output_tokens", 0) or 0),
                    len(content),
                )
                return None

            if prevent_continuation:
                logger.warning(
                    "Analysis agent stopped after tool hooks prevented "
                    "continuation: %s",
                    tool_stop_reason or "hook_stopped",
                )
                return None

            if not has_tool_calls:
                # No tool calls → final response, parse JSON
                analysis = self._extract_json(content)
                if analysis is not None:
                    return analysis
                if (
                    not is_last
                    and invalid_json_recovery_count
                    < _MAX_ANALYSIS_INVALID_JSON_RECOVERIES
                ):
                    invalid_json_recovery_count += 1
                    force_tool_free = True
                    messages.append(
                        _build_analysis_invalid_json_recovery_message(
                            invalid_json_recovery_count
                        )
                    )
                    logger.warning(
                        "Analysis LLM returned invalid JSON; retrying with "
                        "compact JSON (iter=%s content_chars=%s recovery=%s/%s)",
                        iteration + 1,
                        len(content),
                        invalid_json_recovery_count,
                        _MAX_ANALYSIS_INVALID_JSON_RECOVERIES,
                    )
                    continue
                return None

            # Continue with the updated messages (assistant + tool results).
            logger.debug(
                f"Analysis agent used tools "
                f"(iter {iteration + 1}/{_ANALYSIS_MAX_ITERATIONS})"
            )

        # Should not reach here (last iteration disables tools), but just in case
        logger.warning(
            f"Analysis agent reached max iterations ({_ANALYSIS_MAX_ITERATIONS})"
        )
        for m in reversed(messages):
            if m.get("role") == "assistant" and m.get("content"):
                return self._extract_json(m["content"])
        return None

    @staticmethod
    def _extract_json(text: str) -> Optional[Dict[str, Any]]:
        """Extract a JSON object from LLM response text.

        Handles markdown code fences and bare JSON.
        """
        # Try code block first
        code_match = re.search(
            r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL
        )
        if code_match:
            text = code_match.group(1).strip()
        else:
            # Try bare JSON object
            json_match = re.search(r"\{.*\}", text, re.DOTALL)
            if json_match:
                text = json_match.group()

        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
            logger.warning(f"LLM returned non-dict JSON: {type(data)}")
            return None
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse LLM analysis JSON: {e}")
            logger.debug(f"Raw LLM output (first 500 chars): {text[:500]}")
            return None

    @staticmethod
    def _parse_analysis(
        task_id: str,
        data: Dict[str, Any],
        context: Dict[str, Any],
    ) -> Optional[ExecutionAnalysis]:
        """Convert the raw LLM JSON output into an ExecutionAnalysis.

        Also attaches observed tool execution records from ``traj.jsonl``
        so the analysis contains both LLM judgments and factual data.
        """
        try:
            now = datetime.now()

            # Quality counters are only valid for skills that were actually
            # listed/discovered/invoked in this execution. Available catalog
            # IDs are intentionally not accepted as judgments.
            selected_skill_ids = [
                sid for sid in context.get("selected_skills", []) if sid
            ]
            known_skill_ids: set = set(selected_skill_ids)
            skill_sel = context.get("skill_selection") or {}
            suggestion_known_skill_ids: set = set(known_skill_ids)
            for sid in skill_sel.get("available_skills", []):
                if sid:
                    suggestion_known_skill_ids.add(sid)

            # Parse skill judgments (LLM-generated)
            judgments: List[SkillJudgment] = []
            seen_judgment_ids: set = set()
            for jd in data.get("skill_judgments", []):
                raw_sid = jd.get("skill_id", "")
                corrected = _correct_skill_ids([raw_sid], known_skill_ids)
                skill_id = corrected[0] if corrected else raw_sid
                if skill_id not in known_skill_ids:
                    logger.debug(
                        "Ignoring analyzer skill judgment for non-selected skill %r",
                        skill_id,
                    )
                    continue
                if not skill_id or skill_id in seen_judgment_ids:
                    continue
                seen_judgment_ids.add(skill_id)
                judgments.append(
                    SkillJudgment(
                        skill_id=skill_id,
                        skill_applied=_parse_analysis_bool(
                            jd.get("skill_applied", False)
                        ),
                        note=jd.get("note", ""),
                    )
                )

            for sid in selected_skill_ids:
                if sid not in seen_judgment_ids:
                    judgments.append(
                        SkillJudgment(
                            skill_id=sid,
                            skill_applied=False,
                            note=(
                                "Analyzer response omitted this selected/retrieved "
                                "skill; defaulted to not applied for quality accounting."
                            ),
                        )
                    )
                    seen_judgment_ids.add(sid)

            # Parse evolution_suggestions (new format: list of typed suggestions)
            suggestions: List[EvolutionSuggestion] = []
            for raw_sug in data.get("evolution_suggestions", []):
                if not isinstance(raw_sug, dict):
                    logger.debug("Ignoring non-object evolution suggestion")
                    continue
                try:
                    evo_type = EvolutionType(raw_sug.get("type", ""))
                except ValueError:
                    logger.debug(f"Unknown evolution type: {raw_sug.get('type')}")
                    continue

                cat = None
                if raw_sug.get("category"):
                    try:
                        cat = SkillCategory(raw_sug["category"])
                    except ValueError:
                        logger.debug(f"Unknown category: {raw_sug.get('category')}")

                # Support both "target_skills" (list) and legacy "target_skill" (str)
                raw_targets = raw_sug.get("target_skills")
                if isinstance(raw_targets, list):
                    targets = [t for t in raw_targets if t]
                else:
                    legacy = raw_sug.get("target_skill", "")
                    targets = [legacy] if legacy else []

                # Correct LLM-hallucinated skill IDs against known IDs.
                # LLMs frequently swap/drop characters in hex suffixes
                # (e.g. "61f694bc" instead of "61f694cb").
                targets = _correct_skill_ids(targets, suggestion_known_skill_ids)

                raw_contract = raw_sug.get("capture_contract")
                capture_contract = (
                    CaptureContract.from_dict(raw_contract)
                    if isinstance(raw_contract, dict)
                    else None
                )
                suggestions.append(EvolutionSuggestion(
                    evolution_type=evo_type,
                    target_skill_ids=targets,
                    category=cat,
                    local_category_path=str(raw_sug.get("local_category_path") or ""),
                    direction=raw_sug.get("direction", ""),
                    capture_contract=capture_contract,
                ))

            phase_failed_skill_ids = _extract_skill_phase_failed_skill_ids(
                data,
                context,
                known_skill_ids,
            )

            analysis = ExecutionAnalysis(
                task_id=task_id,
                timestamp=now,
                task_completed=_parse_analysis_bool(
                    data.get("task_completed", False)
                ),
                execution_note=data.get("execution_note", ""),
                tool_issues=data.get("tool_issues", []),
                skill_judgments=judgments,
                skill_phase_failed_skill_ids=phase_failed_skill_ids,
                evolution_suggestions=suggestions,
                analyzed_by=data.get("analyzed_by", ""),
                analyzed_at=now,
            )
            return analysis

        except Exception as e:
            logger.error(f"Failed to parse analysis response: {e}")
            return None

    # Convenience queries (delegated to store)
    def get_store(self) -> SkillStore:
        """Access the underlying SkillStore for direct queries."""
        return self._store

    def close(self) -> None:
        """Close the store connection."""
        self._store.close()


def _packet_selected_skill_ids(
    packet: "EvidencePacket",
    runtime_meta: Dict[str, Any],
) -> List[str]:
    ids: list[str] = []
    ids.extend(_coerce_id_list(runtime_meta.get("active_skills")))
    for ref_type in ("skill_file", "skill_event", "skill_record"):
        for ref in packet.selected_refs.get(ref_type, []):
            ids.extend(_coerce_id_list(ref.metadata.get("skill_ids")))
            sid = str(ref.metadata.get("skill_id") or "")
            if sid:
                ids.append(sid)
    return list(dict.fromkeys(ids))


def _packet_skill_contents(packet: "EvidencePacket") -> Dict[str, str]:
    snippets_by_ref = {snippet.ref_id: snippet.text for snippet in packet.expanded_snippets}
    contents: Dict[str, str] = {}
    for ref in packet.selected_refs.get("skill_file", []):
        sid = str(ref.metadata.get("skill_id") or "")
        if not sid:
            continue
        contents[sid] = snippets_by_ref.get(ref.ref_id) or ref.preview[:_SKILL_CONTENT_MAX_CHARS]
    return contents


def _packet_skill_section(packet: "EvidencePacket") -> str:
    skill_refs = packet.selected_refs.get("skill_file", [])
    if not skill_refs:
        return ""
    snippets_by_ref = {snippet.ref_id: snippet.text for snippet in packet.expanded_snippets}
    parts: list[str] = []
    for ref in skill_refs:
        sid = str(ref.metadata.get("skill_id") or ref.ref_id)
        path = str(ref.metadata.get("path") or ref.uri or "")
        content = snippets_by_ref.get(ref.ref_id) or ref.preview
        parts.append(
            f"### {sid}\n"
            f"**Path**: `{path}`\n"
            f"**Evidence ref**: `{ref.ref_id}`\n\n"
            f"{content[:_SKILL_CONTENT_MAX_CHARS]}"
        )
    return "## Selected Skills\n\n" + "\n\n---\n\n".join(parts)


def _packet_skill_execution(packet: "EvidencePacket") -> Dict[str, Any]:
    events: list[dict[str, Any]] = []
    for ref in packet.selected_refs.get("skill_event", []):
        events.append(
            {
                "ref_id": ref.ref_id,
                "skill_id": ref.metadata.get("skill_id"),
                "event_type": ref.metadata.get("event_type")
                or ref.metadata.get("lifecycle_event")
                or ref.metadata.get("status"),
            }
        )
    selected = [
        str(item.get("skill_id"))
        for item in events
        if item.get("skill_id")
        and str(item.get("event_type") or "").lower()
        in {"selected", "listed", "discovered", "invoked", "applied", "completed"}
    ]
    return {
        "selected": list(dict.fromkeys(selected)),
        "events": events,
        "source": "evidence_packet",
    }


def _packet_tool_records(packet: "EvidencePacket") -> List[Dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for ref_type in ("tool_event", "tool_result", "tool_incident"):
        for ref in packet.selected_refs.get(ref_type, []):
            metadata = ref.metadata
            tool_key = str(metadata.get("tool_key") or "")
            tool_name = str(metadata.get("tool_name") or metadata.get("tool") or "")
            backend = ""
            server = ""
            if tool_key:
                parts = tool_key.split(":", 2)
                if len(parts) == 3:
                    backend, server, tool_name = parts
                elif len(parts) == 2:
                    backend, tool_name = parts
            records.append(
                {
                    "ref_id": ref.ref_id,
                    "ref_type": ref.ref_type,
                    "backend": backend,
                    "server": server,
                    "tool": tool_name,
                    "tool_name": tool_name,
                    "tool_key": tool_key,
                    "status": metadata.get("status") or metadata.get("outcome"),
                    "command": metadata.get("input_preview"),
                    "result": {
                        "status": metadata.get("status") or metadata.get("outcome"),
                        "output": metadata.get("result_preview") or ref.preview,
                        "stderr": metadata.get("error_message"),
                    },
                }
            )
    return records


def _packet_tool_defs(
    runtime_meta: Dict[str, Any],
    used_tool_keys: set[str],
) -> List[Dict[str, Any]]:
    tool_defs: list[dict[str, Any]] = []
    retrieved = runtime_meta.get("retrieved_tools_list") or []
    if isinstance(retrieved, list):
        for item in retrieved:
            if isinstance(item, dict):
                tool_defs.append(dict(item))
            elif isinstance(item, str) and item:
                tool_defs.append({"name": item, "backend": "retrieved"})
    for key in sorted(used_tool_keys):
        parts = key.split(":", 2)
        if len(parts) == 3:
            backend, server, name = parts
            tool_defs.append({"name": name, "backend": backend, "server_name": server})
        elif len(parts) == 2:
            backend, name = parts
            tool_defs.append({"name": name, "backend": backend})
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in tool_defs:
        key = (
            str(item.get("backend") or ""),
            str(item.get("server_name") or item.get("server") or ""),
            str(item.get("name") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _packet_snippet_section(
    packet: "EvidencePacket",
    ref_types: set[str],
    *,
    fallback_label: str,
) -> str:
    ref_type_by_id = {
        ref.ref_id: ref.ref_type
        for refs in packet.selected_refs.values()
        for ref in refs
    }
    chunks = [
        snippet.text
        for snippet in packet.expanded_snippets
        if ref_type_by_id.get(snippet.ref_id) in ref_types
    ]
    if chunks:
        return "\n\n".join(chunks)
    previews: list[str] = []
    for ref_type in sorted(ref_types):
        for ref in packet.selected_refs.get(ref_type, []):
            previews.append(
                f"ref_id: {ref.ref_id}\nref_type: {ref.ref_type}\npreview: {ref.preview}"
            )
    return "\n\n".join(previews) if previews else fallback_label


def _packet_resource_info(packet: "EvidencePacket") -> str:
    lines = [
        "Evidence Packet Context:",
        f"- packet_id: {packet.packet_id}",
        f"- trigger_job_id: {packet.trigger_job_id}",
        f"- profile: {packet.profile_name}/{packet.subprofile}",
        "- EvidencePacket is the primary fact source for this analysis.",
        "- recording_ref entries are fallback/debug context only.",
        "- tool_result previews may be incomplete; use readable_paths for full output when needed.",
        "- compact_summary entries are lossy and must be tied back to exact refs.",
        "- Any evolution_suggestions are proposals only, not commands to edit skills.",
    ]
    if packet.profile_name == "quality_signal":
        lines.extend(
            [
                "- This QUALITY_SIGNAL job already exists because rule-based evidence gates created it.",
                "- Do not create, remove, or request TriggerJobs.",
                "- Treat quality_signal_ref as derived evidence, not a final diagnosis or mutation command.",
                "- Use only packet refs; every factual claim behind a proposed change must be supported by selected refs.",
                "- If evidence does not support a skill change, choose no evolution_suggestions.",
                "- If the issue is external, permission, sandbox, API key, or outage related, choose no evolution_suggestions.",
            ]
        )
    if packet.instructions:
        lines.append("Packet instructions:")
        for key, value in sorted(packet.instructions.items()):
            lines.append(f"  - {key}: {value}")
    if packet.readable_paths:
        lines.append("Readable paths:")
        for item in packet.readable_paths:
            state = "readable" if item.readable else f"not_readable:{item.missing_reason}"
            lines.append(
                f"  - {item.ref_id} [{item.purpose}] {state}: `{item.path}`"
            )
    lines.append("Selected refs:")
    for ref_type, refs in sorted(packet.selected_refs.items()):
        lines.append(f"  - {ref_type}: {', '.join(ref.ref_id for ref in refs)}")
    return "\n".join(lines)


def _first_user_packet_preview(packet: "EvidencePacket") -> str:
    for ref in packet.selected_refs.get("transcript_message", []):
        if str(ref.metadata.get("role") or "").lower() == "user":
            text = str(ref.preview or ref.metadata.get("preview") or "").strip()
            if text:
                return text[:500]
    return ""


def _packet_recording_dir(packet: "EvidencePacket") -> str:
    for ref in packet.selected_refs.get("recording_ref", []):
        return str(ref.uri or ref.metadata.get("recording_dir") or "")
    return ""


def _analysis_session_id(
    context: Dict[str, Any],
    *,
    execution_result: Dict[str, Any] | None = None,
) -> str | None:
    if execution_result is not None:
        session_id = _clean_session_id(execution_result.get("session_id"))
        if session_id:
            return session_id
    session_id = _clean_session_id(context.get("session_id"))
    if session_id:
        return session_id
    packet = context.get("packet")
    if packet is not None:
        return _packet_session_id(packet)
    return None


def _packet_session_id(packet: "EvidencePacket") -> str | None:
    scope = getattr(packet, "scope", None)
    session_id = _clean_session_id(getattr(scope, "session_id", None))
    if session_id:
        return session_id
    selected_refs = getattr(packet, "selected_refs", None)
    if not isinstance(selected_refs, dict):
        return None
    refs = [
        ref
        for ref_type in sorted(selected_refs)
        for ref in selected_refs.get(ref_type, []) or []
    ]
    for ref in refs:
        session_id = _clean_session_id(getattr(ref, "session_id", None))
        if session_id:
            return session_id
    for ref in refs:
        metadata = getattr(ref, "metadata", None)
        if isinstance(metadata, dict):
            session_id = _clean_session_id(metadata.get("session_id"))
            if session_id:
                return session_id
    return None


def _clean_session_id(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None
