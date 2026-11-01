"""macOS Seatbelt profile generation for ProcessSandboxManager."""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

from .platform_utils import find_executable
from .types import SandboxDependencyIssue, SandboxPolicy


def diagnose_macos_dependencies() -> list[SandboxDependencyIssue]:
    issues: list[SandboxDependencyIssue] = []
    sandbox_exec = find_executable("sandbox-exec")
    if not sandbox_exec:
        issues.append(
            SandboxDependencyIssue(
                "error",
                "missing_sandbox_exec",
                "sandbox-exec is required for macOS sandboxing.",
                "Install or enable Apple's sandbox-exec tool.",
            )
        )
    else:
        sandbox_exec_error = _sandbox_exec_smoke_error(sandbox_exec)
        if sandbox_exec_error:
            issues.append(sandbox_exec_error)
    if not find_executable("log"):
        issues.append(
            SandboxDependencyIssue(
                "warning",
                "missing_log_stream",
                "macOS log stream is unavailable; structured sandbox violations may be incomplete.",
            )
        )
    if not find_executable("rg"):
        issues.append(
            SandboxDependencyIssue(
                "warning",
                "missing_rg",
                "ripgrep is unavailable; dynamic sensitive-path scanning is reduced.",
                "Install ripgrep or configure sandbox.ripgrep.",
            )
        )
    return issues


@lru_cache(maxsize=4)
def _sandbox_exec_smoke_error(sandbox_exec: str) -> SandboxDependencyIssue | None:
    profile = "(version 1)\n(allow default)\n"
    try:
        result = subprocess.run(
            [sandbox_exec, "-p", profile, "/usr/bin/true"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except OSError as exc:
        return SandboxDependencyIssue(
            "error",
            "sandbox_exec_unusable",
            f"sandbox-exec is present but could not be started: {exc}",
            "Run outside a parent sandbox or disable OpenSpace process sandbox.",
        )
    except subprocess.TimeoutExpired:
        return SandboxDependencyIssue(
            "error",
            "sandbox_exec_timeout",
            "sandbox-exec is present but did not complete a smoke test.",
            "Run outside a parent sandbox or disable OpenSpace process sandbox.",
        )
    if result.returncode == 0:
        return None
    stderr = (result.stderr or result.stdout or "").strip()
    suffix = f": {stderr}" if stderr else f" (exit {result.returncode})"
    return SandboxDependencyIssue(
        "error",
        "sandbox_exec_unusable",
        f"sandbox-exec is present but cannot apply sandbox profiles{suffix}",
        "Run outside a parent sandbox or disable OpenSpace process sandbox.",
    )


def build_macos_profile(policy: SandboxPolicy, *, command_tag: str) -> str:
    lines: list[str] = [
        "(version 1)",
        "(deny default)",
        "(allow process*)",
        "(allow signal (target self))",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
        "(allow file-read*)",
    ]
    for path in policy.deny_read:
        lines.append(
            f'(deny file-read* (subpath "{_sbpl_escape(path)}") '
            f'(with message "openspace:{command_tag}:read-deny:{_sbpl_escape(path)}"))'
        )
    for path in policy.allow_read:
        lines.append(f'(allow file-read* (subpath "{_sbpl_escape(path)}"))')
    for path in policy.allow_write:
        lines.append(f'(allow file-write* (subpath "{_sbpl_escape(path)}"))')
    for path in policy.deny_write:
        lines.append(
            f'(deny file-write* (subpath "{_sbpl_escape(path)}") '
            f'(with message "openspace:{command_tag}:write-deny:{_sbpl_escape(path)}"))'
        )
    if policy.allow_network:
        lines.append("(allow network*)")
    if policy.allow_all_unix_sockets:
        lines.append("(allow system-socket)")
    for socket_path in policy.allow_unix_sockets:
        lines.append(f'(allow network* (literal "{_sbpl_escape(socket_path)}"))')
    return "\n".join(lines) + "\n"


def build_macos_argv(
    command: str,
    *,
    shell: str,
    profile: str,
) -> list[str]:
    sandbox_exec = find_executable("sandbox-exec") or "sandbox-exec"
    return [sandbox_exec, "-p", profile, shell, "-c", command]


def parse_macos_violation_message(
    message: str,
    *,
    command: str,
    command_tag: str,
) -> dict[str, str] | None:
    if f"openspace:{command_tag}:" not in message and "deny" not in message:
        return None
    operation = "unknown"
    if "read-deny" in message or "file-read" in message:
        operation = "read"
    elif "write-deny" in message or "file-write" in message:
        operation = "write"
    path = None
    marker = f"openspace:{command_tag}:{operation}-deny:"
    if marker in message:
        path = message.split(marker, 1)[1].split()[0].strip('"')
    return {
        "command": command,
        "command_tag": command_tag,
        "operation": operation,
        "path": path or "",
        "raw_message": message,
    }


def _sbpl_escape(value: str) -> str:
    # SBPL strings are not XML, but reuse the same no-control principle and
    # explicitly escape backslash/quote to avoid policy injection.
    return str(Path(value).expanduser()).replace("\\", "\\\\").replace('"', '\\"')


__all__ = [
    "build_macos_argv",
    "build_macos_profile",
    "diagnose_macos_dependencies",
    "parse_macos_violation_message",
]
