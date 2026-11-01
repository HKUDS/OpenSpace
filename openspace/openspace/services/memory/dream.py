"""Background auto-memory consolidation ("dream").

OpenSpace consolidates recently written memory through a provider-neutral
lightweight tool loop and emits runtime events for UI progress.

Implemented behavior:
- enabled/auto-memory/subagent gates
- time gate using ``.consolidate-lock`` mtime
- session-count gate with current-session exclusion
- 10-minute scan throttle when time gate passes but session gate does not
- live-process lock and rollback on failure
- memory-dir-scoped tool gate
- best-effort fire-and-forget execution with drain support

Current scope:
- Progress is exposed through ``auto_dream_*`` runtime events.
- Daily-log paths are supported; the prompt also reviews ``logs/`` when present.
- User control is exposed through explicit environment variables.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

from openspace.grounding.core.tool.base import BaseTool
from openspace.services.memory.extract import (
    BASH_TOOL_NAME,
    FILE_EDIT_TOOL_NAME,
    FILE_READ_TOOL_NAME,
    FILE_WRITE_TOOL_NAME,
    GLOB_TOOL_NAME,
    GREP_TOOL_NAME,
    MEMORY_READ_TOOL_NAME,
    MEMORY_WRITE_TOOL_NAME,
    create_auto_mem_can_use_tool,
    extract_written_paths,
)
from openspace.services.memory.daily_log import (
    build_daily_log_consolidation_prompt,
    get_memory_mode,
    mark_log_entries_consolidated,
    scan_unconsolidated_logs,
)
from openspace.services.memory.memdir import (
    DIR_EXISTS_GUIDANCE,
    ENTRYPOINT_NAME,
    MAX_ENTRYPOINT_LINES,
    ensure_memory_dir_exists,
    get_auto_mem_path,
    is_auto_memory_enabled,
)
from openspace.services.memory.paths import find_project_root, get_openspace_config_home_dir
from openspace.services.memory.memory_scan import format_memory_manifest, scan_memory_files
from openspace.services.memory.task_scope import maybe_memory_task_scope_key
from openspace.services.conversation.side_query import run_side_query
from openspace.services.tooling.context import ToolUseContext

logger = logging.getLogger(__name__)


OPENSPACE_AUTO_DREAM_ENABLED_ENV = "OPENSPACE_AUTO_DREAM_ENABLED"
OPENSPACE_DISABLE_AUTO_DREAM_ENV = "OPENSPACE_DISABLE_AUTO_DREAM"
OPENSPACE_AUTO_DREAM_MIN_HOURS_ENV = "OPENSPACE_AUTO_DREAM_MIN_HOURS"
OPENSPACE_AUTO_DREAM_MIN_SESSIONS_ENV = "OPENSPACE_AUTO_DREAM_MIN_SESSIONS"
OPENSPACE_AUTO_DREAM_SCAN_INTERVAL_SECONDS_ENV = (
    "OPENSPACE_AUTO_DREAM_SCAN_INTERVAL_SECONDS"
)
OPENSPACE_AUTO_DREAM_SESSIONS_DIR_ENV = "OPENSPACE_AUTO_DREAM_SESSIONS_DIR"
OPENSPACE_MEMORY_DREAM_MODEL_ENV = "OPENSPACE_MEMORY_DREAM_MODEL"
OPENSPACE_REMOTE_ENV = "OPENSPACE_REMOTE"
OPENSPACE_KAIROS_ACTIVE_ENV = "OPENSPACE_KAIROS_ACTIVE"

DEFAULT_AUTO_DREAM_MIN_HOURS = 24.0
DEFAULT_AUTO_DREAM_MIN_SESSIONS = 5
DEFAULT_SESSION_SCAN_INTERVAL_MS = 10 * 60 * 1000
DEFAULT_MAX_DREAM_TURNS = 5
LOCK_FILE = ".consolidate-lock"
HOLDER_STALE_MS = 60 * 60 * 1000
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")

AppendSystemMessageFn = Callable[[dict[str, Any]], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class AutoDreamConfig:
    """Scheduling knobs equivalent to OpenSpace ``tengu_onyx_plover`` fields."""

    min_hours: float = DEFAULT_AUTO_DREAM_MIN_HOURS
    min_sessions: int = DEFAULT_AUTO_DREAM_MIN_SESSIONS
    scan_interval_ms: int = DEFAULT_SESSION_SCAN_INTERVAL_MS
    model: str | None = None


@dataclass(frozen=True, slots=True)
class DreamTurn:
    """A single assistant turn from the dream agent."""

    text: str
    tool_use_count: int


@dataclass(slots=True)
class DreamTaskState:
    """OS event-side equivalent of OpenSpace ``DreamTaskState``."""

    task_id: str
    phase: str = "starting"
    sessions_reviewing: int = 0
    files_touched: list[str] = field(default_factory=list)
    turns: list[DreamTurn] = field(default_factory=list)


@dataclass(slots=True)
class DreamResult:
    """Auditable result for tests and event payloads.

    OpenSpace ``executeAutoDream`` resolves ``void``.  OS returns a result when called
    directly while keeping the stop-hook submit path fire-and-forget.
    """

    ran: bool = False
    skipped_reason: str | None = None
    sessions_reviewed: int = 0
    files_touched: list[str] = field(default_factory=list)
    turn_count: int = 0
    error: str | None = None
    duration_ms: float = 0.0


def _env_truthy(value: str | None) -> bool:
    return value is not None and value.lower() in {"1", "true", "yes", "on"}


def _env_defined_falsy(value: str | None) -> bool:
    return value is not None and value.lower() in {"0", "false", "no", "off", ""}


def is_auto_dream_enabled() -> bool:
    """Return whether background consolidation is enabled.

    OpenSpace reads ``settings.json:autoDreamEnabled`` first, then GrowthBook.  OS keeps
    only the canonical ``autoDream.enabled`` setting with env overrides.
    """

    if (
        _env_truthy(os.environ.get(OPENSPACE_REMOTE_ENV))
        and not os.environ.get("OPENSPACE_REMOTE_MEMORY_DIR")
    ):
        return False
    disabled = os.environ.get(OPENSPACE_DISABLE_AUTO_DREAM_ENV)
    if _env_truthy(disabled):
        return False
    if _env_defined_falsy(disabled):
        return True
    explicit = os.environ.get(OPENSPACE_AUTO_DREAM_ENABLED_ENV)
    if explicit is not None:
        return _env_truthy(explicit)
    try:
        from openspace.services.runtime_support.settings import get_setting

        return bool(get_setting("autoDream.enabled", False))
    except Exception:
        return False


def get_auto_dream_config() -> AutoDreamConfig:
    """Parse Auto Dream scheduling config with OpenSpace's defensive validation."""

    def setting(path: str, default: Any) -> Any:
        try:
            from openspace.services.runtime_support.settings import get_setting

            return get_setting(path, default)
        except Exception:
            return default

    return AutoDreamConfig(
        min_hours=_parse_positive_float(
            os.environ.get(OPENSPACE_AUTO_DREAM_MIN_HOURS_ENV),
            float(setting("autoDream.minHours", DEFAULT_AUTO_DREAM_MIN_HOURS)),
        ),
        min_sessions=_parse_positive_int(
            os.environ.get(OPENSPACE_AUTO_DREAM_MIN_SESSIONS_ENV),
            int(setting("autoDream.minSessions", DEFAULT_AUTO_DREAM_MIN_SESSIONS)),
        ),
        scan_interval_ms=int(
            _parse_positive_float(
                os.environ.get(OPENSPACE_AUTO_DREAM_SCAN_INTERVAL_SECONDS_ENV),
                float(
                    setting(
                        "autoDream.scanIntervalSeconds",
                        DEFAULT_SESSION_SCAN_INTERVAL_MS / 1000,
                    )
                ),
            )
            * 1000
        ),
        model=os.environ.get(OPENSPACE_MEMORY_DREAM_MODEL_ENV)
        or setting("autoDream.model", None),
    )


