"""Daily-log memory mode.

Implementation notes:
- ``memdir/paths.ts::getAutoMemDailyLogPath``
- ``memdir/memdir.ts::buildAssistantDailyLogPrompt``
- ``services/autoDream/consolidationPrompt.ts``
- ``services/autoDream/autoDream.ts::isGateOpen``

OpenSpace only provides the KAIROS prompt/path semantics: append to
``logs/YYYY/MM/YYYY-MM-DD.md`` and let a dream pass distill logs into topic
memories later.  It does not provide a structured writer, unconsolidated scan,
or consolidated marker.  OpenSpace adds those runtime primitives here so the
daily-log mode is deterministic and auditable.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Sequence

from openspace.grounding.core.permissions.types import (
    DecisionReasonOther,
    PermissionDeny,
)
from openspace.grounding.core.tool.base import BaseTool
from openspace.grounding.core.types import BackendType, ToolResult, ToolStatus
from openspace.services.memory.memdir import (
    ENTRYPOINT_NAME,
    ensure_memory_dir_exists,
    get_auto_mem_path,
)

MemoryMode = Literal["direct", "daily_log"]

OPENSPACE_MEMORY_MODE_ENV = "OPENSPACE_MEMORY_MODE"
MEMORY_LOG_TOOL_NAME = "memory_log"
LOGS_DIRNAME = "logs"
VALID_MEMORY_MODES: set[str] = {"direct", "daily_log"}


@dataclass(slots=True)
class DailyLogEntry:
    entry_id: str
    time: str
    session_id: str
    source: str
    type: str
    confidence: str
    text: str
    evidence: str = ""
    consolidated: bool = False
    stability: str = "candidate"
    proposed_target: str | None = None
    related_files: list[str] = field(default_factory=list)
    consolidated_at: str | None = None
    consolidated_to: str | None = None
    log_path: Path | None = None


@dataclass(slots=True)
class DailyLogAppendResult:
    entry_ids: list[str] = field(default_factory=list)
    log_paths: list[Path] = field(default_factory=list)


@dataclass(slots=True)
class DailyLogScanResult:
    entries: list[DailyLogEntry] = field(default_factory=list)
    log_paths: list[Path] = field(default_factory=list)

    @property
    def entry_ids(self) -> list[str]:
        return [entry.entry_id for entry in self.entries]

    @property
    def session_ids(self) -> list[str]:
        return _uniq(entry.session_id for entry in self.entries if entry.session_id)


def get_memory_mode(value: str | None = None) -> MemoryMode:
    """Resolve OpenSpace memory mode.

    ``direct`` is the conservative default.  Settings ``memory.mode`` is the
    durable gate; ``OPENSPACE_MEMORY_MODE`` remains the highest-priority
    runtime override.
    ``OPENSPACE_KAIROS_ACTIVE`` is intentionally not read here.
    """

    if value is None:
        value = os.environ.get(OPENSPACE_MEMORY_MODE_ENV)
    if value is None:
        try:
            from openspace.services.runtime_support.settings import get_setting

            value = get_setting("memory.mode", None)
        except Exception:
            value = None
    raw = (value or "direct").strip().lower()
    if raw in {"daily-log", "dailylog", "logs"}:
        raw = "daily_log"
    if raw not in VALID_MEMORY_MODES:
        return "direct"
    return raw  # type: ignore[return-value]


def is_daily_log_mode(value: str | None = None) -> bool:
    return get_memory_mode(value) == "daily_log"


def get_daily_log_path(memory_dir: str | Path, day: date | datetime | None = None) -> Path:
    current = day or date.today()
    if isinstance(current, datetime):
        current = current.date()
    yyyy = f"{current.year:04d}"
    mm = f"{current.month:02d}"
    dd = f"{current.day:02d}"
    return Path(memory_dir).expanduser().resolve() / LOGS_DIRNAME / yyyy / mm / f"{yyyy}-{mm}-{dd}.md"


async def append_daily_log_entries(
    context: Any,
    entries: Sequence[Mapping[str, Any] | DailyLogEntry],
) -> DailyLogAppendResult:
    """Append candidate memory entries to daily log files.

    The file is rewritten via ``os.replace`` so frontmatter can be updated
    atomically while preserving append-only entry semantics.
    """

    memory_dir = get_auto_mem_path(cwd=getattr(context, "cwd", None))
    ensure_memory_dir_exists(memory_dir)
    normalized = _normalize_entries(entries, context)
    result = append_daily_log_entries_to_dir(memory_dir, normalized)
    if result.entry_ids:
        sink = getattr(context, "emit_event", None)
        if callable(sink):
            event_result = sink(
                "memory_logged",
                {
                    "memory_dir": str(memory_dir),
                    "log_paths": [str(path) for path in result.log_paths],
                    "entry_ids": list(result.entry_ids),
                    "entry_count": len(result.entry_ids),
                    "source": "daily_log",
                },
            )
            if inspect.isawaitable(event_result):
                await event_result
    return result


def append_daily_log_entries_to_dir(
    memory_dir: str | Path,
    entries: Sequence[DailyLogEntry],
) -> DailyLogAppendResult:
    root = Path(memory_dir).expanduser().resolve()
    by_path: dict[Path, list[DailyLogEntry]] = {}
    for entry in entries:
        path = get_daily_log_path(root, _date_from_iso(entry.time))
        by_path.setdefault(path, []).append(entry)

    appended_ids: list[str] = []
    touched_paths: list[Path] = []
    for path, path_entries in by_path.items():
        existing = _read_log_file(path)
        existing_ids = {entry.entry_id for entry in existing}
        additions: list[DailyLogEntry] = []
        for entry in path_entries:
            candidate = entry
            if candidate.entry_id in existing_ids:
                candidate = DailyLogEntry(
                    **{
                        **_entry_to_dict(candidate),
                        "entry_id": _dedupe_entry_id(candidate.entry_id, existing_ids),
                    }
                )
            existing_ids.add(candidate.entry_id)
            candidate.log_path = path
            additions.append(candidate)
        if not additions:
            continue
        updated = [*existing, *additions]
        _write_log_file(path, updated, day=_date_from_log_path(path))
        appended_ids.extend(entry.entry_id for entry in additions)
        touched_paths.append(path)
    return DailyLogAppendResult(entry_ids=appended_ids, log_paths=touched_paths)


def scan_unconsolidated_logs(
    memory_dir: str | Path,
    since: datetime | float | int | None = None,
) -> DailyLogScanResult:
    root = Path(memory_dir).expanduser().resolve()
    logs_root = root / LOGS_DIRNAME
    if not logs_root.exists():
        return DailyLogScanResult()

    since_ts = _since_to_timestamp(since)
    entries: list[DailyLogEntry] = []
    paths: list[Path] = []
    for path in sorted(logs_root.glob("*/*/*.md")):
        if since_ts is not None:
            try:
                if path.stat().st_mtime < since_ts:
                    continue
            except OSError:
                continue
        file_entries = [
            entry for entry in _read_log_file(path) if not entry.consolidated
        ]
        if not file_entries:
            continue
        paths.append(path)
        entries.extend(file_entries)
    return DailyLogScanResult(entries=entries, log_paths=paths)


def mark_log_entries_consolidated(
    memory_dir: str | Path,
    entry_ids: Sequence[str],
    consolidated_at: str | datetime | None = None,
    consolidated_to: Sequence[str] | str | None = None,
) -> int:
    ids = {str(entry_id) for entry_id in entry_ids if str(entry_id)}
    if not ids:
        return 0

    root = Path(memory_dir).expanduser().resolve()
    logs_root = root / LOGS_DIRNAME
    if not logs_root.exists():
        return 0

    at = _coerce_iso(consolidated_at) if consolidated_at is not None else _utc_now_iso()
    if isinstance(consolidated_to, str):
        consolidated_to_text = consolidated_to
    elif consolidated_to:
        consolidated_to_text = ", ".join(str(item) for item in consolidated_to)
    else:
        consolidated_to_text = "dropped"

    count = 0
    for path in sorted(logs_root.glob("*/*/*.md")):
        entries = _read_log_file(path)
        changed = False
        for entry in entries:
            if entry.entry_id not in ids:
                continue
            if not entry.consolidated:
                count += 1
            entry.consolidated = True
            entry.consolidated_at = at
            entry.consolidated_to = consolidated_to_text
            changed = True
        if changed:
            _write_log_file(path, entries, day=_date_from_log_path(path))
    return count


def build_daily_log_consolidation_prompt(
    memory_dir: str | Path,
    log_paths: Sequence[str | Path],
    manifest: str,
    entries: Sequence[DailyLogEntry] | None = None,
) -> str:
    """Build OpenSpace's deterministic layer around OpenSpace's logs prompt."""

    lines = [
        "## Daily-log consolidation mode",
        "",
        "OpenSpace has already scanned the daily-log raw material. Treat these entries as candidate memories, not final memory files.",
        "",
        "Unconsolidated log files:",
    ]
    if log_paths:
        lines.extend(f"- `{Path(path)}`" for path in log_paths)
    else:
        lines.append("- none")

    if entries:
        lines.extend(["", "Unconsolidated entry ids:"])
        lines.extend(f"- `{entry.entry_id}` ({entry.type}, {entry.confidence}) {entry.text}" for entry in entries)

    lines.extend(
        [
            "",
            "Existing topic memory manifest:",
            manifest.strip() or "(no topic memories yet)",
            "",
            "After this dream succeeds, OpenSpace will mark the listed log entries as consolidated. Do not edit files under `logs/` directly; update top-level topic memory files and `MEMORY.md` instead.",
        ]
    )
    return "\n".join(lines)


def format_daily_log_entries(
    memory_dir: str | Path,
    *,
    include_consolidated: bool = False,
    limit: int = 50,
) -> str:
    root = Path(memory_dir).expanduser().resolve()
    logs_root = root / LOGS_DIRNAME
    if not logs_root.exists():
        return "No daily memory logs found."

    entries: list[DailyLogEntry] = []
    for path in sorted(logs_root.glob("*/*/*.md"), reverse=True):
        for entry in _read_log_file(path):
            if entry.consolidated and not include_consolidated:
                continue
            entries.append(entry)
    entries.sort(key=lambda entry: entry.time, reverse=True)
    if not entries:
        return "No unconsolidated daily memory log entries found."

    lines = ["Daily memory logs:"]
    for entry in entries[: max(1, limit)]:
        status = "consolidated" if entry.consolidated else "pending"
        target = f" -> {entry.consolidated_to}" if entry.consolidated_to else ""
        location = f" ({entry.log_path})" if entry.log_path else ""
        lines.append(
            f"- [{status}] {entry.time} {entry.session_id} {entry.type}/{entry.confidence}: {entry.text}{target}{location}"
        )
    if len(entries) > limit:
        lines.append(f"... {len(entries) - limit} more entr{'y' if len(entries) - limit == 1 else 'ies'} omitted")
    return "\n".join(lines)


def extract_logged_entries(
    messages: Sequence[Mapping[str, Any]],
) -> tuple[list[str], list[str]]:
    entry_ids: list[str] = []
    log_paths: list[str] = []
    for message in messages:
        meta = message.get("_meta")
        if not isinstance(meta, Mapping):
            continue
        result_meta = meta.get("tool_result_metadata")
        if not isinstance(result_meta, Mapping):
            continue
        if result_meta.get("type") != "memory_log":
            continue
        raw_ids = result_meta.get("entry_ids")
        if isinstance(raw_ids, list):
            entry_ids.extend(str(item) for item in raw_ids if str(item))
        raw_paths = result_meta.get("log_paths")
        if isinstance(raw_paths, list):
            log_paths.extend(str(item) for item in raw_paths if str(item))
        raw_path = result_meta.get("log_path")
        if isinstance(raw_path, str):
            log_paths.append(raw_path)
    return _uniq(entry_ids), _uniq(log_paths)


class MemoryLogTool(BaseTool):
    """Append structured candidate memories to the daily log."""

    _name = MEMORY_LOG_TOOL_NAME
    _description = (
        "Append candidate memories to the daily log for later dream consolidation."
    )
    backend_type = BackendType.SHELL
    _is_read_only = False
    _is_concurrency_safe = False
    search_hint = "log candidate persistent memories"
    parameter_descriptions = {
        "entries": (
            "List of candidate memory entries. Each entry should include text, type "
            "(user/feedback/project/reference), confidence (low/medium/high), and evidence."
        ),
    }

    def __init__(self) -> None:
        self._current_context: Any | None = None
        super().__init__()

    def set_context(self, context: Any) -> None:
        self._current_context = context

    async def validate_input(self, input: dict[str, Any], context: Any = None) -> str | None:
        if get_memory_mode(getattr(context, "memory_mode", None)) != "daily_log":
            return "memory_log is only available when memory_mode is daily_log."
        entries = input.get("entries")
        if not isinstance(entries, list) or not entries:
            return "entries must be a non-empty list."
        for index, entry in enumerate(entries, start=1):
            if not isinstance(entry, Mapping):
                return f"entries[{index}] must be an object."
            if not str(entry.get("text") or "").strip():
                return f"entries[{index}].text is required."
        return None

    async def check_permissions(self, input: dict[str, Any], context: Any = None):
        from openspace.grounding.core.permissions import (
            PermissionAllow,
            check_write_permission_for_tool,
            deny_missing_permission_context,
        )

        validation_error = await self.validate_input(input, context)
        if validation_error is not None:
            return PermissionDeny(
                message=validation_error,
                decision_reason=DecisionReasonOther(reason=validation_error),
            )

        perm_ctx = getattr(context, "permission_context", None)
        if perm_ctx is None:
            return deny_missing_permission_context(self._name)

        memory_dir = get_auto_mem_path(cwd=getattr(context, "cwd", None))
        normalized = _normalize_entries(input["entries"], context)
        log_paths = _uniq(
            str(get_daily_log_path(memory_dir, _date_from_iso(entry.time)))
            for entry in normalized
        )

        allowed = PermissionAllow(updated_input=None)
        for log_path in log_paths:
            decision = check_write_permission_for_tool(
                tool_name=self._name,
                input_path=log_path,
                context=perm_ctx,
            )
            if not isinstance(decision, PermissionAllow):
                return decision
            allowed = decision
        return allowed

    async def _arun(self, entries: list[dict[str, Any]]) -> ToolResult:
        context = self._current_context
        validation_error = await self.validate_input({"entries": entries}, context)
        if validation_error:
            return ToolResult(
                status=ToolStatus.ERROR,
                content=validation_error,
                error=validation_error,
            )
        result = await append_daily_log_entries(context, entries)
        if not result.entry_ids:
            return ToolResult(
                status=ToolStatus.SUCCESS,
                content="No daily-log entries were appended.",
                metadata={"type": "memory_log", "entry_ids": [], "log_paths": []},
            )
        rendered_paths = ", ".join(str(path) for path in result.log_paths)
        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=f"Logged {len(result.entry_ids)} candidate memor{'y' if len(result.entry_ids) == 1 else 'ies'} to {rendered_paths}.",
            metadata={
                "type": "memory_log",
                "entry_ids": list(result.entry_ids),
                "log_paths": [str(path) for path in result.log_paths],
                "memory_dir": str(get_auto_mem_path(cwd=getattr(context, "cwd", None))),
            },
        )


def build_extract_daily_log_prompt(
    new_message_count: int,
    existing_memories: str,
) -> str:
    manifest = (
        "\n\n## Existing topic memory files\n\n"
        + existing_memories
        + "\n\nUse this list to avoid logging duplicates unless the new signal changes or confirms an existing memory."
        if existing_memories
        else ""
    )
    return "\n".join(
        [
            (
                "You are now acting as the daily-log memory extraction subagent. "
                f"Analyze the most recent ~{new_message_count} messages above and append candidate memories to the daily log."
            ),
            "",
            f"Use the `{MEMORY_LOG_TOOL_NAME}` tool for every memory candidate. Do not write topic memory files or `{ENTRYPOINT_NAME}` in this mode; a later dream pass will distill daily logs into durable topic memory.",
            "",
            "Each entry should include:",
            "- text: the candidate memory in one concise sentence",
            "- type: one of user, feedback, project, reference",
            "- confidence: low, medium, or high",
            "- evidence: short reason from the recent messages",
            "- proposed_target: optional topic filename if obvious",
            "",
            "Only log information useful in future conversations. Do not log secrets, code facts that can be read from the repository, or one-off task progress.",
            manifest,
        ]
    )


def _normalize_entries(
    entries: Sequence[Mapping[str, Any] | DailyLogEntry],
    context: Any,
) -> list[DailyLogEntry]:
    normalized: list[DailyLogEntry] = []
    existing_ids: set[str] = set()
    for index, raw in enumerate(entries, start=1):
        if isinstance(raw, DailyLogEntry):
            entry = raw
        else:
            entry = _entry_from_mapping(raw, context, index)
        if entry.entry_id in existing_ids:
            entry = DailyLogEntry(
                **{**_entry_to_dict(entry), "entry_id": _dedupe_entry_id(entry.entry_id, existing_ids)}
            )
        existing_ids.add(entry.entry_id)
        normalized.append(entry)
    return normalized


def _entry_from_mapping(raw: Mapping[str, Any], context: Any, index: int) -> DailyLogEntry:
    now = _coerce_iso(raw.get("time") or raw.get("timestamp") or None)
    session_id = str(raw.get("session_id") or getattr(context, "session_id", None) or "unknown")
    text = str(raw.get("text") or "").strip()
    evidence = str(raw.get("evidence") or "").strip()
    memory_type = _clean_choice(raw.get("type") or raw.get("memory_type"), {"user", "feedback", "project", "reference"}, "project")
    confidence = _clean_choice(raw.get("confidence"), {"low", "medium", "high"}, "medium")
    stability = _clean_choice(raw.get("stability"), {"candidate", "confirmed", "superseded"}, "candidate")
    related = raw.get("related_files")
    related_files = [str(item) for item in related] if isinstance(related, list) else []
    proposed_target = raw.get("proposed_target")
    entry_id = str(raw.get("entry_id") or "").strip()
    if not entry_id:
        entry_id = _make_entry_id(now, session_id, text, evidence, index)
    return DailyLogEntry(
        entry_id=entry_id,
        time=now,
        session_id=session_id,
        source=str(raw.get("source") or "extract_memories"),
        type=memory_type,
        confidence=confidence,
        text=text,
        evidence=evidence,
        consolidated=bool(raw.get("consolidated", False)),
        stability=stability,
        proposed_target=str(proposed_target).strip() if proposed_target else None,
        related_files=related_files,
        consolidated_at=_coerce_iso(raw.get("consolidated_at")) if raw.get("consolidated_at") else None,
        consolidated_to=str(raw.get("consolidated_to")).strip() if raw.get("consolidated_to") else None,
    )


def _entry_to_dict(entry: DailyLogEntry) -> dict[str, Any]:
    return {
        "entry_id": entry.entry_id,
        "time": entry.time,
        "session_id": entry.session_id,
        "source": entry.source,
        "type": entry.type,
        "confidence": entry.confidence,
        "text": entry.text,
        "evidence": entry.evidence,
        "consolidated": entry.consolidated,
        "stability": entry.stability,
        "proposed_target": entry.proposed_target,
        "related_files": list(entry.related_files),
        "consolidated_at": entry.consolidated_at,
        "consolidated_to": entry.consolidated_to,
    }


def _write_log_file(path: Path, entries: Sequence[DailyLogEntry], *, day: date) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = _render_log_file(day, entries)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(path.parent),
        delete=False,
    ) as handle:
        handle.write(content)
        tmp_name = handle.name
    os.replace(tmp_name, path)


def _render_log_file(day: date, entries: Sequence[DailyLogEntry]) -> str:
    session_ids = _uniq(entry.session_id for entry in entries if entry.session_id)
    last_consolidated = _latest(
        entry.consolidated_at for entry in entries if entry.consolidated_at
    )
    status = "consolidated" if entries and all(entry.consolidated for entry in entries) else "active"
    lines = [
        "---",
        f"date: {day.isoformat()}",
        f"status: {status}",
        f"last_consolidated_at: {last_consolidated or ''}",
        "session_ids:",
        *[f"  - {_json_scalar(session_id)}" for session_id in session_ids],
        "---",
        "",
    ]
    for entry in entries:
        heading_time = _heading_time(entry.time)
        lines.extend(
            [
                f"## {heading_time} - {entry.session_id}",
                "",
                f"- entry_id: {_json_scalar(entry.entry_id)}",
            ]
        )
        for key, value in _entry_to_dict(entry).items():
            if key == "entry_id":
                continue
            lines.append(f"  {key}: {_render_value(value)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _read_log_file(path: Path) -> list[DailyLogEntry]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    body = raw
    if raw.startswith("---\n"):
        parts = raw.split("---\n", 2)
        if len(parts) == 3:
            body = parts[2]

    entries: list[DailyLogEntry] = []
    current: dict[str, Any] | None = None
    for line in body.splitlines():
        if line.startswith("- entry_id:"):
            if current is not None:
                entry = _entry_from_parsed(current, path)
                if entry is not None:
                    entries.append(entry)
            current = {"entry_id": _parse_value(line.split(":", 1)[1].strip())}
            continue
        if current is None or not line.startswith("  ") or ":" not in line:
            continue
        key, value = line.strip().split(":", 1)
        current[key] = _parse_value(value.strip())
    if current is not None:
        entry = _entry_from_parsed(current, path)
        if entry is not None:
            entries.append(entry)
    return entries


def _entry_from_parsed(raw: Mapping[str, Any], path: Path) -> DailyLogEntry | None:
    text = str(raw.get("text") or "").strip()
    entry_id = str(raw.get("entry_id") or "").strip()
    if not entry_id or not text:
        return None
    related = raw.get("related_files")
    return DailyLogEntry(
        entry_id=entry_id,
        time=str(raw.get("time") or _date_from_log_path(path).isoformat()),
        session_id=str(raw.get("session_id") or "unknown"),
        source=str(raw.get("source") or "extract_memories"),
        type=str(raw.get("type") or "project"),
        confidence=str(raw.get("confidence") or "medium"),
        text=text,
        evidence=str(raw.get("evidence") or ""),
        consolidated=bool(raw.get("consolidated", False)),
        stability=str(raw.get("stability") or "candidate"),
        proposed_target=str(raw.get("proposed_target")) if raw.get("proposed_target") else None,
        related_files=[str(item) for item in related] if isinstance(related, list) else [],
        consolidated_at=str(raw.get("consolidated_at")) if raw.get("consolidated_at") else None,
        consolidated_to=str(raw.get("consolidated_to")) if raw.get("consolidated_to") else None,
        log_path=path,
    )


def _parse_value(value: str) -> Any:
    if value == "":
        return None
    if value == "true":
        return True
    if value == "false":
        return False
    if value == "[]":
        return []
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _render_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    return _json_scalar(str(value))


def _json_scalar(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _clean_choice(value: Any, allowed: set[str], default: str) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in allowed else default


def _make_entry_id(time_iso: str, session_id: str, text: str, evidence: str, index: int) -> str:
    day = _date_from_iso(time_iso).isoformat()
    digest = hashlib.sha1(
        f"{time_iso}\0{session_id}\0{text}\0{evidence}\0{index}".encode("utf-8")
    ).hexdigest()[:10]
    return f"{day}-{_safe_id_part(session_id)}-{index:02d}-{digest}"


def _dedupe_entry_id(entry_id: str, existing: set[str]) -> str:
    base = entry_id
    index = 2
    while entry_id in existing:
        entry_id = f"{base}-{index}"
        index += 1
    return entry_id


def _safe_id_part(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value)
    return cleaned.strip("-_")[:48] or "session"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _coerce_iso(value: Any) -> str:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text
    else:
        return _utc_now_iso()
    if dt.tzinfo is None:
        return dt.replace(microsecond=0).isoformat()
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _date_from_iso(value: str) -> date:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return date.today()


def _date_from_log_path(path: Path) -> date:
    try:
        return date.fromisoformat(path.stem)
    except ValueError:
        return date.today()


def _heading_time(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%H:%M")
    except ValueError:
        return value[:5] if len(value) >= 5 else value


def _since_to_timestamp(since: datetime | float | int | None) -> float | None:
    if since is None:
        return None
    if isinstance(since, datetime):
        return since.timestamp()
    return float(since)


def _latest(values: Iterable[str]) -> str | None:
    ordered = sorted(value for value in values if value)
    return ordered[-1] if ordered else None


def _uniq(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            out.append(item)
            seen.add(item)
    return out


__all__ = [
    "LOGS_DIRNAME",
    "MEMORY_LOG_TOOL_NAME",
    "OPENSPACE_MEMORY_MODE_ENV",
    "DailyLogAppendResult",
    "DailyLogEntry",
    "DailyLogScanResult",
    "MemoryLogTool",
    "MemoryMode",
    "append_daily_log_entries",
    "append_daily_log_entries_to_dir",
    "build_daily_log_consolidation_prompt",
    "build_extract_daily_log_prompt",
    "extract_logged_entries",
    "format_daily_log_entries",
    "get_daily_log_path",
    "get_memory_mode",
    "is_daily_log_mode",
    "mark_log_entries_consolidated",
    "scan_unconsolidated_logs",
]
