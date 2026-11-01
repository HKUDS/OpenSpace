"""Process sandbox manager.

Owns local OS process sandbox state, dependency diagnostics, command wrapping,
violation annotation, and settings conversion.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path
from typing import Any, Mapping, Sequence

from openspace.services.runtime_support.settings import get_settings_for_source, update_settings_for_source

from .errors import SandboxDependencyError, SandboxUnavailableError
from .linux import build_linux_bwrap_argv, diagnose_linux_dependencies
from .macos import build_macos_argv, build_macos_profile, diagnose_macos_dependencies
from .platform_utils import get_platform, is_supported_platform
from .sandbox_utils import generate_command_tag, shell_join_command
from .settings_adapter import convert_to_sandbox_runtime_config
from .types import (
    Platform,
    SandboxDependencyIssue,
    SandboxPolicy,
    SandboxRuntimeConfig,
    SandboxSettings,
    SandboxViolation,
    SandboxWrappedCommand,
)
from .violation_store import SandboxViolationStore, violations_to_xml
from .windows import diagnose_windows_dependencies


_DENY_HINT_RE = re.compile(
    r"(Operation not permitted|Permission denied|sandbox-exec:.*deny|bwrap:|seccomp|Network is unreachable)",
    re.IGNORECASE | re.DOTALL,
)
_SINGLETONS: dict[str, "ProcessSandboxManager"] = {}


class ProcessSandboxManager:
    def __init__(
        self,
        *,
        cwd: str | Path | None = None,
        settings: Mapping[str, Any] | None = None,
        violation_store: SandboxViolationStore | None = None,
    ) -> None:
        self.cwd = str(Path(cwd or os.getcwd()).expanduser().resolve())
        self._settings_override = settings
        self._runtime_config: SandboxRuntimeConfig | None = None
        self._dependency_issues: list[SandboxDependencyIssue] | None = None
        self._initialized = False
        self._violation_store = violation_store or SandboxViolationStore()

    async def initialize(self) -> None:
        self.refresh_config()
        self._dependency_issues = self.check_dependencies()
        reason = self.get_unavailable_reason()
        if reason and self.is_sandbox_required():
            raise SandboxDependencyError(reason)
        self._initialized = self.is_sandboxing_enabled()

    async def reset(self) -> None:
        self._runtime_config = None
        self._dependency_issues = None
        self._initialized = False
        self._violation_store.clear()

    def refresh_config(self) -> None:
        self._runtime_config = convert_to_sandbox_runtime_config(
            self._settings_override,
            cwd=self.cwd,
        )
        self._dependency_issues = None

    def runtime_config(self) -> SandboxRuntimeConfig:
        if self._runtime_config is None:
            self.refresh_config()
        assert self._runtime_config is not None
        return self._runtime_config

    def is_supported_platform(self) -> bool:
        return is_supported_platform(self.platform)

    @property
    def platform(self) -> Platform:
        return get_platform()

    def is_platform_in_enabled_list(self) -> bool:
        platforms = self.runtime_config().settings.enabled_platforms
        if platforms is None:
            return True
        return self.platform in platforms

    def is_enabled_in_settings(self) -> bool:
        return self.runtime_config().settings.enabled

    def is_enabled(self) -> bool:
        return self.is_sandboxing_enabled()

    def is_sandboxing_enabled(self) -> bool:
        if not self.is_supported_platform():
            return False
        if not self.is_platform_in_enabled_list():
            return False
        if not self.is_enabled_in_settings():
            return False
        return not any(issue.severity == "error" for issue in self.check_dependencies())

    def is_sandbox_required(self) -> bool:
        settings = self.runtime_config().settings
        return settings.enabled and settings.fail_if_unavailable

    def get_unavailable_reason(self) -> str | None:
        if not self.is_enabled_in_settings():
            return None
        if not self.is_supported_platform():
            if self.platform == "wsl1":
                return "sandbox.enabled is set but WSL1 is not supported (requires WSL2)."
            return (
                "sandbox.enabled is set but this platform is not supported "
                "(requires macOS, Linux, or WSL2)."
            )
        if not self.is_platform_in_enabled_list():
            return (
                f"sandbox.enabled is set but {self.platform} is not in "
                "sandbox.enabledPlatforms."
            )
        errors = [issue.message for issue in self.check_dependencies() if issue.severity == "error"]
        if errors:
            return "sandbox.enabled is set but dependencies are missing: " + "; ".join(errors)
        return None

    def check_dependencies(self) -> list[SandboxDependencyIssue]:
        if self._dependency_issues is not None:
            return list(self._dependency_issues)
        platform = self.platform
        config = self.runtime_config()
        needs_network_bridge = bool(
            config.policy.allowed_domains or config.policy.denied_domains
        )
        if platform == "macos":
            issues = diagnose_macos_dependencies()
        elif platform in {"linux", "wsl2"}:
            issues = diagnose_linux_dependencies(
                needs_network_bridge=needs_network_bridge
            )
        elif platform == "windows":
            issues = diagnose_windows_dependencies()
        elif platform == "wsl1":
            issues = [
                SandboxDependencyIssue(
                    "error",
                    "unsupported_wsl1",
                    "WSL1 is not supported for process sandboxing.",
                    "Use WSL2 or native Linux.",
                )
            ]
        else:
            issues = [
                SandboxDependencyIssue(
                    "error",
                    "unsupported_platform",
                    "This platform is not supported for process sandboxing.",
                )
            ]
        self._dependency_issues = issues
        return list(issues)

    def get_linux_glob_pattern_warnings(self) -> list[str]:
        return list(self.runtime_config().linux_glob_warnings)

    def are_unsandboxed_commands_allowed(self) -> bool:
        return self.runtime_config().settings.allow_unsandboxed_commands

    def is_auto_allow_bash_if_sandboxed_enabled(self) -> bool:
        return self.runtime_config().settings.auto_allow_bash_if_sandboxed

    def are_settings_locked_by_policy(self) -> bool:
        return False

    def get_excluded_commands(self) -> list[str]:
        return list(self.runtime_config().settings.excluded_commands)

    def get_fs_read_config(self) -> dict[str, list[str]]:
        policy = self.runtime_config().policy
        return {"denyRead": list(policy.deny_read), "allowRead": list(policy.allow_read)}

    def get_fs_write_config(self) -> dict[str, list[str]]:
        policy = self.runtime_config().policy
        return {
            "allowWrite": list(policy.allow_write),
            "denyWrite": list(policy.deny_write),
        }

    def get_network_restriction_config(self) -> dict[str, Any]:
        policy = self.runtime_config().policy
        return {
            "allowedDomains": list(policy.allowed_domains),
            "deniedDomains": list(policy.denied_domains),
            "allowNetwork": policy.allow_network,
            "domainFiltering": (
                "unavailable" if policy.allowed_domains or policy.denied_domains else "none"
            ),
        }

    def get_allow_unix_sockets(self) -> list[str]:
        return list(self.runtime_config().policy.allow_unix_sockets)

    def get_allow_local_binding(self) -> bool:
        return self.runtime_config().policy.allow_local_binding

    def get_ignore_violations(self) -> dict[str, list[str]]:
        return dict(self.runtime_config().policy.ignore_violations)

    def get_enable_weaker_nested_sandbox(self) -> bool:
        return self.runtime_config().policy.enable_weaker_nested_sandbox

    async def wait_for_network_initialization(self) -> bool:
        return True

    def set_sandbox_settings(
        self,
        *,
        enabled: bool | None = None,
        auto_allow_bash_if_sandboxed: bool | None = None,
        allow_unsandboxed_commands: bool | None = None,
    ) -> None:
        existing = get_settings_for_source("localSettings", self.cwd) or {}
        sandbox = dict(existing.get("sandbox") or {})
        if enabled is not None:
            sandbox["enabled"] = enabled
        if auto_allow_bash_if_sandboxed is not None:
            sandbox["autoAllowBashIfSandboxed"] = auto_allow_bash_if_sandboxed
        if allow_unsandboxed_commands is not None:
            sandbox["allowUnsandboxedCommands"] = allow_unsandboxed_commands
        update = dict(existing)
        update["sandbox"] = sandbox
        update_settings_for_source("localSettings", update, cwd=self.cwd)
        self.refresh_config()

    async def wrap_command(
        self,
        command: str | Sequence[str],
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        shell: str = "/bin/bash",
        policy: SandboxPolicy | None = None,
    ) -> SandboxWrappedCommand:
        command_text = shell_join_command(command)
        if not self.is_sandboxing_enabled():
            reason = self.get_unavailable_reason() or "Sandboxing is disabled."
            raise SandboxUnavailableError(reason)

        effective_cwd = str(Path(cwd or self.cwd).expanduser().resolve())
        effective_env = dict(env or {})
        runtime = self.runtime_config()
        effective_policy = policy or runtime.policy
        command_tag = generate_command_tag(command_text)
        diagnostics: list[str] = []
        wrapped_env = {**effective_env, **self._proxy_env(runtime.settings)}

        if self.platform == "macos":
            profile = build_macos_profile(effective_policy, command_tag=command_tag)
            argv = build_macos_argv(command_text, shell=shell, profile=profile)
        elif self.platform in {"linux", "wsl2"}:
            argv, linux_diagnostics = build_linux_bwrap_argv(
                command_text,
                cwd=effective_cwd,
                shell=shell,
                policy=effective_policy,
            )
            diagnostics.extend(linux_diagnostics)
        else:
            raise SandboxUnavailableError(
                f"Sandboxing is not supported on platform {self.platform}."
            )

        return SandboxWrappedCommand(
            argv=argv,
            env=wrapped_env,
            cwd=effective_cwd,
            platform=self.platform,
            policy=effective_policy,
            command=command_text,
            command_tag=command_tag,
            diagnostics=diagnostics,
        )

    async def cleanup_after_command(
        self,
        wrapped: SandboxWrappedCommand | None = None,
    ) -> None:
        callbacks = list(wrapped.cleanup_callbacks) if wrapped else []
        for callback in callbacks:
            result = callback()
            if asyncio.iscoroutine(result):
                await result
        for path in self.runtime_config().post_command_scrub_paths:
            try:
                candidate = Path(path)
                if candidate.is_dir():
                    shutil.rmtree(candidate)
                else:
                    candidate.unlink()
            except FileNotFoundError:
                continue
            except OSError:
                continue

    def get_violation_store(self) -> SandboxViolationStore:
        return self._violation_store

    def annotate_stderr_with_sandbox_failures(
        self,
        command: str,
        stderr: str,
        *,
        command_tag: str | None = None,
    ) -> str:
        if "<sandbox_violations>" in stderr:
            return stderr
        violations: list[SandboxViolation] = []
        if command_tag:
            violations.extend(self._violation_store.for_command_tag(command_tag))
        if not violations:
            violations.extend(self._violation_store.for_command(command))
        if not violations and _DENY_HINT_RE.search(stderr):
            violations.append(
                SandboxViolation(
                    command=command,
                    command_tag=command_tag or generate_command_tag(command),
                    platform=self.platform,
                    operation=_infer_operation(stderr),
                    raw_message=_human_denial_hint(stderr, self.runtime_config().policy),
                )
            )
        xml = violations_to_xml(violations)
        if not xml:
            return stderr
        separator = "" if stderr.endswith("\n") or not stderr else "\n"
        return f"{stderr}{separator}{xml}"

    @staticmethod
    def _proxy_env(settings: SandboxSettings) -> dict[str, str]:
        env: dict[str, str] = {}
        if settings.network.http_proxy_port:
            value = f"http://127.0.0.1:{settings.network.http_proxy_port}"
            env["HTTP_PROXY"] = value
            env["HTTPS_PROXY"] = value
        if settings.network.socks_proxy_port:
            env["ALL_PROXY"] = (
                f"socks5://127.0.0.1:{settings.network.socks_proxy_port}"
            )
        return env


def diagnose_sandbox_dependencies(
    *,
    cwd: str | Path | None = None,
    settings: Mapping[str, Any] | None = None,
) -> list[SandboxDependencyIssue]:
    return ProcessSandboxManager(cwd=cwd, settings=settings).check_dependencies()


def get_process_sandbox_manager(
    *,
    cwd: str | Path | None = None,
    settings: Mapping[str, Any] | None = None,
) -> ProcessSandboxManager:
    if settings is not None:
        return ProcessSandboxManager(cwd=cwd, settings=settings)
    key = str(Path(cwd or os.getcwd()).expanduser().resolve())
    manager = _SINGLETONS.get(key)
    if manager is None:
        manager = ProcessSandboxManager(cwd=key)
        _SINGLETONS[key] = manager
        return manager
    manager.refresh_config()
    return manager


def _infer_operation(stderr: str) -> str:
    lowered = stderr.lower()
    if "network is unreachable" in lowered:
        return "network"
    if "write" in lowered:
        return "write"
    if "read" in lowered or "permission denied" in lowered:
        return "read"
    if "bwrap" in lowered or "seccomp" in lowered:
        return "process"
    return "unknown"


def _human_denial_hint(stderr: str, policy: SandboxPolicy) -> str:
    lowered = stderr.lower()
    if "network is unreachable" in lowered and not policy.allow_network:
        return "Sandbox blocked network access because sandbox.network is disabled."
    if "operation not permitted" in lowered or "permission denied" in lowered:
        return "Sandbox blocked an operation outside the configured filesystem or process policy."
    if "bwrap:" in lowered:
        return "bubblewrap reported a sandbox setup or runtime failure."
    if "sandbox-exec" in lowered:
        return "macOS sandbox-exec reported a denied operation."
    if "seccomp" in lowered:
        return "Sandbox seccomp policy blocked this operation."
    return "Sandbox blocked this operation."


__all__ = [
    "ProcessSandboxManager",
    "diagnose_sandbox_dependencies",
    "get_process_sandbox_manager",
]