def _parse_positive_float(raw: str | None, default: float) -> float:
    if not raw:
        return default
    try:
        parsed = float(raw)
    except ValueError:
        return default
    return parsed if parsed > 0 and parsed < float("inf") else default


def _parse_positive_int(raw: str | None, default: int) -> int:
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def build_consolidation_prompt(
    memory_root: str | Path,
    transcript_dir: str | Path,
    extra: str = "",
) -> str:
    """Build OpenSpace's consolidation prompt with OpenSpace path values."""

    prompt = f"""# Dream: Memory Consolidation

You are performing a dream - a reflective pass over your memory files. Synthesize what you've learned recently into durable, well-organized memories so that future sessions can orient quickly.

Memory directory: `{memory_root}`
{DIR_EXISTS_GUIDANCE}

Session transcripts: `{transcript_dir}` (large JSONL/NDJSON files - grep narrowly, don't read whole files)

---

## Phase 1 - Orient

- `ls` the memory directory to see what already exists
- Read `{ENTRYPOINT_NAME}` to understand the current index
- Skim existing topic files so you improve them rather than creating duplicates
- If `logs/` or `sessions/` subdirectories exist (assistant-mode layout), review recent entries there

## Phase 2 - Gather recent signal

Look for new information worth persisting. Sources in rough priority order:

1. **Daily logs** (`logs/YYYY/MM/YYYY-MM-DD.md`) if present - these are the append-only stream
2. **Existing memories that drifted** - facts that contradict something you see in the codebase now
3. **Transcript search** - if you need specific context (e.g., "what was the error message from yesterday's build failure?"), grep the transcripts for narrow terms:
   `grep -rn "<narrow term>" {transcript_dir}/ --include="*.jsonl" --include="*.messages" | tail -50`

Don't exhaustively read transcripts. Look only for things you already suspect matter.

## Phase 3 - Consolidate

For each thing worth remembering, write or update a memory file at the top level of the memory directory. Use the memory file format and type conventions from your system prompt's auto-memory section - it's the source of truth for what to save, how to structure it, and what NOT to save.

Focus on:
- Merging new signal into existing topic files rather than creating near-duplicates
- Converting relative dates ("yesterday", "last week") to absolute dates so they remain interpretable after time passes
- Deleting contradicted facts - if today's investigation disproves an old memory, fix it at the source

## Phase 4 - Prune and index

Update `{ENTRYPOINT_NAME}` so it stays under {MAX_ENTRYPOINT_LINES} lines AND under ~25KB. It's an **index**, not a dump - each entry should be one line under ~150 characters: `- [Title](file.md) - one-line hook`. Never write memory content directly into it.

- Remove pointers to memories that are now stale, wrong, or superseded
- Demote verbose entries: if an index line is over ~200 chars, it's carrying content that belongs in the topic file - shorten the line, move the detail
- Add pointers to newly important memories
- Resolve contradictions - if two files disagree, fix the wrong one

---

Return a brief summary of what you consolidated, updated, or pruned. If nothing changed (memories are already tight), say so."""
    if extra:
        prompt += f"\n\n## Additional context\n\n{extra}"
    return prompt


