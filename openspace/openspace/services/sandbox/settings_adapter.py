"""Settings adapter for the process sandbox runtime.

This is the Python port of the OpenSpace adapter-layer functions in
``utils/sandbox/sandbox-adapter.ts``.  It converts OpenSpace's effective
settings plus permission rules into a platform-neutral
``SandboxRuntimeConfig`` consumed by :class:`ProcessSandboxManager`.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from openspace.services.runtime_support.settings import (
    FILE_SETTING_SOURCES,
    EditableSettingSource,
    SettingSource,
    get_effective_settings,
    get_project_root,
    get_settings_for_source,
    get_settings_path_for_source,
    update_settings_for_source,
)

from .sandbox_utils import (
    default_allow_write_paths,
    expand_sensitive_paths,
    has_glob_chars,
    normalize_path,
)
from .types import (
    SandboxPolicy,
    SandboxRuntimeConfig,
    SandboxSettings,
    dedupe_strings,
)


FILE_EDIT_TOOL_NAMES = {"edit", "Edit"}
FILE_READ_TOOL_NAMES = {"read", "Read"}
WEB_FETCH_TOOL_NAMES = {"web_fetch", "WebFetch"}


def permission_rule_value_from_string(rule_string: str) -> dict[str, str]:
    match = re.match(r"^([^(]+)\(([^)]+)\)$", rule_string)
    if not match:
        return {"toolName": rule_string}
    return {"toolName": match.group(1), "ruleContent": match.group(2)}


def permission_rule_extract_prefix(permission_rule: str) -> str | None:
    match = re.match(r"^(.+):\*$", permission_rule)
    return match.group(1) if match else None


def get_settings_root_path_for_source(
    source: SettingSource,
    cwd: str | Path | None = None,
) -> Path:
    path = get_settings_path_for_source(source, cwd)
    if path is not None:
        return path.parent.resolve()
    return get_project_root(cwd).resolve()


def resolve_path_pattern_for_sandbox(
    pattern: str,
    source: SettingSource,
    cwd: str | Path | None = None,
) -> str:
    """Resolve OpenSpace permission-rule path conventions for sandbox-runtime.

    OpenSpace semantics:
    - ``//path`` means absolute ``/path``.
    - ``/path`` means relative to the settings source root.
    - ``~/path`` and relative paths pass through for runtime normalization.
    """

    if pattern.startswith("//"):
        return pattern[1:]
    if pattern.startswith("/") and not pattern.startswith("//"):
        root = get_settings_root_path_for_source(source, cwd)
        return str((root / pattern[1:]).resolve())
    return pattern


def resolve_sandbox_filesystem_path(
    pattern: str,
    source: SettingSource,
    cwd: str | Path | None = None,
) -> str:
    """Resolve ``sandbox.filesystem`` paths.

    Unlike permission rules, ``/path`` is already absolute for explicit
    sandbox filesystem settings.  ``//path`` is accepted for compatibility.
    """

    if pattern.startswith("//"):
        return pattern[1:]
    expanded = Path(pattern).expanduser()
    if expanded.is_absolute():
        return str(expanded)
    root = get_settings_root_path_for_source(source, cwd)
    return str((root / expanded).resolve())


def should_allow_managed_sandbox_domains_only(
    cwd: str | Path | None = None,
) -> bool:
    policy = get_settings_for_source("envSettings", cwd) or {}
    sandbox = policy.get("sandbox") if isinstance(policy.get("sandbox"), Mapping) else {}
    network = sandbox.get("network") if isinstance(sandbox.get("network"), Mapping) else {}
    return bool(network.get("allowManagedDomainsOnly", False))


def should_allow_managed_read_paths_only(
    cwd: str | Path | None = None,
) -> bool:
    policy = get_settings_for_source("envSettings", cwd) or {}
    sandbox = policy.get("sandbox") if isinstance(policy.get("sandbox"), Mapping) else {}
    filesystem = (
        sandbox.get("filesystem") if isinstance(sandbox.get("filesystem"), Mapping) else {}
    )
    return bool(filesystem.get("allowManagedReadPathsOnly", False))


def convert_to_sandbox_runtime_config(
    settings: Mapping[str, Any] | None = None,
    *,
    cwd: str | Path | None = None,
) -> SandboxRuntimeConfig:
    effective = dict(settings or get_effective_settings(cwd))
    sandbox_settings = SandboxSettings.from_mapping(effective.get("sandbox"))
    permissions = _mapping(effective.get("permissions"))

    allowed_domains: list[str] = []
    denied_domains: list[str] = []

    if should_allow_managed_sandbox_domains_only(cwd):
        # OpenSpace has no enterprise policy source yet. Keep the branch
        # explicit and intentionally empty rather than mixing user domains into
        # a managed-only policy.
        pass
    else:
        allowed_domains.extend(sandbox_settings.network.allowed_domains)
        for rule_string in _string_list(permissions.get("allow")):
            rule = permission_rule_value_from_string(rule_string)
            if (
                rule.get("toolName") in WEB_FETCH_TOOL_NAMES
                and rule.get("ruleContent", "").startswith("domain:")
            ):
                allowed_domains.append(rule["ruleContent"][len("domain:") :])

    for rule_string in _string_list(permissions.get("deny")):
        rule = permission_rule_value_from_string(rule_string)
        if (
            rule.get("toolName") in WEB_FETCH_TOOL_NAMES
            and rule.get("ruleContent", "").startswith("domain:")
        ):
            denied_domains.append(rule["ruleContent"][len("domain:") :])

    cwd_path = normalize_path(".", base_dir=cwd)
    allow_write: list[str] = default_allow_write_paths(cwd_path)
    deny_write: list[str] = []
    deny_read: list[str] = expand_sensitive_paths()
    allow_read: list[str] = []
    linux_glob_warnings: list[str] = []
    post_command_scrub_paths: list[str] = []

    for source in FILE_SETTING_SOURCES:
        settings_path = get_settings_path_for_source(source, cwd)
        if settings_path is not None:
            deny_write.append(str(settings_path.resolve()))
        source_settings = get_settings_for_source(source, cwd) or {}
        source_permissions = _mapping(source_settings.get("permissions"))
        _collect_permission_paths(
            source_permissions,
            source,
            allow_write=allow_write,
            deny_write=deny_write,
            deny_read=deny_read,
            cwd=cwd,
        )
        _collect_filesystem_paths(
            _mapping(_mapping(source_settings.get("sandbox")).get("filesystem")),
            source,
            allow_write=allow_write,
            deny_write=deny_write,
            deny_read=deny_read,
            allow_read=allow_read,
            linux_glob_warnings=linux_glob_warnings,
            cwd=cwd,
        )

    # Effective settings may include env/runtime sandbox overrides. Their
    # explicit filesystem paths use cwd as the resolution root.
    _collect_filesystem_paths(
        _mapping(_mapping(effective.get("sandbox")).get("filesystem")),
        "runtimeSettings",
        allow_write=allow_write,
        deny_write=deny_write,
        deny_read=deny_read,
        allow_read=allow_read,
        linux_glob_warnings=linux_glob_warnings,
        cwd=cwd,
    )

    allow_write.extend(_string_list(permissions.get("additionalDirectories")))
    _collect_bare_git_write_guards(
        cwd_path,
        deny_write=deny_write,
        post_command_scrub_paths=post_command_scrub_paths,
    )
    worktree_repo = detect_worktree_main_repo_path(cwd_path)
    if worktree_repo and worktree_repo != cwd_path:
        allow_write.append(worktree_repo)

    policy = SandboxPolicy(
        allow_read=dedupe_strings([normalize_path(p, base_dir=cwd_path) for p in allow_read]),
        deny_read=dedupe_strings([normalize_path(p, base_dir=cwd_path) for p in deny_read]),
        allow_write=dedupe_strings(
            [normalize_path(p, base_dir=cwd_path) for p in allow_write]
        ),
        deny_write=dedupe_strings(
            [normalize_path(p, base_dir=cwd_path) for p in deny_write]
        ),
        allowed_domains=dedupe_strings(allowed_domains),
        denied_domains=dedupe_strings(denied_domains),
        allow_network=not denied_domains or bool(allowed_domains),
        allow_unix_sockets=list(sandbox_settings.network.allow_unix_sockets),
        allow_all_unix_sockets=sandbox_settings.network.allow_all_unix_sockets,
        allow_local_binding=sandbox_settings.network.allow_local_binding,
        ignore_violations=dict(sandbox_settings.ignore_violations),
        enable_weaker_nested_sandbox=sandbox_settings.enable_weaker_nested_sandbox,
        enable_weaker_network_isolation=sandbox_settings.enable_weaker_network_isolation,
    )
    return SandboxRuntimeConfig(
        settings=sandbox_settings,
        policy=policy,
        linux_glob_warnings=dedupe_strings(linux_glob_warnings),
        ripgrep_command=sandbox_settings.ripgrep_command,
        ripgrep_args=list(sandbox_settings.ripgrep_args),
        post_command_scrub_paths=dedupe_strings(post_command_scrub_paths),
    )


def add_to_excluded_commands(
    command: str,
    permission_updates: Sequence[Mapping[str, Any]] | None = None,
    *,
    cwd: str | Path | None = None,
    source: EditableSettingSource = "localSettings",
) -> str:
    existing = get_settings_for_source(source, cwd) or {}
    sandbox = dict(_mapping(existing.get("sandbox")))
    excluded = _string_list(sandbox.get("excludedCommands"))

    pattern = command
    for update in permission_updates or []:
        if update.get("type") != "addRules":
            continue
        for rule in update.get("rules", []):
            if not isinstance(rule, Mapping):
                continue
            if rule.get("toolName") not in {"bash", "Bash"}:
                continue
            content = rule.get("ruleContent")
            if isinstance(content, str):
                pattern = permission_rule_extract_prefix(content) or content
                break

    if pattern not in excluded:
        excluded.append(pattern)
        sandbox["excludedCommands"] = excluded
        updated = dict(existing)
        updated["sandbox"] = sandbox
        update_settings_for_source(source, updated, cwd=cwd)
    return pattern


def remove_from_excluded_commands(
    command: str,
    *,
    cwd: str | Path | None = None,
    source: EditableSettingSource = "localSettings",
) -> bool:
    existing = get_settings_for_source(source, cwd) or {}
    sandbox = dict(_mapping(existing.get("sandbox")))
    excluded = _string_list(sandbox.get("excludedCommands"))
    if command not in excluded:
        return False

    sandbox["excludedCommands"] = [pattern for pattern in excluded if pattern != command]
    updated = dict(existing)
    updated["sandbox"] = sandbox
    update_settings_for_source(source, updated, cwd=cwd)
    return True


def detect_worktree_main_repo_path(cwd: str | Path) -> str | None:
    git_path = Path(cwd) / ".git"
    try:
        content = git_path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"^gitdir:\s*(.+)$", content, re.MULTILINE)
    if not match:
        return None
    gitdir = (Path(cwd) / match.group(1).strip()).resolve()
    marker = f"{os.sep}.git{os.sep}worktrees{os.sep}"
    raw = str(gitdir)
    index = raw.rfind(marker)
    if index <= 0:
        return None
    return raw[:index]


def _collect_permission_paths(
    permissions: Mapping[str, Any],
    source: SettingSource,
    *,
    allow_write: list[str],
    deny_write: list[str],
    deny_read: list[str],
    cwd: str | Path | None,
) -> None:
    for rule_string in _string_list(permissions.get("allow")):
        rule = permission_rule_value_from_string(rule_string)
        if rule.get("toolName") in FILE_EDIT_TOOL_NAMES and rule.get("ruleContent"):
            allow_write.append(
                resolve_path_pattern_for_sandbox(rule["ruleContent"], source, cwd)
            )
    for rule_string in _string_list(permissions.get("deny")):
        rule = permission_rule_value_from_string(rule_string)
        if rule.get("toolName") in FILE_EDIT_TOOL_NAMES and rule.get("ruleContent"):
            deny_write.append(
                resolve_path_pattern_for_sandbox(rule["ruleContent"], source, cwd)
            )
        if rule.get("toolName") in FILE_READ_TOOL_NAMES and rule.get("ruleContent"):
            deny_read.append(
                resolve_path_pattern_for_sandbox(rule["ruleContent"], source, cwd)
            )


def _collect_filesystem_paths(
    filesystem: Mapping[str, Any],
    source: SettingSource,
    *,
    allow_write: list[str],
    deny_write: list[str],
    deny_read: list[str],
    allow_read: list[str],
    linux_glob_warnings: list[str],
    cwd: str | Path | None,
) -> None:
    for key, target in (
        ("allowWrite", allow_write),
        ("denyWrite", deny_write),
        ("denyRead", deny_read),
        ("allowRead", allow_read),
    ):
        for value in _string_list(filesystem.get(key)):
            resolved = resolve_sandbox_filesystem_path(value, source, cwd)
            target.append(resolved)
            if has_glob_chars(value):
                linux_glob_warnings.append(value)


def _collect_bare_git_write_guards(
    cwd: str,
    *,
    deny_write: list[str],
    post_command_scrub_paths: list[str],
) -> None:
    for name in ("HEAD", "objects", "refs", "hooks", "config"):
        path = Path(cwd) / name
        if path.exists():
            deny_write.append(str(path))
        else:
            post_command_scrub_paths.append(str(path))
    deny_write.append(str(Path(cwd) / ".openspace" / "skills"))
    deny_write.append(str(Path(cwd) / ".openspace" / "settings.json"))
    deny_write.append(str(Path(cwd) / ".openspace" / "settings.local.json"))
    deny_write.append(str(Path(tempfile.gettempdir()) / "openspace-settings"))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _string_list(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


__all__ = [
    "add_to_excluded_commands",
    "convert_to_sandbox_runtime_config",
    "detect_worktree_main_repo_path",
    "get_settings_root_path_for_source",
    "permission_rule_extract_prefix",
    "permission_rule_value_from_string",
    "resolve_path_pattern_for_sandbox",
    "resolve_sandbox_filesystem_path",
    "should_allow_managed_sandbox_domains_only",
]
