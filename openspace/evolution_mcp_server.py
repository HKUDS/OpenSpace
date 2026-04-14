"""OpenSpace evolution-only MCP server.

This sidecar is designed for host-agent workflows where the main coding is
handled elsewhere (for example Codex Desktop with subscription auth), while
OpenSpace is only used to capture reusable skills via a separate provider.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import subprocess
import signal
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from openspace.mcp_stdio import maybe_redirect_stderr_to_file
from openspace.mcp_tool_registration import register_evolution_tools

class _MCPSafeStdout:
    """Stdout wrapper: binary (.buffer) -> real stdout, text (.write) -> stderr."""

    def __init__(self, real_stdout, stderr):
        self._real = real_stdout
        self._stderr = stderr

    @property
    def buffer(self):
        return self._real.buffer

    def fileno(self):
        return self._real.fileno()

    def write(self, s):
        return self._stderr.write(s)

    def writelines(self, lines):
        return self._stderr.writelines(lines)

    def flush(self):
        self._stderr.flush()
        try:
            self._real.flush()
        except ValueError:
            pass

    def isatty(self):
        return self._stderr.isatty()

    @property
    def encoding(self):
        return self._stderr.encoding

    @property
    def errors(self):
        return self._stderr.errors

    @property
    def closed(self):
        return self._stderr.closed

    def readable(self):
        return False

    def writable(self):
        return True

    def seekable(self):
        return False

    def __getattr__(self, name):
        return getattr(self._stderr, name)


_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

_real_stdout = sys.stdout
maybe_redirect_stderr_to_file(_LOG_DIR, "evolution_mcp_stderr.log")

sys.stdout = _MCPSafeStdout(_real_stdout, sys.stderr)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(_LOG_DIR / "evolution_mcp_server.log")],
)
logger = logging.getLogger("openspace.evolution_mcp_server")

from mcp.server.fastmcp import FastMCP

_fastmcp_kwargs: dict = {}
try:
    if "description" in inspect.signature(FastMCP.__init__).parameters:
        _fastmcp_kwargs["description"] = (
            "OpenSpace evolution sidecar: capture reusable skills from host-agent work."
        )
except (TypeError, ValueError):
    pass

mcp = FastMCP("OpenSpace Evolution", **_fastmcp_kwargs)

_openspace_instance = None
_openspace_lock = asyncio.Lock()
_UPLOAD_META_FILENAME = ".upload_meta.json"
_idle_watchdog_started = False
_activity_lock = threading.Lock()
_active_request_count = 0
_last_activity_at = time.monotonic()
_shutdown_started = False
_shutdown_lock = threading.Lock()


def _json_ok(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _json_error(error: Any, **extra) -> str:
    return json.dumps({"error": str(error), **extra}, ensure_ascii=False)


def _mark_request_start() -> None:
    global _active_request_count, _last_activity_at
    with _activity_lock:
        _active_request_count += 1
        _last_activity_at = time.monotonic()
    from openspace.shared_mcp_runtime import update_current_daemon_status

    update_current_daemon_status("evolution", touch=True, active_delta=1)


def _mark_request_end() -> None:
    global _active_request_count, _last_activity_at
    with _activity_lock:
        _active_request_count = max(0, _active_request_count - 1)
        _last_activity_at = time.monotonic()
    from openspace.shared_mcp_runtime import update_current_daemon_status

    update_current_daemon_status("evolution", touch=True, active_delta=-1)


def _shutdown_worker(reason: str) -> None:
    logger.info("Shutting down OpenSpace evolution daemon: %s", reason)
    instance = _openspace_instance
    if instance is not None and instance.is_initialized():
        try:
            asyncio.run(asyncio.wait_for(instance.cleanup(), timeout=10.0))
        except Exception as exc:
            logger.warning("OpenSpace evolution cleanup during shutdown failed: %s", exc)
    logging.shutdown()
    os._exit(0)


def _begin_shutdown(reason: str) -> None:
    global _shutdown_started
    with _shutdown_lock:
        if _shutdown_started:
            return
        _shutdown_started = True

    threading.Thread(
        target=_shutdown_worker,
        args=(reason,),
        name="openspace-evolution-shutdown",
        daemon=True,
    ).start()


def _install_signal_handlers() -> None:
    def _handle(signum, _frame) -> None:
        try:
            signame = signal.Signals(signum).name
        except Exception:
            signame = str(signum)
        _begin_shutdown(f"signal {signame}")

    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(signum, _handle)
        except Exception:
            continue


def _idle_watchdog_loop(idle_timeout_seconds: int) -> None:
    check_interval = max(1, min(max(idle_timeout_seconds // 3, 1), 60))
    logger.info("Evolution MCP idle watchdog enabled: timeout=%ss", idle_timeout_seconds)
    while True:
        time.sleep(check_interval)
        with _activity_lock:
            active = _active_request_count
            idle_for = time.monotonic() - _last_activity_at
        if active == 0 and idle_for >= idle_timeout_seconds:
            logger.info(
                "Evolution MCP idle watchdog exiting process after %.1fs idle with no active requests",
                idle_for,
            )
            _begin_shutdown(f"idle timeout after {idle_for:.1f}s")
            return


def _maybe_start_idle_watchdog() -> None:
    global _idle_watchdog_started
    if _idle_watchdog_started:
        return

    timeout_raw = os.environ.get("OPENSPACE_EVOLUTION_MCP_IDLE_TIMEOUT_SECONDS", "").strip()
    if not timeout_raw:
        timeout_raw = os.environ.get("OPENSPACE_MCP_IDLE_TIMEOUT_SECONDS", "").strip()
    if timeout_raw:
        try:
            idle_timeout_seconds = int(timeout_raw)
        except ValueError:
            logger.warning(
                "Invalid evolution MCP idle timeout value=%r "
                "(from OPENSPACE_EVOLUTION_MCP_IDLE_TIMEOUT_SECONDS or OPENSPACE_MCP_IDLE_TIMEOUT_SECONDS)",
                timeout_raw,
            )
            return
    else:
        idle_timeout_seconds = 900

    if idle_timeout_seconds <= 0:
        return

    watchdog = threading.Thread(
        target=_idle_watchdog_loop,
        args=(idle_timeout_seconds,),
        name="openspace-evolution-mcp-idle-watchdog",
        daemon=True,
    )
    watchdog.start()
    _idle_watchdog_started = True


async def _get_openspace():
    global _openspace_instance
    if _openspace_instance is not None and _openspace_instance.is_initialized():
        return _openspace_instance

    async with _openspace_lock:
        if _openspace_instance is not None and _openspace_instance.is_initialized():
            return _openspace_instance

        logger.info("Initializing OpenSpace evolution engine ...")
        from openspace.host_detection import (
            build_grounding_config_path,
            build_llm_kwargs,
            load_runtime_env,
        )
        from openspace.tool_layer import OpenSpace, OpenSpaceConfig

        load_runtime_env()

        env_model = os.environ.get("OPENSPACE_MODEL", "")
        workspace = os.environ.get("OPENSPACE_WORKSPACE")
        enable_rec = os.environ.get("OPENSPACE_ENABLE_RECORDING", "false").lower() in (
            "true",
            "1",
            "yes",
        )
        backend_scope_raw = os.environ.get("OPENSPACE_BACKEND_SCOPE", "shell,system")
        backend_scope = [
            b.strip() for b in backend_scope_raw.split(",") if b.strip()
        ] or None

        config_path = build_grounding_config_path()
        model, llm_kwargs = build_llm_kwargs(env_model)

        config = OpenSpaceConfig(
            llm_model=model,
            llm_kwargs=llm_kwargs,
            workspace_dir=workspace,
            grounding_max_iterations=1,
            enable_recording=enable_rec,
            enable_skill_engine_without_recording=True,
            recording_backends=["shell"] if enable_rec else None,
            backend_scope=backend_scope,
            grounding_config_path=config_path,
        )

        _openspace_instance = OpenSpace(config=config)
        await _openspace_instance.initialize()
        logger.info("OpenSpace evolution engine ready (model=%s).", model)
        return _openspace_instance


def _write_upload_meta(skill_dir: Path, info: Dict[str, Any]) -> None:
    meta = {
        "origin": info.get("origin", "captured"),
        "parent_skill_ids": info.get("parent_skill_ids", []),
        "change_summary": info.get("change_summary", ""),
        "created_by": info.get("created_by", "openspace"),
        "tags": info.get("tags", []),
    }
    (skill_dir / _UPLOAD_META_FILENAME).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _extract_json_object(text: str) -> Dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        parts = raw.split("\n", 1)
        raw = parts[1] if len(parts) == 2 else raw
        if raw.endswith("```"):
            raw = raw[:-3].rstrip()

    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        data = json.loads(raw[start : end + 1])
        if isinstance(data, dict):
            return data

    raise ValueError("LLM did not return a valid JSON object")


def _run_git(args: List[str], cwd: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            text=True,
            capture_output=True,
            check=False,
        )
    except Exception as exc:
        logger.debug("git %s failed: %s", " ".join(args), exc)
        return ""

    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 32].rstrip() + "\n...[truncated]..."


def _normalize_file_paths(
    workspace: Path,
    file_paths: Optional[Iterable[str]],
) -> List[Path]:
    normalized: List[Path] = []
    for raw in file_paths or []:
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = workspace / path
        normalized.append(path.resolve())
    return normalized


def _build_repo_context(
    workspace: Path,
    file_paths: List[Path],
) -> str:
    sections: List[str] = []

    if (workspace / ".git").exists():
        status = _run_git(["status", "--short"], workspace)
        if status:
            sections.append("## Git status\n" + _truncate(status, 4_000))

        diff_stat = _run_git(["diff", "--stat"], workspace)
        if diff_stat:
            sections.append("## Git diff stat\n" + _truncate(diff_stat, 4_000))

        staged_stat = _run_git(["diff", "--cached", "--stat"], workspace)
        if staged_stat:
            sections.append("## Git staged diff stat\n" + _truncate(staged_stat, 4_000))

        if file_paths:
            rel_paths = []
            for path in file_paths:
                try:
                    rel_paths.append(str(path.relative_to(workspace)))
                except ValueError:
                    rel_paths.append(str(path))
            scoped_diff = _run_git(
                ["diff", "--unified=1", "--", *rel_paths],
                workspace,
            )
            if scoped_diff:
                sections.append("## Focused diff\n" + _truncate(scoped_diff, 12_000))

    if file_paths:
        lines = ["## Mentioned files"]
        for path in file_paths:
            lines.append(f"- {path}")
        sections.append("\n".join(lines))

    return "\n\n".join(sections) if sections else "(no repository context available)"


def _existing_skill_names(registry) -> List[str]:
    names = []
    for meta in registry.list_skills():
        names.append(meta.name)
    return sorted(set(names))


def _build_planning_prompt(
    *,
    task: str,
    summary: str,
    workspace: Path,
    repo_context: str,
    existing_skills: List[str],
    max_skills: int,
) -> str:
    skill_list = "\n".join(f"- {name}" for name in existing_skills[:200]) or "(none)"
    return f"""You are deciding which reusable OpenSpace skills should be captured from a completed coding task.