def read_last_consolidated_at(memory_dir: str | Path) -> float:
    """Return lock mtime in milliseconds, or 0 if absent."""

    try:
        return _lock_path(memory_dir).stat().st_mtime * 1000
    except OSError:
        return 0.0


def try_acquire_consolidation_lock(memory_dir: str | Path) -> float | None:
    """Acquire the Auto Dream lock.

    Returns the pre-acquire mtime in milliseconds, or ``None`` if another live
    process owns a fresh lock.
    """

    root = Path(memory_dir).expanduser().resolve()
    path = _lock_path(root)
    prior_mtime: float | None = None
    holder_pid: int | None = None
    try:
        stat = path.stat()
        prior_mtime = stat.st_mtime * 1000
        raw = path.read_text(encoding="utf-8").strip()
        holder_pid = int(raw) if raw else None
    except (OSError, ValueError):
        pass

    if prior_mtime is not None and (time.time() * 1000 - prior_mtime) < HOLDER_STALE_MS:
        if holder_pid is not None and _is_process_running(holder_pid):
            logger.debug("[autoDream] lock held by live PID %s", holder_pid)
            return None

    root.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()), encoding="utf-8")
    try:
        verify = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if verify != os.getpid():
        return None
    return prior_mtime or 0.0


def rollback_consolidation_lock(memory_dir: str | Path, prior_mtime: float) -> None:
    """Rewind the lock mtime after a failed consolidation."""

    path = _lock_path(memory_dir)
    try:
        if prior_mtime == 0:
            path.unlink(missing_ok=True)
            return
        path.write_text("", encoding="utf-8")
        seconds = prior_mtime / 1000
        os.utime(path, (seconds, seconds))
    except OSError:
        logger.debug("[autoDream] rollback failed", exc_info=True)


def _lock_path(memory_dir: str | Path) -> Path:
    return Path(memory_dir).expanduser().resolve() / LOCK_FILE


def _is_process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def get_session_transcript_dir(sessions_dir: str | Path | None = None) -> Path:
    """Return the directory Auto Dream should scan for session transcripts."""

    raw = sessions_dir or os.environ.get(OPENSPACE_AUTO_DREAM_SESSIONS_DIR_ENV)
    if raw:
        return Path(raw).expanduser().resolve()
    return get_openspace_config_home_dir() / "sessions"


def list_sessions_touched_since(
    since_ms: float,
    *,
    cwd: str | Path | None = None,
    sessions_dir: str | Path | None = None,
    current_session_id: str | None = None,
) -> list[str]:
    """Return session ids with transcript mtime after ``since_ms``.

    OpenSpace scans per-project JSONL transcripts.  OS stores ``<id>.messages`` plus
    ``<id>.json`` metadata under ``~/.openspace/sessions``; we use the
    transcript mtime and filter metadata to the current project when possible.
    """

    root = get_session_transcript_dir(sessions_dir)
    if not root.exists():
        return []

    candidates: dict[str, float] = {}
    for path in root.iterdir():
        if path.suffix not in {".messages", ".json"}:
            continue
        session_id = path.stem
        if not session_id or not SESSION_ID_RE.match(session_id):
            continue
        if session_id == current_session_id:
            continue
        try:
            mtime_ms = path.stat().st_mtime * 1000
        except OSError:
            continue
        if mtime_ms <= since_ms:
            continue
        if cwd is not None and not _session_matches_project(root, session_id, cwd):
            continue
        candidates[session_id] = max(candidates.get(session_id, 0.0), mtime_ms)

    return [
        session_id
        for session_id, _mtime in sorted(
            candidates.items(),
            key=lambda item: item[1],
            reverse=True,
        )
    ]


