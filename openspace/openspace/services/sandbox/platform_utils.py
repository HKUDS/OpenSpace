"""Platform detection for the local process sandbox runtime."""

from __future__ import annotations

import os
import platform as _platform
import shutil
from pathlib import Path

from .types import Platform


def get_platform() -> Platform:
    system = _platform.system().lower()
    if system == "darwin":
        return "macos"
    if system == "linux":
        return _linux_or_wsl_platform()
    if system == "windows":
        return "windows"
    return "unsupported"


def is_supported_platform(platform: Platform | str | None = None) -> bool:
    current = platform or get_platform()
    return current in {"macos", "linux", "wsl2"}


def find_executable(name: str) -> str | None:
    return shutil.which(name)


def is_wsl() -> bool:
    return get_platform() in {"wsl1", "wsl2"}


def user_namespaces_available() -> bool:
    """Best-effort Linux user namespace availability check.

    This mirrors OpenSpace's dependency check contract: an error means bwrap cannot
    provide the Linux sandbox.  The check is intentionally read-only and avoids
    trying to spawn privileged helpers.
    """

    if get_platform() not in {"linux", "wsl2"}:
        return False
    clone_path = Path("/proc/sys/kernel/unprivileged_userns_clone")
    if clone_path.exists():
        try:
            return clone_path.read_text(encoding="utf-8").strip() != "0"
        except OSError:
            return False
    return Path("/proc/self/ns/user").exists()


def _linux_or_wsl_platform() -> Platform:
    version = ""
    try:
        version = Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        pass
    if "microsoft" not in version and "wsl" not in version:
        return "linux"
    if os.environ.get("WSL_INTEROP"):
        return "wsl2"
    return "wsl1"


__all__ = [
    "find_executable",
    "get_platform",
    "is_supported_platform",
    "is_wsl",
    "user_namespaces_available",
]
