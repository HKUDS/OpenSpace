"""Process-level sandbox data contracts.

This module is the Python counterpart to OpenSpace's
``entrypoints/sandboxTypes.ts`` plus the runtime structures exposed by
``utils/sandbox/sandbox-adapter.ts``.  It intentionally models the local OS
process sandbox, not the existing provider sandbox registry under
``grounding.core.security.sandbox``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Mapping, Sequence


Platform = Literal["macos", "linux", "wsl1", "wsl2", "windows", "unsupported"]
IssueSeverity = Literal["error", "warning"]
ViolationOperation = Literal["read", "write", "network", "process", "unknown"]


@dataclass(slots=True)
class SandboxDependencyIssue:
    severity: IssueSeverity
    code: str
    message: str
    remediation: str | None = None

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }
        if self.remediation:
            data["remediation"] = self.remediation
        return data


@dataclass(slots=True)
class SandboxViolation:
    command: str
    command_tag: str
    platform: Platform | str
    operation: ViolationOperation = "unknown"
    path: str | None = None
    domain: str | None = None
    raw_message: str = ""
    timestamp_ms: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "command_tag": self.command_tag,
            "platform": self.platform,
            "operation": self.operation,
            "path": self.path,
            "domain": self.domain,
            "raw_message": self.raw_message,
            "timestamp_ms": self.timestamp_ms,
        }


@dataclass(slots=True)
class SandboxNetworkSettings:
    allowed_domains: list[str] = field(default_factory=list)
    allow_managed_domains_only: bool = False
    allow_unix_sockets: list[str] = field(default_factory=list)
    allow_all_unix_sockets: bool = False
    allow_local_binding: bool = False
    http_proxy_port: int | None = None
    socks_proxy_port: int | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "SandboxNetworkSettings":
        data = raw if isinstance(raw, Mapping) else {}
        return cls(
            allowed_domains=_string_list(data.get("allowedDomains")),
            allow_managed_domains_only=bool(data.get("allowManagedDomainsOnly", False)),
            allow_unix_sockets=_string_list(data.get("allowUnixSockets")),
            allow_all_unix_sockets=bool(data.get("allowAllUnixSockets", False)),
            allow_local_binding=bool(data.get("allowLocalBinding", False)),
            http_proxy_port=_optional_int(data.get("httpProxyPort")),
            socks_proxy_port=_optional_int(data.get("socksProxyPort")),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "allowedDomains": list(self.allowed_domains),
            "allowManagedDomainsOnly": self.allow_managed_domains_only,
            "allowUnixSockets": list(self.allow_unix_sockets),
            "allowAllUnixSockets": self.allow_all_unix_sockets,
            "allowLocalBinding": self.allow_local_binding,
            "httpProxyPort": self.http_proxy_port,
            "socksProxyPort": self.socks_proxy_port,
        }


@dataclass(slots=True)
class SandboxFilesystemSettings:
    allow_write: list[str] = field(default_factory=list)
    deny_write: list[str] = field(default_factory=list)
    deny_read: list[str] = field(default_factory=list)
    allow_read: list[str] = field(default_factory=list)
    allow_managed_read_paths_only: bool = False

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any] | None,
    ) -> "SandboxFilesystemSettings":
        data = raw if isinstance(raw, Mapping) else {}
        return cls(
            allow_write=_string_list(data.get("allowWrite")),
            deny_write=_string_list(data.get("denyWrite")),
            deny_read=_string_list(data.get("denyRead")),
            allow_read=_string_list(data.get("allowRead")),
            allow_managed_read_paths_only=bool(
                data.get("allowManagedReadPathsOnly", False)
            ),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "allowWrite": list(self.allow_write),
            "denyWrite": list(self.deny_write),
            "denyRead": list(self.deny_read),
            "allowRead": list(self.allow_read),
            "allowManagedReadPathsOnly": self.allow_managed_read_paths_only,
        }


@dataclass(slots=True)
class SandboxSettings:
    enabled: bool = False
    fail_if_unavailable: bool = False
    enabled_platforms: list[str] | None = None
    auto_allow_bash_if_sandboxed: bool = True
    allow_unsandboxed_commands: bool = True
    network: SandboxNetworkSettings = field(default_factory=SandboxNetworkSettings)
    filesystem: SandboxFilesystemSettings = field(default_factory=SandboxFilesystemSettings)
    ignore_violations: dict[str, list[str]] = field(default_factory=dict)
    enable_weaker_nested_sandbox: bool = False
    enable_weaker_network_isolation: bool = False
    excluded_commands: list[str] = field(default_factory=list)
    ripgrep_command: str | None = None
    ripgrep_args: list[str] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "SandboxSettings":
        data = raw if isinstance(raw, Mapping) else {}
        ripgrep = data.get("ripgrep") if isinstance(data.get("ripgrep"), Mapping) else {}
        enabled_platforms_raw = data.get("enabledPlatforms")
        enabled_platforms = (
            _string_list(enabled_platforms_raw)
            if isinstance(enabled_platforms_raw, list)
            else None
        )
        return cls(
            enabled=bool(data.get("enabled", False)),
            fail_if_unavailable=bool(data.get("failIfUnavailable", False)),
            enabled_platforms=enabled_platforms,
            auto_allow_bash_if_sandboxed=bool(
                data.get("autoAllowBashIfSandboxed", True)
            ),
            allow_unsandboxed_commands=bool(
                data.get("allowUnsandboxedCommands", True)
            ),
            network=SandboxNetworkSettings.from_mapping(data.get("network")),
            filesystem=SandboxFilesystemSettings.from_mapping(data.get("filesystem")),
            ignore_violations=_ignore_violations(data.get("ignoreViolations")),
            enable_weaker_nested_sandbox=bool(
                data.get("enableWeakerNestedSandbox", False)
            ),
            enable_weaker_network_isolation=bool(
                data.get("enableWeakerNetworkIsolation", False)
            ),
            excluded_commands=_string_list(data.get("excludedCommands")),
            ripgrep_command=(
                ripgrep.get("command") if isinstance(ripgrep.get("command"), str) else None
            ),
            ripgrep_args=_string_list(ripgrep.get("args")),
        )

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "enabled": self.enabled,
            "failIfUnavailable": self.fail_if_unavailable,
            "autoAllowBashIfSandboxed": self.auto_allow_bash_if_sandboxed,
            "allowUnsandboxedCommands": self.allow_unsandboxed_commands,
            "network": self.network.to_json(),
            "filesystem": self.filesystem.to_json(),
            "ignoreViolations": {
                key: list(values) for key, values in self.ignore_violations.items()
            },
            "enableWeakerNestedSandbox": self.enable_weaker_nested_sandbox,
            "enableWeakerNetworkIsolation": self.enable_weaker_network_isolation,
            "excludedCommands": list(self.excluded_commands),
        }
        if self.enabled_platforms is not None:
            data["enabledPlatforms"] = list(self.enabled_platforms)
        if self.ripgrep_command is not None or self.ripgrep_args:
            data["ripgrep"] = {
                "command": self.ripgrep_command or "rg",
                "args": list(self.ripgrep_args),
            }
        return data


@dataclass(slots=True)
class SandboxPolicy:
    name: str = "default"
    allow_read: list[str] = field(default_factory=list)
    deny_read: list[str] = field(default_factory=list)
    allow_write: list[str] = field(default_factory=list)
    deny_write: list[str] = field(default_factory=list)
    allowed_domains: list[str] = field(default_factory=list)
    denied_domains: list[str] = field(default_factory=list)
    allow_network: bool = True
    allow_unix_sockets: list[str] = field(default_factory=list)
    allow_all_unix_sockets: bool = False
    allow_local_binding: bool = False
    allow_syscalls: list[str] = field(default_factory=list)
    ignore_violations: dict[str, list[str]] = field(default_factory=dict)
    enable_weaker_nested_sandbox: bool = False
    enable_weaker_network_isolation: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "allowRead": list(self.allow_read),
            "denyRead": list(self.deny_read),
            "allowWrite": list(self.allow_write),
            "denyWrite": list(self.deny_write),
            "allowedDomains": list(self.allowed_domains),
            "deniedDomains": list(self.denied_domains),
            "allowNetwork": self.allow_network,
            "allowUnixSockets": list(self.allow_unix_sockets),
            "allowAllUnixSockets": self.allow_all_unix_sockets,
            "allowLocalBinding": self.allow_local_binding,
            "allowSyscalls": list(self.allow_syscalls),
            "ignoreViolations": {
                key: list(values) for key, values in self.ignore_violations.items()
            },
            "enableWeakerNestedSandbox": self.enable_weaker_nested_sandbox,
            "enableWeakerNetworkIsolation": self.enable_weaker_network_isolation,
        }


@dataclass(slots=True)
class SandboxRuntimeConfig:
    settings: SandboxSettings
    policy: SandboxPolicy
    linux_glob_warnings: list[str] = field(default_factory=list)
    ripgrep_command: str | None = None
    ripgrep_args: list[str] = field(default_factory=list)
    post_command_scrub_paths: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SandboxWrappedCommand:
    argv: list[str]
    env: dict[str, str]
    cwd: str
    platform: Platform | str
    policy: SandboxPolicy
    command: str
    command_tag: str
    diagnostics: list[str] = field(default_factory=list)
    cleanup_callbacks: list[Callable[[], Awaitable[None] | None]] = field(
        default_factory=list
    )

    def to_metadata(self) -> dict[str, Any]:
        return {
            "applied": True,
            "platform": self.platform,
            "policy_name": self.policy.name,
            "command_tag": self.command_tag,
            "diagnostics": list(self.diagnostics),
        }


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _ignore_violations(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, list[str]] = {}
    for key, entries in value.items():
        if isinstance(key, str):
            out[key] = _string_list(entries)
    return out


def dedupe_strings(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


__all__ = [
    "IssueSeverity",
    "Platform",
    "SandboxDependencyIssue",
    "SandboxNetworkSettings",
    "SandboxFilesystemSettings",
    "SandboxPolicy",
    "SandboxRuntimeConfig",
    "SandboxSettings",
    "SandboxViolation",
    "SandboxWrappedCommand",
    "ViolationOperation",
    "dedupe_strings",
]
