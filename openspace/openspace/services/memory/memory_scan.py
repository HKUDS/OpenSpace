"""Memory-directory scanning primitives.

Implementation notes: ``memdir/memoryScan.ts`` (95 lines).  This module scans topic
memory files, parses their frontmatter header, and formats a manifest for
recall/extraction follow-up steps.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .memdir import ENTRYPOINT_NAME
from .memory_types import MemoryType, parse_memory_type

MAX_MEMORY_FILES = 200
FRONTMATTER_MAX_LINES = 30


@dataclass(frozen=True)
class MemoryHeader:
    filename: str
    file_path: Path
    mtime_ms: float
    description: Optional[str]
    memory_type: Optional[MemoryType]

    @property
    def filePath(self) -> str:
        """legacy-compatible camelCase alias."""

        return str(self.file_path)

    @property
    def mtimeMs(self) -> float:
        """legacy-compatible camelCase alias."""

        return self.mtime_ms

    @property
    def type(self) -> Optional[MemoryType]:
        """legacy-compatible field name."""

        return self.memory_type


def scan_memory_files(memory_dir: str | Path) -> list[MemoryHeader]:
    """Scan ``memory_dir`` for topic ``.md`` files, newest first.

    Any directory-level error returns ``[]`` like OpenSpace.  Per-file errors are
    ignored via the same all-settled behavior as ``Promise.allSettled``.
    """

    root = Path(memory_dir).expanduser()
    try:
        entries = list(root.rglob("*.md"))
    except OSError:
        return []

    headers: list[MemoryHeader] = []
    for entry in entries:
        try:
            if (
                entry.name == ENTRYPOINT_NAME
                or not entry.is_file()
                or _is_daily_log_path(root, entry)
            ):
                continue
            header = _read_memory_header(root, entry)
        except OSError:
            continue
        if header is not None:
            headers.append(header)

    headers.sort(key=lambda item: item.mtime_ms, reverse=True)
    return headers[:MAX_MEMORY_FILES]


def _is_daily_log_path(root: Path, path: Path) -> bool:
    """Return True for ``logs/YYYY/MM/*.md`` raw daily-log files."""

    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return False
    return len(parts) >= 4 and parts[0] == "logs"


def format_memory_manifest(memories: list[MemoryHeader]) -> str:
    """Format memory headers as OpenSpace's one-line text manifest."""

    lines: list[str] = []
    for memory in memories:
        tag = f"[{memory.memory_type}] " if memory.memory_type else ""
        timestamp = datetime.fromtimestamp(
            memory.mtime_ms / 1000,
            tz=timezone.utc,
        ).isoformat().replace("+00:00", "Z")
        if memory.description:
            lines.append(f"- {tag}{memory.filename} ({timestamp}): {memory.description}")
        else:
            lines.append(f"- {tag}{memory.filename} ({timestamp})")
    return "\n".join(lines)


def _read_memory_header(root: Path, path: Path) -> Optional[MemoryHeader]:
    stat = path.stat()
    try:
        content = _read_first_lines(path, FRONTMATTER_MAX_LINES)
    except UnicodeDecodeError:
        return None
    frontmatter = _parse_frontmatter(content)
    try:
        filename = path.relative_to(root).as_posix()
    except ValueError:
        filename = path.name
    return MemoryHeader(
        filename=filename,
        file_path=path.resolve(),
        mtime_ms=stat.st_mtime * 1000,
        description=_coerce_description(frontmatter.get("description")),
        memory_type=parse_memory_type(frontmatter.get("type")),
    )


def _read_first_lines(path: Path, limit: int) -> str:
    lines: list[str] = []
    with path.open("r", encoding="utf-8") as handle:
        for index, line in enumerate(handle):
            if index >= limit:
                break
            lines.append(line)
    return "".join(lines)


def _parse_frontmatter(content: str) -> dict[str, object]:
    normalized = content.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        return {}
    end = normalized.find("\n---", 4)
    if end == -1:
        return {}
    raw = normalized[4:end]
    parsed: dict[str, object] = {}
    current_key: Optional[str] = None
    list_values: list[str] = []

    def flush_list() -> None:
        nonlocal current_key, list_values
        if current_key is not None and list_values:
            parsed[current_key] = list(list_values)
        current_key = None
        list_values = []

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if current_key is not None and stripped.startswith("-"):
            list_values.append(_strip_yaml_scalar(stripped[1:].strip()))
            continue
        flush_list()
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not value:
            current_key = key
            list_values = []
            continue
        parsed[key] = _strip_yaml_scalar(value)

    flush_list()
    return parsed


def _strip_yaml_scalar(value: str) -> str:
    value = value.strip()
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    return value


def _coerce_description(value: object) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        text = str(value).strip()
        return text or None
    return None
