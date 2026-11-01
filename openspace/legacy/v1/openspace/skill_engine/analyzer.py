"""ExecutionAnalyzer — post-execution analysis and skill quality tracking.

Responsibilities:
  1. After each task execution, load recording artifacts.
  2. Build an LLM prompt and obtain an ``ExecutionAnalysis``.
  3. Persist the analysis and update ``SkillRecord`` counters via ``SkillStore``.
  4. Surface evolution candidates for downstream processing.

Integration:
  Instantiated once during ``OpenSpace.initialize()``.
  ``analyze_execution()`` is called in the ``finally`` block of ``OpenSpace.execute()``.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from openspace.grounding.core.tool import BaseTool

from .types import (
    EvolutionSuggestion,
    EvolutionType,
    ExecutionAnalysis,
    SkillCategory,
    SkillJudgment,
)
from .store import SkillStore
from openspace.prompts import SkillEnginePrompts
from openspace.utils.logging import Logger
from .conversation_formatter import format_conversations

if TYPE_CHECKING:
    from openspace.llm import LLMClient
    from openspace.grounding.core.quality import ToolQualityManager
    from .registry import SkillRegistry

logger = Logger.get_logger(__name__)


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
        enabled: bool = True,
        skill_registry: Optional["SkillRegistry"] = None,
        quality_manager: Optional["ToolQualityManager"] = None,
    ) -> None:
        self._store = store
        self._llm_client = llm_client
        self._model = model
        self.enabled = enabled
        self._skill_registry = skill_registry
        self._quality_manager = quality_manager

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
                ambiguous. A lightweight ``run_shell`` tool is always appended.
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

        try:
            from gdpval_bench.token_tracker import set_call_source, reset_call_source
            _src_tok = set_call_source("analyzer")
        except ImportError:
            _src_tok = None

        try:
            # 1. Load recording artifacts
            context = self._load_recording_context(rec_path, execution_result)
            if context is None:
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
            await self._store.record_analysis(analysis)
            evo_types = [s.evolution_type.value for s in analysis.evolution_suggestions]
            logger.info(
                f"Execution analysis saved for task {task_id}: "
                f"completed={analysis.task_completed}, "
                f"skills_judged={len(analysis.skill_judgments)}, "
                f"evolution_suggestions={evo_types or 'none'}"
            )

            # 6. Feed tool issues to quality manager (if available).
            #    Build tool-status map from raw traj records for dedup.
            traj_tool_status = self._build_tool_status_map(
                context.get("traj_records", [])
            )
            await self._record_tool_quality_feedback(analysis, traj_tool_status)

            return analysis

        except Exception as e:
            logger.error(f"Execution analysis failed for task {task_id}: {e}")
            return None
        finally:
            if _src_tok is not None:
                reset_call_source(_src_tok)

    async def get_evolution_candidates(
        self, limit: int = 20
    ) -> List[ExecutionAnalysis]:
        """Return recent analyses flagged as evolution candidates."""
        return self._store.load_evolution_candidates(limit=limit)

    @staticmethod
    def _build_tool_status_map(
        traj_records: List[Dict[str, Any]],
    ) -> Dict[str, bool]:
        """Build {tool_key: has_any_success} from raw traj records.

        Used for deduplication: if all calls for a tool already failed
        (rule-based caught them), there's no need for the LLM to add
        another failure record.
        """
        tool_has_success: Dict[str, bool] = {}
        for entry in traj_records:
            backend = entry.get("backend", "unknown")
            tool = entry.get("tool", "unknown")
            server = entry.get("server", "")
            status = (entry.get("result") or {}).get("status", "unknown")

            # Build canonical key matching the prompt format
            key = f"{backend}:{server}:{tool}" if server else f"{backend}:{tool}"

            if key not in tool_has_success:
                tool_has_success[key] = False
            if status != "error":
                tool_has_success[key] = True
        return tool_has_success

    async def _record_tool_quality_feedback(
        self,
        analysis: ExecutionAnalysis,
        traj_tool_status: Dict[str, bool],
    ) -> None:
        """Feed LLM-identified tool issues to the ToolQualityManager.

        **Deduplication**: The rule-based system already records each tool
        call as success/failure.  The LLM adds value only when it catches
        *semantic* failures the rule-based system missed.

        ``traj_tool_status`` maps ``tool_key → has_any_success_call``.
        If all calls already failed → skip (rule-based caught it).
        If any call was "success" but LLM says problematic → inject correction.
        If tool not in traj → trust LLM (internal/system call).
        """
        if not self._quality_manager or not analysis.tool_issues:
            return
        try:
            filtered_issues: list[str] = []
            for issue in analysis.tool_issues:
                # Extract key from "key — description"
                if "—" in issue:
                    key_part = issue.split("—", 1)[0].strip()
                elif " - " in issue:
                    key_part = issue.split(" - ", 1)[0].strip()
                else:
                    key_part = issue.strip()

                if key_part in traj_tool_status and not traj_tool_status[key_part]:
                    logger.debug(
                        f"Skipping LLM issue for {key_part}: "
                        f"rule-based already recorded all calls as errors"
                    )
                    continue
                filtered_issues.append(issue)

            if not filtered_issues:
                return

            updated = await self._quality_manager.record_llm_tool_issues(
                tool_issues=filtered_issues,
                task_id=analysis.task_id,
            )
            if updated:
                logger.debug(
                    f"Fed {updated} LLM tool issue(s) to ToolQualityManager "
                    f"(filtered from {len(analysis.tool_issues)} total) "
                    f"for task {analysis.task_id}"
                )
        except Exception as e:
            # Quality feedback is best-effort; never break analysis flow
            logger.debug(f"Tool quality feedback failed: {e}")

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

        # Extract key fields from metadata
        task_description = metadata.get(
            "task_description",
            (metadata.get("skill_selection") or {}).get("task", ""),
        )
        if not task_description:
            task_description = execution_result.get("instruction", "")

        skill_selection = metadata.get("skill_selection", {})
        selected_skills = skill_selection.get("selected", [])

        retrieved_tools = metadata.get("retrieved_tools", {})
        tool_defs = retrieved_tools.get("tools", [])
        tool_names = [t.get("name", "") for t in tool_defs]

        # Extract skill content from conversations setup message
        # selected_skills contains skill_ids (stored in metadata by tool_layer)
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
            "task_id": metadata.get("task_id", ""),
            "task_description": task_description,
            "selected_skills": selected_skills,
            "skill_selection": skill_selection,
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
                    f"use read_file(\"{meta.path}\") to see full content]"
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
                if files:
                    resource_lines.append(f"  Files: {', '.join(files)}")

        skill_dirs = {
            sid: info["dir"]
            for sid, info in skill_data.items()
            if info.get("dir") and info["dir"] != "(unknown)"
        }
        if skill_dirs:
            resource_lines.append("**Skill directories**:")
            for sid, d in skill_dirs.items():
                resource_lines.append(f"  - {sid}: `{d}`")

        resource_lines.append(
            "\nYou have `read_file`, `list_dir`, and `run_shell` tools for deeper "
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
        tools (``read_file``, ``list_dir``, ``run_shell``, ``shell_agent``,
        MCP tools, etc.) for deeper investigation or error reproduction.

        Reuses ``LLMClient.complete()`` for retry, rate-limiting, tool
        serialization, and tool execution.

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
                result = await self._llm_client.complete(
                    messages=messages,
                    tools=analysis_tools if not is_last else None,
                    execute_tools=True,
                    model=model,
                )
            except Exception as e:
                logger.error(f"Analysis LLM call failed (iter {iteration}): {e}")
                return None

            content = result["message"].get("content", "")
            has_tool_calls = result["has_tool_calls"]

            # Record iteration delta
            updated_messages = result["messages"]
            delta = updated_messages[msg_count_before:]
            await RecordingManager.record_iteration_context(
                iteration=iteration + 1,
                delta_messages=copy.deepcopy(delta),
                response_metadata={
                    "has_tool_calls": has_tool_calls,
                    "tool_calls_count": len(result.get("tool_results", [])),
                    "is_final": not has_tool_calls,
                },
                agent_name="ExecutionAnalyzer",
            )

            if not has_tool_calls:
                # No tool calls → final response, parse JSON
                return self._extract_json(content)

            # Tools were called and executed by complete() — continue with
            # the updated messages (includes assistant + tool result messages).
            messages = updated_messages
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

            # Collect all known skill IDs from context for fuzzy correction.
            # LLMs often garble hex suffixes when reproducing skill IDs.
            known_skill_ids: set = set()
            for sid in context.get("selected_skills", []):
                known_skill_ids.add(sid)
            # Also include skill IDs from the skill_selection metadata
            skill_sel = context.get("skill_selection") or {}
            for sid in skill_sel.get("available_skills", []):
                known_skill_ids.add(sid)

            # Parse skill judgments (LLM-generated)
            judgments: List[SkillJudgment] = []
            for jd in data.get("skill_judgments", []):
                raw_sid = jd.get("skill_id", "")
                corrected = _correct_skill_ids([raw_sid], known_skill_ids)
                judgments.append(
                    SkillJudgment(
                        skill_id=corrected[0] if corrected else raw_sid,
                        skill_applied=bool(jd.get("skill_applied", False)),
                        note=jd.get("note", ""),
                    )
                )

            # Parse evolution_suggestions (new format: list of typed suggestions)
            suggestions: List[EvolutionSuggestion] = []
            for raw_sug in data.get("evolution_suggestions", []):
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
                targets = _correct_skill_ids(targets, known_skill_ids)

                suggestions.append(EvolutionSuggestion(
                    evolution_type=evo_type,
                    target_skill_ids=targets,
                    category=cat,
                    direction=raw_sug.get("direction", ""),
                ))

            analysis = ExecutionAnalysis(
                task_id=task_id,
                timestamp=now,
                task_completed=bool(data.get("task_completed", False)),
                execution_note=data.get("execution_note", ""),
                tool_issues=data.get("tool_issues", []),
                skill_judgments=judgments,
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