def _session_matches_project(root: Path, session_id: str, cwd: str | Path) -> bool:
    meta_path = root / f"{session_id}.json"
    if not meta_path.exists():
        return True
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return True

    current = Path(cwd).expanduser().resolve()
    current_root = find_project_root(current)
    for key in ("project_path", "workspace_dir", "worktree_path"):
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            candidate = Path(value).expanduser().resolve()
        except OSError:
            continue
        if candidate == current or candidate == current_root:
            return True
        if find_project_root(candidate) == current_root:
            return True
    return False


def should_schedule_auto_dream(context: ToolUseContext) -> bool:
    """Cheap gate used by stop hooks before creating a background task."""

    if _should_skip_context(context):
        return False
    if not _is_gate_open():
        return False
    return True


class AutoDreamer:
    """Stateful background memory consolidator."""

    def __init__(
        self,
        *,
        max_turns: int = DEFAULT_MAX_DREAM_TURNS,
        config: AutoDreamConfig | None = None,
    ) -> None:
        self.max_turns = max(1, int(max_turns))
        self.config = config
        self._last_session_scan_at_by_dir: dict[str, float] = {}
        self._in_flight: set[asyncio.Task[DreamResult]] = set()
        self._task_scope_keys: dict[asyncio.Task[DreamResult], str | None] = {}

    async def execute(
        self,
        context: ToolUseContext,
        append_system_message: AppendSystemMessageFn | None = None,
    ) -> DreamResult:
        task = asyncio.current_task()
        if task is not None:
            self._in_flight.add(task)  # type: ignore[arg-type]
            self._task_scope_keys[task] = maybe_memory_task_scope_key(context)  # type: ignore[index]
        try:
            return await self._execute_impl(
                context,
                append_system_message,
                manual=False,
            )
        finally:
            if task is not None:
                self._in_flight.discard(task)  # type: ignore[arg-type]
                self._task_scope_keys.pop(task, None)

    async def execute_manual(
        self,
        context: ToolUseContext,
        append_system_message: AppendSystemMessageFn | None = None,
        *,
        extra_context: str = "",
        logs_mode: bool = False,
    ) -> DreamResult:
        """Run a user-triggered dream.

        Manual ``/dream`` shares the consolidation lock, memory-scoped tool
        gate, progress events, and memory-saved notification path with
        background Auto Dream.  It intentionally bypasses only the automatic
        scheduler gates: AutoDream enabled setting, KAIROS/remote skip, time
        gate, session-count gate, and scan throttle.
        """

        task = asyncio.current_task()
        if task is not None:
            self._in_flight.add(task)  # type: ignore[arg-type]
            self._task_scope_keys[task] = maybe_memory_task_scope_key(context)  # type: ignore[index]
        try:
            return await self._execute_impl(
                context,
                append_system_message,
                manual=True,
                manual_extra_context=extra_context,
                force_logs_mode=logs_mode,
            )
        finally:
            if task is not None:
                self._in_flight.discard(task)  # type: ignore[arg-type]
                self._task_scope_keys.pop(task, None)

    def submit(
        self,
        context: ToolUseContext,
        append_system_message: AppendSystemMessageFn | None = None,
    ) -> asyncio.Task[DreamResult]:
        task = asyncio.create_task(self.execute(context, append_system_message))
        self._in_flight.add(task)
        self._task_scope_keys[task] = maybe_memory_task_scope_key(context)

        def _done(done: asyncio.Task[DreamResult]) -> None:
            self._in_flight.discard(done)
            self._task_scope_keys.pop(done, None)
            try:
                done.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("Background auto dream task failed", exc_info=True)

        task.add_done_callback(_done)
        return task

    async def drain(
        self,
        timeout_s: float = 60.0,
        *,
        context: Any | None = None,
        scope_key: str | None = None,
    ) -> int:
        scope_key = scope_key or maybe_memory_task_scope_key(context)
        tasks = [
            task
            for task in self._in_flight
            if scope_key is None or self._task_scope_keys.get(task) == scope_key
        ]
        if not tasks:
            return 0
        done, _pending = await asyncio.wait(
            tasks,
            timeout=max(0.0, timeout_s),
        )
        for task in done:
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("Pending auto dream failed during drain", exc_info=True)
        return len(_pending)

    async def _execute_impl(
        self,
        context: ToolUseContext,
        append_system_message: AppendSystemMessageFn | None,
        *,
        manual: bool = False,
        manual_extra_context: str = "",
        force_logs_mode: bool = False,
    ) -> DreamResult:
        if _should_skip_context(context):
            await _emit_dream_skipped(context, "subagent", manual=manual)
            return DreamResult(skipped_reason="subagent")
        if not manual:
            if _env_truthy(os.environ.get(OPENSPACE_REMOTE_ENV)):
                await _emit_dream_skipped(context, "remote_mode", manual=manual)
                return DreamResult(skipped_reason="remote_mode")
            if not is_auto_dream_enabled():
                await _emit_dream_skipped(context, "disabled", manual=manual)
                return DreamResult(skipped_reason="disabled")
        if not is_auto_memory_enabled():
            await _emit_dream_skipped(context, "auto_memory_disabled", manual=manual)
            return DreamResult(skipped_reason="auto_memory_disabled")

        memory_mode = get_memory_mode(getattr(context, "memory_mode", None))
        daily_log_mode = force_logs_mode or memory_mode == "daily_log"

        cfg = self.config or get_auto_dream_config()
        memory_dir = get_auto_mem_path(cwd=context.cwd)
        memory_dir_key = str(Path(memory_dir).expanduser().resolve())
        try:
            last_at = read_last_consolidated_at(memory_dir)
        except Exception as exc:
            logger.debug("[autoDream] readLastConsolidatedAt failed: %s", exc)
            await _emit_dream_skipped(
                context,
                "read_last_failed",
                manual=manual,
                memory_dir=str(memory_dir),
                error=str(exc),
            )
            return DreamResult(skipped_reason="read_last_failed", error=str(exc))

        hours_since = (time.time() * 1000 - last_at) / 3_600_000
        if not manual and hours_since < cfg.min_hours:
            await _emit_dream_skipped(
                context,
                "time_gate",
                manual=manual,
                memory_dir=str(memory_dir),
                hours_since=hours_since,
            )
            return DreamResult(skipped_reason="time_gate")

        since_scan_ms = (
            time.time() * 1000
            - self._last_session_scan_at_by_dir.get(memory_dir_key, 0.0)
        )
        if not manual and since_scan_ms < cfg.scan_interval_ms:
            logger.debug(
                "[autoDream] scan throttle - last scan was %.1fs ago",
                since_scan_ms / 1000,
            )
            await _emit_dream_skipped(
                context,
                "scan_throttle",
                manual=manual,
                memory_dir=str(memory_dir),
                since_scan_ms=since_scan_ms,
            )
            return DreamResult(skipped_reason="scan_throttle")
        if not manual:
            self._last_session_scan_at_by_dir[memory_dir_key] = time.time() * 1000

        try:
            session_ids = list_sessions_touched_since(
                last_at,
                cwd=context.cwd,
                current_session_id=getattr(context, "session_id", None),
            )
        except Exception as exc:
            logger.debug("[autoDream] listSessionsTouchedSince failed: %s", exc)
            await _emit_dream_skipped(
                context,
                "session_scan_failed",
                manual=manual,
                memory_dir=str(memory_dir),
                error=str(exc),
            )
            return DreamResult(skipped_reason="session_scan_failed", error=str(exc))

        if not manual and len(session_ids) < cfg.min_sessions:
            logger.debug(
                "[autoDream] skip - %s sessions since last consolidation, need %s",
                len(session_ids),
                cfg.min_sessions,
            )
            await _emit_dream_skipped(
                context,
                "session_gate",
                manual=manual,
                memory_dir=str(memory_dir),
                sessions_reviewed=len(session_ids),
            )
            return DreamResult(
                skipped_reason="session_gate",
                sessions_reviewed=len(session_ids),
            )

        llm_client = context.llm_client
        if llm_client is None or not hasattr(llm_client, "call_model"):
            await _emit_dream_skipped(
                context,
                "missing_llm_client",
                manual=manual,
                memory_dir=str(memory_dir),
                sessions_reviewed=len(session_ids),
            )
            return DreamResult(
                skipped_reason="missing_llm_client",
                sessions_reviewed=len(session_ids),
            )

        tools = _select_dream_tools(context)
        if not tools:
            await _emit_dream_skipped(
                context,
                "missing_tools",
                manual=manual,
                memory_dir=str(memory_dir),
                sessions_reviewed=len(session_ids),
            )
            return DreamResult(
                skipped_reason="missing_tools",
                sessions_reviewed=len(session_ids),
            )

        daily_log_scan = None
        if daily_log_mode:
            daily_log_scan = scan_unconsolidated_logs(memory_dir)
            if not daily_log_scan.entries:
                await _emit_dream_skipped(
                    context,
                    "no_daily_log_entries",
                    manual=manual,
                    memory_dir=str(memory_dir),
                    sessions_reviewed=len(session_ids),
                )
                return DreamResult(
                    skipped_reason="no_daily_log_entries",
                    sessions_reviewed=len(session_ids),
                )

        prior_mtime = try_acquire_consolidation_lock(memory_dir)
        if prior_mtime is None:
            await _emit_dream_skipped(
                context,
                "lock_busy",
                manual=manual,
                memory_dir=str(memory_dir),
                sessions_reviewed=len(session_ids),
            )
            return DreamResult(
                skipped_reason="lock_busy",
                sessions_reviewed=len(session_ids),
            )

        start_time = time.time()
        task_state = DreamTaskState(
            task_id=(
                _background_task_id(context, "auto_dream")
                or f"dream-{int(start_time * 1000)}"
            ),
            sessions_reviewing=len(session_ids),
        )
        await context.emit_event(
            "auto_dream_start",
            {
                "task_id": task_state.task_id,
                "sessions_reviewing": len(session_ids),
                "memory_dir": str(memory_dir),
                "manual": manual,
            },
        )

        try:
            ensure_memory_dir_exists(memory_dir)
            transcript_dir = get_session_transcript_dir()
            extra = _build_auto_dream_extra(
                session_ids,
                manual=manual,
                extra_context=manual_extra_context,
            )
            if daily_log_mode and daily_log_scan is not None:
                manifest = format_memory_manifest(scan_memory_files(memory_dir))
                daily_extra = build_daily_log_consolidation_prompt(
                    memory_dir,
                    daily_log_scan.log_paths,
                    manifest,
                    daily_log_scan.entries,
                )
                extra = "\n\n".join(part for part in (extra, daily_extra) if part)
            prompt = build_consolidation_prompt(memory_dir, transcript_dir, extra)
            can_use_tool = create_auto_mem_can_use_tool(memory_dir)
            result_messages: list[dict[str, Any]] = []
            turn_count = 0

            async def on_side_message(
                message: dict[str, Any],
                _side_context: Any,
            ) -> None:
                if message.get("role") != "assistant":
                    return
                await _record_dream_progress(
                    task_state,
                    message,
                    memory_dir=memory_dir,
                    context=context,
                )

            model_override = os.environ.get(OPENSPACE_MEMORY_DREAM_MODEL_ENV) or cfg.model
            side_result = await run_side_query(
                prompt,
                tools=tools,
                model=model_override,
                parent_context=context,
                llm_client=llm_client,
                messages=list(context.messages or []),
                max_turns=self.max_turns,
                can_use_tool=can_use_tool,
                query_source="auto_dream",
                fork_label="auto_dream",
                agent_type="auto_dream",
                denied_result_type="auto_dream_tool_denied",
                tui_available=False,
                is_async_agent=True,
                on_message=on_side_message,
            )
            if side_result.aborted:
                raise asyncio.CancelledError()
            result_messages = side_result.messages
            total_usage = side_result.total_usage
            turn_count = side_result.turn_count

            touched_paths = _uniq(
                [
                    *task_state.files_touched,
                    *extract_written_paths(result_messages, memory_dir=memory_dir),
                ]
            )
            log_entry_ids: list[str] = []
            if daily_log_mode and daily_log_scan is not None:
                log_entry_ids = daily_log_scan.entry_ids
                mark_log_entries_consolidated(
                    memory_dir,
                    log_entry_ids,
                    consolidated_to=touched_paths or "dropped",
                )
            duration_ms = (time.time() - start_time) * 1000
            await context.emit_event(
                "auto_dream_complete",
                {
                    "task_id": task_state.task_id,
                    "sessions_reviewed": len(session_ids),
                    "files_touched": touched_paths,
                    "turn_count": turn_count,
                    "input_tokens": total_usage.input_tokens,
                    "output_tokens": total_usage.output_tokens,
                    "duration_ms": duration_ms,
                    "source": "daily_log" if daily_log_mode else "direct",
                    "manual": manual,
                    "log_entry_ids": log_entry_ids,
                },
            )
            if touched_paths:
                await _append_memory_improved_message(
                    context,
                    touched_paths,
                    append_system_message,
                    source="daily_log" if daily_log_mode else "direct",
                    log_entry_ids=log_entry_ids,
                )
            return DreamResult(
                ran=True,
                sessions_reviewed=len(session_ids),
                files_touched=touched_paths,
                turn_count=turn_count,
                duration_ms=duration_ms,
            )
        except asyncio.CancelledError:
            rollback_consolidation_lock(memory_dir, prior_mtime)
            await context.emit_event(
                "auto_dream_cancelled",
                {"task_id": task_state.task_id, "manual": manual},
            )
            return DreamResult(
                ran=True,
                skipped_reason="aborted",
                sessions_reviewed=len(session_ids),
                files_touched=list(task_state.files_touched),
                turn_count=len(task_state.turns),
                duration_ms=(time.time() - start_time) * 1000,
            )
        except Exception as exc:
            rollback_consolidation_lock(memory_dir, prior_mtime)
            duration_ms = (time.time() - start_time) * 1000
            logger.debug("[autoDream] fork failed: %s", exc, exc_info=True)
            await context.emit_event(
                "auto_dream_error",
                {
                    "task_id": task_state.task_id,
                    "sessions_reviewed": len(session_ids),
                    "duration_ms": duration_ms,
                    "error": str(exc),
                    "manual": manual,
                },
            )
            return DreamResult(
                ran=True,
                sessions_reviewed=len(session_ids),
                files_touched=list(task_state.files_touched),
                turn_count=len(task_state.turns),
                duration_ms=duration_ms,
                error=str(exc),
            )


