"""Local process sandbox runtime.

This package is separate from provider sandboxes under
``grounding.core.security``.
"""

from .errors import (
    SandboxDependencyError,
    SandboxError,
    SandboxPolicyError,
    SandboxUnavailableError,
)
from .manager import (
    ProcessSandboxManager,
    diagnose_sandbox_dependencies,
    get_process_sandbox_manager,
)
from .settings_adapter import (
    add_to_excluded_commands,
    convert_to_sandbox_runtime_config,
    remove_from_excluded_commands,
    resolve_path_pattern_for_sandbox,
    resolve_sandbox_filesystem_path,
)
from .types import (
    SandboxDependencyIssue,
    SandboxFilesystemSettings,
    SandboxNetworkSettings,
    SandboxPolicy,
    SandboxRuntimeConfig,
    SandboxSettings,
    SandboxViolation,
    SandboxWrappedCommand,
)
from .ui_utils import (
    build_sandbox_status,
    format_sandbox_doctor,
    format_sandbox_status,
    format_sandbox_violations,
    remove_sandbox_violation_tags,
    sandbox_doctor_status,
)
from .violation_store import SandboxViolationStore


__all__ = [
    "ProcessSandboxManager",
    "SandboxDependencyError",
    "SandboxDependencyIssue",
    "SandboxError",
    "SandboxFilesystemSettings",
    "SandboxNetworkSettings",
    "SandboxPolicy",
    "SandboxPolicyError",
    "SandboxRuntimeConfig",
    "SandboxSettings",
    "SandboxUnavailableError",
    "SandboxViolation",
    "SandboxViolationStore",
    "SandboxWrappedCommand",
    "add_to_excluded_commands",
    "build_sandbox_status",
    "convert_to_sandbox_runtime_config",
    "diagnose_sandbox_dependencies",
    "format_sandbox_doctor",
    "format_sandbox_status",
    "format_sandbox_violations",
    "get_process_sandbox_manager",
    "remove_sandbox_violation_tags",
    "remove_from_excluded_commands",
    "resolve_path_pattern_for_sandbox",
    "resolve_sandbox_filesystem_path",
    "sandbox_doctor_status",
]