The main coding work was already completed by a host agent. Your job is ONLY to identify reusable patterns worth turning into new skills.

Task:
{task}

Execution summary:
{summary}

Workspace:
{workspace}

Repository context:
{repo_context}

Existing local skill names:
{skill_list}

Return exactly one JSON object with this shape:
{{
  "suggestions": [
    {{
      "category": "workflow",
      "direction": "1-2 sentences describing the reusable pattern to capture."
    }}
  ]
}}

Rules:
- Suggest at most {max_skills} skills.
- Only suggest skills that are reusable across future tasks.
- Categories must be one of: "tool_guide", "workflow", "reference".
- Do not suggest trivial one-step actions.
- Do not restate repo-specific one-off details as a reusable skill.
- Avoid duplicating an existing skill unless the new capability is clearly distinct.
- If nothing is worth capturing, return {{"suggestions": []}}.
"""


async def _plan_suggestions(
    *,
    openspace,
    task: str,
    summary: str,
    workspace: Path,
    repo_context: str,
    max_skills: int,
) -> List[Dict[str, str]]:
    registry = openspace._skill_registry
    if not registry:
        return []

    logger.info(
        "Planning evolution captures for task=%r (max_skills=%d)",
        task[:120],
        max_skills,
    )
    prompt = _build_planning_prompt(
        task=task,
        summary=summary,
        workspace=workspace,
        repo_context=repo_context,
        existing_skills=_existing_skill_names(registry),
        max_skills=max_skills,
    )

    response = await openspace._llm_client.complete(
        messages=prompt,
        execute_tools=False,
        model=openspace.config.llm_model,
    )
    data = _extract_json_object(response["message"]["content"])
    raw_suggestions = data.get("suggestions", [])
    if not isinstance(raw_suggestions, list):
        raise ValueError("suggestions must be a list")

    deduped: List[Dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_suggestions:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category", "")).strip()
        direction = str(item.get("direction", "")).strip()
        if category not in {"tool_guide", "workflow", "reference"} or not direction:
            continue
        key = (category, direction.lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append({"category": category, "direction": direction})
        if len(deduped) >= max_skills:
            break
    logger.info("Planned %d capture suggestion(s)", len(deduped))
    return deduped


async def _prepend_output_dir(openspace, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    registry = openspace._skill_registry
    if not registry:
        return

    if output_dir not in registry._skill_dirs:
        registry._skill_dirs.insert(0, output_dir)

    skill_store = openspace._skill_store
    metas = registry.discover_from_dirs([output_dir])
    if metas and skill_store:
        await skill_store.sync_from_registry(metas)


async def _register_extra_skill_dirs(openspace, dirs: List[Path]) -> None:
    registry = openspace._skill_registry
    skill_store = openspace._skill_store
    if not registry:
        return

    metas = registry.discover_from_dirs(dirs)
    if metas and skill_store:
        await skill_store.sync_from_registry(metas)


async def _evolve_from_context_impl(
    task: str,
    summary: str,
    workspace_dir: str | None = None,
    file_paths: list[str] | None = None,
    max_skills: int = 3,
    skill_dirs: list[str] | None = None,
    output_dir: str | None = None,
) -> str:
    """Capture reusable skills from a completed host-agent task.

    Use this when the main task was already handled by another agent
    (for example Codex Desktop) and OpenSpace should only spend provider
    tokens on post-task skill capture.

    Args:
        task: Short description of the completed task.
        summary: What changed, what was learned, and what seems reusable.
        workspace_dir: Repository/workspace path. Defaults to OPENSPACE_WORKSPACE.
        file_paths: Optional files worth emphasizing when planning captures.
        max_skills: Maximum number of new skills to capture.
        skill_dirs: Optional additional skill directories to register first.
        output_dir: Override directory for new skills. Defaults to the first
            OPENSPACE_HOST_SKILL_DIRS entry.
    """
    _mark_request_start()
    try:
        if not task.strip():
            return _json_error("task is required", status="error")
        if not summary.strip():
            return _json_error("summary is required", status="error")

        openspace = await _get_openspace()
        if not openspace._skill_evolver or not openspace._skill_registry:
            return _json_error("Skill evolution is not enabled", status="error")

        workspace = Path(workspace_dir or openspace.config.workspace_dir or os.getcwd()).resolve()
        normalized_paths = _normalize_file_paths(workspace, file_paths)

        if skill_dirs:
            extra_dirs = [Path(p).expanduser().resolve() for p in skill_dirs if p]
            if extra_dirs:
                await _register_extra_skill_dirs(openspace, extra_dirs)

        if output_dir:
            await _prepend_output_dir(openspace, Path(output_dir).expanduser().resolve())

        repo_context = _build_repo_context(workspace, normalized_paths)
        suggestions = await _plan_suggestions(
            openspace=openspace,
            task=task,
            summary=summary,
            workspace=workspace,
            repo_context=repo_context,
            max_skills=max(0, min(max_skills, 8)),
        )

        if not suggestions:
            return _json_ok(
                {
                    "status": "success",
                    "task": task,
                    "workspace_dir": str(workspace),
                    "suggestion_count": 0,
                    "created_skills": [],
                    "message": "No reusable skill captures were suggested.",
                }
            )

        from openspace.skill_engine import EvolutionContext, EvolutionTrigger
        from openspace.skill_engine.types import (
            EvolutionSuggestion,
            EvolutionType,
            ExecutionAnalysis,
            SkillCategory,
        )

        evolver = openspace._skill_evolver
        task_id = f"sidecar_{uuid.uuid4().hex[:12]}"
        now = datetime.now()
        analysis = ExecutionAnalysis(
            task_id=task_id,
            timestamp=now,
            task_completed=True,
            execution_note=_truncate(summary, 1_500),
            analyzed_by=openspace.config.llm_model,
            analyzed_at=now,
        )

        created_skills: List[Dict[str, Any]] = []
        skipped: List[Dict[str, str]] = []
        for suggestion in suggestions:
            logger.info(
                "Capturing skill (%s): %s",
                suggestion["category"],
                suggestion["direction"][:180],
            )
            ctx = EvolutionContext(
                trigger=EvolutionTrigger.ANALYSIS,
                suggestion=EvolutionSuggestion(
                    evolution_type=EvolutionType.CAPTURED,
                    target_skill_ids=[],
                    category=SkillCategory(suggestion["category"]),
                    direction=suggestion["direction"],
                ),
                source_task_id=task_id,
                recent_analyses=[analysis],
                available_tools=[],
            )
            new_record = await evolver.evolve(ctx)
            if not new_record:
                logger.info("Capture skipped by evolver")
                skipped.append(suggestion)
                continue

            skill_dir = Path(new_record.path).parent if new_record.path else None
            if skill_dir:
                _write_upload_meta(
                    skill_dir,
                    {
                        "origin": new_record.lineage.origin.value,
                        "parent_skill_ids": new_record.lineage.parent_skill_ids,
                        "change_summary": new_record.lineage.change_summary,
                        "created_by": new_record.lineage.created_by or "openspace",
                        "tags": new_record.tags,
                    },
                )

            created_skills.append(
                {
                    "name": new_record.name,
                    "skill_id": new_record.skill_id,
                    "skill_dir": str(skill_dir) if skill_dir else "",
                    "path": new_record.path,
                    "category": suggestion["category"],
                    "direction": suggestion["direction"],
                    "upload_ready": bool(skill_dir),
                }
            )

        return _json_ok(
            {
                "status": "success",
                "task": task,
                "workspace_dir": str(workspace),
                "suggestion_count": len(suggestions),
                "created_skills": created_skills,
                "skipped_suggestions": skipped,
            }
        )
    except Exception as e:
        logger.error("evolve_from_context failed: %s", e, exc_info=True)
        return _json_error(e, status="error")
    finally:
        _mark_request_end()


class _DirectEvolutionToolImplementation:
    async def evolve_from_context(
        self,
        task: str,
        summary: str,
        workspace_dir: str | None = None,
        file_paths: list[str] | None = None,
        max_skills: int = 3,
        skill_dirs: list[str] | None = None,
        output_dir: str | None = None,
    ) -> str:
        return await _evolve_from_context_impl(
            task=task,
            summary=summary,
            workspace_dir=workspace_dir,
            file_paths=file_paths,
            max_skills=max_skills,
            skill_dirs=skill_dirs,
            output_dir=output_dir,
        )


register_evolution_tools(mcp, _DirectEvolutionToolImplementation())


def run_mcp_server() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="OpenSpace Evolution MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
    )
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    if args.transport == "stdio" or os.environ.get("OPENSPACE_MCP_DAEMON") == "1":
        _install_signal_handlers()
        _maybe_start_idle_watchdog()

    mcp.settings.port = args.port
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    run_mcp_server()