def _should_skip_context(context: ToolUseContext) -> bool:
    if getattr(context, "is_async_agent", False):
        return True
    if getattr(context, "parent_task_id", None):
        return True
    if getattr(context, "agent_type", None) in {"extract_memories", "auto_dream"}:
        return True
    return False


def _background_task_id(context: ToolUseContext, key: str) -> str | None:
    task_ids = getattr(context, "background_task_ids", None)
    if isinstance(task_ids, dict):
        value = task_ids.get(key)
        return str(value) if value else None
    return None


async def _emit_dream_skipped(
    context: ToolUseContext,
    reason: str,
    *,
    manual: bool,
    **extra: Any,
) -> None:
    event_type = "manual_dream_skipped" if manual else "auto_dream_skipped"
    await context.emit_event(
        event_type,
        {
            "task_id": _background_task_id(context, "auto_dream"),
            "reason": reason,
            "manual": manual,
            **extra,
        },
    )


def _is_gate_open() -> bool:
    """OpenSpace ``isGateOpen`` equivalent with OS env-backed state."""

    if _env_truthy(os.environ.get(OPENSPACE_REMOTE_ENV)):
        return False
    if not is_auto_memory_enabled():
        return False
    return is_auto_dream_enabled()


def _build_auto_dream_extra(
    session_ids: Sequence[str],
    *,
    manual: bool = False,
    extra_context: str = "",
) -> str:
    lines = [
        "",
        "**Tool constraints for this run:** Bash is restricted to read-only commands (`ls`, `find`, `grep`, `cat`, `stat`, `wc`, `head`, `tail`, and similar). Anything that writes, redirects to a file, or modifies state will be denied. Plan your exploration with this in mind - no need to probe.",
        "",
    ]
    if manual:
        lines.append("This dream was explicitly triggered by the user, so do a focused consolidation pass even if automatic scheduling thresholds have not been met.")
        lines.append("")
    lines.extend(
        [
            f"Sessions since last consolidation ({len(session_ids)}):",
            *[f"- {session_id}" for session_id in session_ids],
        ]
    )
    if extra_context.strip():
        lines.extend(["", "User-supplied context for this dream:", extra_context.strip()])
    return "\n".join(lines)


