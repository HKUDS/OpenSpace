"""Linux/WSL2 bubblewrap command generation for ProcessSandboxManager."""

from __future__ import annotations

from pathlib import Path

from .platform_utils import find_executable, user_namespaces_available
from .types import SandboxDependencyIssue, SandboxPolicy


def diagnose_linux_dependencies(*, needs_network_bridge: bool = False) -> list[SandboxDependencyIssue]:
    issues: list[SandboxDependencyIssue] = []
    if not find_executable("bwrap"):
        issues.append(
            SandboxDependencyIssue(
                "error",
                "missing_bwrap",
                "bubblewrap (bwrap) is required for Linux sandboxing.",
                "Install bubblewrap, for example: apt install bubblewrap.",
            )
        )
    if not user_namespaces_available():
        issues.append(
            SandboxDependencyIssue(
                "error",
                "user_namespace_unavailable",
                "Unprivileged user namespaces are unavailable; bwrap cannot isolate commands.",
                "Enable user namespaces or run on a kernel/container that supports them.",
            )
        )
    if needs_network_bridge and not find_executable("socat"):
        issues.append(
            SandboxDependencyIssue(
                "error",
                "missing_socat",
                "socat is required for bridged proxy sockets in network-restricted Linux sandboxing.",
                "Install socat or disable domain-filtered sandbox networking.",
            )
        )
    elif not find_executable("socat"):
        issues.append(
            SandboxDependencyIssue(
                "warning",
                "missing_socat",
                "socat is unavailable; future domain-filtered network bridging will be disabled.",
            )
        )
    if not find_executable("rg"):
        issues.append(
            SandboxDependencyIssue(
                "warning",
                "missing_rg",
                "ripgrep is unavailable; dynamic sensitive-path scanning is reduced.",
            )
        )
    if not _seccomp_helper_available():
        issues.append(
            SandboxDependencyIssue(
                "warning",
                "missing_seccomp_helper",
                "seccomp helper is unavailable; Unix socket blocking is weaker.",
            )
        )
    return issues


def build_linux_bwrap_argv(
    command: str,
    *,
    cwd: str,
    shell: str,
    policy: SandboxPolicy,
) -> tuple[list[str], list[str]]:
    diagnostics: list[str] = []
    bwrap = find_executable("bwrap") or "bwrap"
    argv = [
        bwrap,
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-cgroup",
        "--die-with-parent",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
    ]

    if not policy.allow_network:
        argv.append("--unshare-net")
    elif policy.allowed_domains or policy.denied_domains:
        diagnostics.append(
            "Linux domain-filtered network sandboxing requires the later proxy bridge; "
            "this wrapper leaves network unshared for now."
        )

    for path in _existing_paths([cwd, *policy.allow_write]):
        argv.extend(["--bind", path, path])

    for path in _existing_paths(policy.allow_read):
        argv.extend(["--ro-bind", path, path])

    for path in _existing_paths([*policy.deny_read, *policy.deny_write]):
        candidate = Path(path)
        if candidate.is_dir():
            argv.extend(["--tmpfs", path])
        elif candidate.exists():
            argv.extend(["--ro-bind", "/dev/null", path])
        else:
            diagnostics.append(f"Skipped sandbox deny path that does not exist: {path}")

    argv.extend(["--", shell, "-c", command])
    return argv, diagnostics


def _existing_paths(paths: list[str]) -> list[str]:
    out: list[str] = []
    for path in paths:
        if path in out:
            continue
        if Path(path).exists():
            out.append(path)
    return out


def _seccomp_helper_available() -> bool:
    return bool(find_executable("apply-seccomp"))


__all__ = ["build_linux_bwrap_argv", "diagnose_linux_dependencies"]
