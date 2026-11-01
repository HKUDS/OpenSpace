"""Windows process sandbox status.

OpenSpace keeps Windows sandboxing explicitly unsupported until a helper can
provide file and network isolation, not just process-tree cleanup.
"""

from __future__ import annotations

from .types import SandboxDependencyIssue


def diagnose_windows_dependencies() -> list[SandboxDependencyIssue]:
    return [
        SandboxDependencyIssue(
            "error",
            "unsupported_windows",
            "Sandboxing is not supported on native Windows yet.",
            "Use macOS, Linux, or WSL2 for local process sandboxing.",
        )
    ]


__all__ = ["diagnose_windows_dependencies"]