def _select_dream_tools(context: ToolUseContext) -> list[BaseTool]:
    allowed = {
        FILE_READ_TOOL_NAME,
        GREP_TOOL_NAME,
        GLOB_TOOL_NAME,
        BASH_TOOL_NAME,
        FILE_EDIT_TOOL_NAME,
        FILE_WRITE_TOOL_NAME,
        MEMORY_READ_TOOL_NAME,
        MEMORY_WRITE_TOOL_NAME,
    }
    selected: list[BaseTool] = []
    seen: set[str] = set()
    for tool in [*(context.all_tools or []), *(context.tools or [])]:
        name = getattr(tool, "name", "")
        if name in allowed and name not in seen:
            selected.append(tool)
            seen.add(name)

    try:
        if MEMORY_WRITE_TOOL_NAME not in seen:
            from openspace.tools.memory_tools import MemoryWriteTool

            selected.append(MemoryWriteTool())
            seen.add(MEMORY_WRITE_TOOL_NAME)
        if MEMORY_READ_TOOL_NAME not in seen:
            from openspace.tools.memory_tools import MemoryReadTool

            selected.append(MemoryReadTool())
            seen.add(MEMORY_READ_TOOL_NAME)
    except Exception:
        pass

    return selected


async def _record_dream_progress(
    task_state: DreamTaskState,
    assistant_message: Mapping[str, Any],
    *,
    memory_dir: str | Path,
    context: ToolUseContext,
) -> None:
    text = ""
    tool_use_count = 0
    touched_paths: list[str] = []

    content = assistant_message.get("content")
    if isinstance(content, str):
        text += content
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, Mapping):
                continue
            if block.get("type") == "text":
                raw = block.get("text")
                if isinstance(raw, str):
                    text += raw
            elif block.get("type") == "tool_use":
                tool_use_count += 1
                touched_paths.extend(
                    _touched_paths_from_tool_call(block, memory_dir=memory_dir)
                )

    tool_calls = assistant_message.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            if isinstance(call, Mapping):
                tool_use_count += 1
                touched_paths.extend(
                    _touched_paths_from_tool_call(call, memory_dir=memory_dir)
                )

    touched_paths = _uniq(touched_paths)
    if not text.strip() and tool_use_count == 0 and not touched_paths:
        return
    task_state.turns.append(DreamTurn(text=text.strip(), tool_use_count=tool_use_count))
    if touched_paths:
        task_state.phase = "updating"
        task_state.files_touched = _uniq([*task_state.files_touched, *touched_paths])
    task_state.turns = task_state.turns[-30:]
    await context.emit_event(
        "auto_dream_progress",
        {
            "task_id": task_state.task_id,
            "phase": task_state.phase,
            "turn": {
                "text": text.strip(),
                "tool_use_count": tool_use_count,
            },
            "files_touched": list(task_state.files_touched),
        },
    )


