"""Shared helpers for process sandbox policy generation."""

from __future__ import annotations

import base64
import os
import re
import shlex
import tempfile
from pathlib import Path
from typing import Iterable


SENSITIVE_DEFAULT_DENY_READ_PATHS: tuple[str, ...] = (
    "~/.ssh",
    "~/.aws",
    "~/.kube",
    "~/.gnupg",
    "~/.docker/config.json",
    "~/.npmrc",
    "~/.pypirc",
    "~/.netrc",
)


def generate_command_tag(command: str) -> str:
    """Return a compact command tag safe to embed in sandbox diagnostics."""

    raw = base64.urlsafe_b64encode(command.encode("utf-8")).decode("ascii")
    return raw.rstrip("=")[:48] or "empty"


def normalize_path(path: str, *, base_dir: str | Path | None = None) -> str:
    """Expand ``~`` and resolve relative paths against ``base_dir``."""

    expanded = Path(path).expanduser()
    if expanded.is_absolute():
        return str(expanded)
    root = Path(base_dir or os.getcwd()).expanduser()
    return str((root / expanded).resolve())


def default_task_output_dir() -> str:
    return str(Path(tempfile.gettempdir()) / "openspace-bash-tasks")


def default_allow_write_paths(cwd: str | Path | None = None) -> list[str]:
    root = normalize_path(".", base_dir=cwd)
    return [root, tempfile.gettempdir(), default_task_output_dir()]


def expand_sensitive_paths() -> list[str]:
    return [normalize_path(path) for path in SENSITIVE_DEFAULT_DENY_READ_PATHS]


def has_glob_chars(path: str) -> bool:
    stripped = path.removesuffix("/**")
    return bool(re.search(r"[*?\[\]]", stripped))


def shell_join_command(command: str | Iterable[str]) -> str:
    if isinstance(command, str):
        return command
    return " ".join(shlex.quote(str(part)) for part in command)


def split_xml_safe(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


__all__ = [
    "SENSITIVE_DEFAULT_DENY_READ_PATHS",
    "default_allow_write_paths",
    "default_task_output_dir",
    "expand_sensitive_paths",
    "generate_command_tag",
    "has_glob_chars",
    "normalize_path",
    "shell_join_command",
    "split_xml_safe",
]