def _touched_paths_from_tool_call(
    tool_call: Mapping[str, Any],
    *,
    memory_dir: str | Path,
) -> list[str]:
    tool_name = _tool_call_name(tool_call)
    tool_input = _tool_call_input(tool_call)
    if tool_name in {FILE_EDIT_TOOL_NAME, FILE_WRITE_TOOL_NAME}:
        path = tool_input.get("file_path")
        if isinstance(path, str) and _path_is_inside(path, Path(memory_dir)):
            return [str(Path(path).expanduser().resolve())]
    if tool_name == MEMORY_WRITE_TOOL_NAME:
        filename = tool_input.get("filename")
        title = tool_input.get("title") or "memory"
        if isinstance(filename, str) and filename.strip():
            candidate = Path(memory_dir) / filename
        else:
            slug = "".join(
                ch.lower() if ch.isalnum() else "_"
                for ch in str(title).strip()
            ).strip("_")[:80] or "memory"
            candidate = Path(memory_dir) / f"{slug}.md"
        return [str(candidate.expanduser().resolve())]
    return []


def _tool_call_name(tool_call: Mapping[str, Any]) -> str:
    function = tool_call.get("function")
    if isinstance(function, Mapping):
        name = function.get("name")
        if isinstance(name, str):
            return name
    name = tool_call.get("name")
    return name if isinstance(name, str) else ""


def _tool_call_input(tool_call: Mapping[str, Any]) -> dict[str, Any]:
    function = tool_call.get("function")
    raw: Any = None
    if isinstance(function, Mapping):
        raw = function.get("arguments")
    elif "input" in tool_call:
        raw = tool_call.get("input")
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _path_is_inside(file_path: str, root: Path) -> bool:
    try:
        candidate = Path(file_path).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate.resolve().relative_to(root.expanduser().resolve())
        return True
    except (OSError, ValueError):
        return False


async def _append_memory_improved_message(
    context: ToolUseContext,
    memory_paths: Sequence[str],
    append_system_message: AppendSystemMessageFn | None,
    *,
    source: str = "direct",
    log_entry_ids: Sequence[str] | None = None,
) -> None:
    paths = [str(path) for path in memory_paths]
    if len(paths) == 1:
        content = f"Memory improved: {paths[0]}"
    else:
        rendered = "\n".join(f"- {path}" for path in paths)
        content = f"Memories improved:\n{rendered}"
    message = {
        "role": "system",
        "content": content,
        "_meta": {
            "type": "memory_improved",
            "memory_paths": paths,
            "source": source,
            "log_entry_ids": list(log_entry_ids or []),
            "timestamp": time.time(),
        },
    }
    if append_system_message is not None:
        result = append_system_message(message)
        if inspect.isawaitable(result):
            await result
    else:
        context.messages.append(message)
    await context.emit_event(
        "memory_saved",
        {
            "memory_paths": paths,
            "message": message,
            "verb": "Improved",
            "source": source,
            "log_entry_ids": list(log_entry_ids or []),
        },
    )


def _uniq(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            out.append(item)
            seen.add(item)
    return out


_default_dreamer: AutoDreamer = AutoDreamer()


def init_auto_dream(dreamer: AutoDreamer | None = None) -> AutoDreamer:
    """Initialize global Auto Dream state, matching OpenSpace ``initAutoDream``."""

    global _default_dreamer
    _default_dreamer = dreamer or AutoDreamer()
    return _default_dreamer


def get_auto_dreamer() -> AutoDreamer:
    return _default_dreamer


async def execute_auto_dream(
    context: ToolUseContext,
    append_system_message: AppendSystemMessageFn | None = None,
) -> DreamResult:
    return await _default_dreamer.execute(context, append_system_message)


async def execute_manual_auto_dream(
    context: ToolUseContext,
    append_system_message: AppendSystemMessageFn | None = None,
    *,
    extra_context: str = "",
    logs_mode: bool = False,
) -> DreamResult:
    return await _default_dreamer.execute_manual(
        context,
        append_system_message,
        extra_context=extra_context,
        logs_mode=logs_mode,
    )


def submit_auto_dream(
    context: ToolUseContext,
    append_system_message: AppendSystemMessageFn | None = None,
) -> asyncio.Task[DreamResult]:
    return _default_dreamer.submit(context, append_system_message)


async def drain_pending_auto_dream(
    timeout_s: float = 60.0,
    *,
    context: Any | None = None,
    scope_key: str | None = None,
) -> int:
    return await _default_dreamer.drain(
        timeout_s,
        context=context,
        scope_key=scope_key,
    )


__all__ = [
    "AutoDreamConfig",
    "AutoDreamer",
    "DreamResult",
    "DreamTaskState",
    "DreamTurn",
    "build_consolidation_prompt",
    "drain_pending_auto_dream",
    "execute_auto_dream",
    "execute_manual_auto_dream",
    "get_auto_dream_config",
    "get_auto_dreamer",
    "get_session_transcript_dir",
    "init_auto_dream",
    "is_auto_dream_enabled",
    "list_sessions_touched_since",
    "read_last_consolidated_at",
    "rollback_consolidation_lock",
    "should_schedule_auto_dream",
    "submit_auto_dream",
    "try_acquire_consolidation_lock",
]
