"""OpenSpace runtime settings service.

User-visible runtime and product settings should read and write through this
module rather than ad-hoc JSON helpers.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Mapping, MutableMapping, Sequence

from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

SettingSource = Literal[
    "defaults",
    "userSettings",
    "projectSettings",
    "localSettings",
    "envSettings",
    "runtimeSettings",
]
EditableSettingSource = Literal["userSettings", "projectSettings", "localSettings"]

OPENSPACE_CONFIG_HOME_ENV = "OPENSPACE_CONFIG_HOME"
# OpenSpace has not published a SchemaStore entry for settings.json yet. Keep
# this unset instead of advertising a public URL that returns 404.
OPENSPACE_SETTINGS_SCHEMA_URL: str | None = None

EDITABLE_SOURCES: tuple[EditableSettingSource, ...] = (
    "userSettings",
    "projectSettings",
    "localSettings",
)
FILE_SETTING_SOURCES: tuple[EditableSettingSource, ...] = EDITABLE_SOURCES
MERGE_SOURCES: tuple[SettingSource, ...] = (
    "defaults",
    "userSettings",
    "projectSettings",
    "localSettings",
    "envSettings",
    "runtimeSettings",
)

THEME_OPTIONS = ("dark", "light")
EDITOR_MODE_OPTIONS = ("normal", "vim")
NOTIFICATION_CHANNEL_OPTIONS = (
    "auto",
    "iterm2",
    "iterm2_with_bell",
    "terminal_bell",
    "kitty",
    "ghostty",
    "notifications_disabled",
)
TEAMMATE_MODE_OPTIONS = ("auto", "tmux", "in-process")
PERMISSION_MODE_OPTIONS = (
    "acceptEdits",
    "bypassPermissions",
    "default",
    "dontAsk",
    "plan",
)
MEMORY_MODE_OPTIONS = ("direct", "daily_log")
SANDBOX_PLATFORM_OPTIONS = ("macos", "linux", "wsl2", "windows")


@dataclass(slots=True)
class SettingsError:
    source: SettingSource
    path: str | None
    key_path: str
    message: str
    invalid_value: Any = None


@dataclass(slots=True)
class SettingsSourceSnapshot:
    source: SettingSource
    path: str | None
    settings: dict[str, Any]


@dataclass(slots=True)
class AutoDreamSettings:
    enabled: bool = False
    min_hours: float = 24.0
    min_sessions: int = 5
    scan_interval_seconds: float = 600.0
    model: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "minHours": self.min_hours,
            "minSessions": self.min_sessions,
            "scanIntervalSeconds": self.scan_interval_seconds,
            "model": self.model,
        }


@dataclass(slots=True)
class DailyLogSettings:
    retention_days: int = 90
    consolidate_on_dream: bool = True

    def to_json(self) -> dict[str, Any]:
        return {
            "retentionDays": self.retention_days,
            "consolidateOnDream": self.consolidate_on_dream,
        }


@dataclass(slots=True)
class MemorySettings:
    mode: str = "direct"
    daily_log: DailyLogSettings = field(default_factory=DailyLogSettings)

    def to_json(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "dailyLog": self.daily_log.to_json(),
        }


@dataclass(slots=True)
class AttachmentSettings:
    todo_reminder_enabled: bool = True

    def to_json(self) -> dict[str, Any]:
        return {
            "todoReminderEnabled": self.todo_reminder_enabled,
        }


@dataclass(slots=True)
class PermissionSettings:
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    ask: list[str] = field(default_factory=list)
    default_mode: str = "default"
    additional_directories: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "allow": list(self.allow),
            "deny": list(self.deny),
            "ask": list(self.ask),
            "defaultMode": self.default_mode,
            "additionalDirectories": list(self.additional_directories),
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
class UISettings:
    theme: str = "dark"
    editor_mode: str = "normal"
    verbose: bool = False
    preferred_notif_channel: str = "auto"
    show_turn_duration: bool = True
    terminal_progress_bar_enabled: bool = True

    def to_json(self) -> dict[str, Any]:
        return {
            "theme": self.theme,
            "editorMode": self.editor_mode,
            "verbose": self.verbose,
            "preferredNotifChannel": self.preferred_notif_channel,
            "showTurnDuration": self.show_turn_duration,
            "terminalProgressBarEnabled": self.terminal_progress_bar_enabled,
        }


@dataclass(slots=True)
class EngineSettings:
    auto_compact_enabled: bool = True
    file_checkpointing_enabled: bool = True
    todo_feature_enabled: bool = True
    model: str | None = None
    always_thinking_enabled: bool | None = None
    permissions: PermissionSettings = field(default_factory=PermissionSettings)
    language: str | None = None
    auto_memory_enabled: bool = True
    auto_memory_directory: str | None = None
    auto_dream: AutoDreamSettings = field(default_factory=AutoDreamSettings)
    memory: MemorySettings = field(default_factory=MemorySettings)
    sandbox: SandboxSettings = field(default_factory=SandboxSettings)

    def to_json(self) -> dict[str, Any]:
        return {
            "autoCompactEnabled": self.auto_compact_enabled,
            "fileCheckpointingEnabled": self.file_checkpointing_enabled,
            "todoFeatureEnabled": self.todo_feature_enabled,
            "model": self.model,
            "alwaysThinkingEnabled": self.always_thinking_enabled,
            "permissions": self.permissions.to_json(),
            "language": self.language,
            "autoMemoryEnabled": self.auto_memory_enabled,
            "autoMemoryDirectory": self.auto_memory_directory,
            "autoDream": self.auto_dream.to_json(),
            "memory": self.memory.to_json(),
            "sandbox": self.sandbox.to_json(),
        }


@dataclass(slots=True)
class ExperimentalSettings:
    teammate_mode: str = "auto"
    output_style: str = "default"
    attachments: AttachmentSettings = field(default_factory=AttachmentSettings)

    def to_json(self) -> dict[str, Any]:
        return {
            "teammateMode": self.teammate_mode,
            "outputStyle": self.output_style,
            "attachments": self.attachments.to_json(),
        }


@dataclass(slots=True)
class Settings:
    ui: UISettings = field(default_factory=UISettings)
    engine: EngineSettings = field(default_factory=EngineSettings)
    experimental: ExperimentalSettings = field(default_factory=ExperimentalSettings)
    schema: str | None = None

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        data.update(self.ui.to_json())
        data.update(self.engine.to_json())
        data.update(self.experimental.to_json())
        if self.schema is not None:
            data["$schema"] = self.schema
        return data

    def get_path(self, key: str, default: Any = None) -> Any:
        return _get_path(self.to_json(), key, default)


@dataclass(slots=True)
class RuntimeSettingsState:
    remote_session: bool = False
    remote_memory_dir: Path | None = None
    cli_model: str | None = None
    permission_mode: str | None = None
    always_thinking_enabled: bool | None = None


@dataclass(slots=True)
class SettingsSnapshot:
    settings: Settings
    raw: dict[str, Any]
    sources: list[SettingsSourceSnapshot]
    errors: list[SettingsError]
    cwd: str
    project_root: str
    loaded_at: float


_CACHE: dict[tuple[Any, ...], SettingsSnapshot] = {}
_SUBSCRIBERS: list[Callable[[SettingsSnapshot], Awaitable[None] | None]] = []
_WATCH_TASK: asyncio.Task[None] | None = None
_WATCH_CWD: str | None = None
_WATCH_INTERVAL_S = 1.0
_WATCH_MTIMES: dict[str, float | None] = {}
_INTERNAL_WRITES: dict[str, float] = {}

_WRITABLE_SETTING_KEYS: frozenset[str] = frozenset(
    {
        "theme",
        "editorMode",
        "verbose",
        "preferredNotifChannel",
        "showTurnDuration",
        "terminalProgressBarEnabled",
        "autoCompactEnabled",
        "fileCheckpointingEnabled",
        "todoFeatureEnabled",
        "model",
        "alwaysThinkingEnabled",
        "permissions.allow",
        "permissions.deny",
        "permissions.ask",
        "permissions.defaultMode",
        "permissions.additionalDirectories",
        "hooks",
        "language",
        "autoMemoryEnabled",
        "autoMemoryDirectory",
        "autoDream.enabled",
        "autoDream.minHours",
        "autoDream.minSessions",
        "autoDream.scanIntervalSeconds",
        "autoDream.model",
        "memory.mode",
        "memory.dailyLog.retentionDays",
        "memory.dailyLog.consolidateOnDream",
        "sandbox.enabled",
        "sandbox.failIfUnavailable",
        "sandbox.enabledPlatforms",
        "sandbox.autoAllowBashIfSandboxed",
        "sandbox.allowUnsandboxedCommands",
        "sandbox.network.allowedDomains",
        "sandbox.network.allowManagedDomainsOnly",
        "sandbox.network.allowUnixSockets",
        "sandbox.network.allowAllUnixSockets",
        "sandbox.network.allowLocalBinding",
        "sandbox.network.httpProxyPort",
        "sandbox.network.socksProxyPort",
        "sandbox.filesystem.allowWrite",
        "sandbox.filesystem.denyWrite",
        "sandbox.filesystem.denyRead",
        "sandbox.filesystem.allowRead",
        "sandbox.filesystem.allowManagedReadPathsOnly",
        "sandbox.ignoreViolations",
        "sandbox.enableWeakerNestedSandbox",
        "sandbox.enableWeakerNetworkIsolation",
        "sandbox.excludedCommands",
        "sandbox.ripgrep.command",
        "sandbox.ripgrep.args",
        "teammateMode",
        "outputStyle",
        "attachments.todoReminderEnabled",
    }
)


def get_openspace_config_home_dir(config_home: str | Path | None = None) -> Path:
    if config_home is not None:
        return Path(config_home).expanduser().resolve()
    env_home = os.environ.get(OPENSPACE_CONFIG_HOME_ENV)
    if env_home:
        return Path(env_home).expanduser().resolve()
    return (Path.home() / ".openspace").resolve()


def get_project_root(cwd: str | Path | None = None) -> Path:
    try:
        from openspace.services.memory.paths import find_project_root

        return find_project_root(cwd)
    except Exception:
        return Path(cwd or os.getcwd()).expanduser().resolve()


def get_settings_path_for_source(
    source: SettingSource,
    cwd: str | Path | None = None,
) -> Path | None:
    if source == "userSettings":
        return get_openspace_config_home_dir() / "settings.json"
    if source == "projectSettings":
        return get_project_root(cwd) / ".openspace" / "settings.json"
    if source == "localSettings":
        return get_project_root(cwd) / ".openspace" / "settings.local.json"
    return None


def get_settings_for_source(
    source: SettingSource,
    cwd: str | Path | None = None,
    *,
    strict: bool = False,
) -> dict[str, Any] | None:
    if source == "defaults":
        return _default_raw_settings()
    if source == "envSettings":
        return _env_settings()
    if source == "runtimeSettings":
        return None
    path = get_settings_path_for_source(source, cwd)
    if path is None or not path.is_file():
        return None
    result = _parse_settings_file(path, source, strict=strict)
    if strict and result.settings is None and result.errors:
        first = result.errors[0]
        raise ValueError(f"{first.key_path}: {first.message}")
    return result.settings


def get_settings(
    cwd: str | Path | None = None,
    *,
    refresh: bool = False,
    runtime: RuntimeSettingsState | Mapping[str, Any] | None = None,
) -> Settings:
    return get_settings_with_errors(cwd, refresh=refresh, runtime=runtime).settings


def get_settings_with_errors(
    cwd: str | Path | None = None,
    *,
    refresh: bool = False,
    runtime: RuntimeSettingsState | Mapping[str, Any] | None = None,
) -> SettingsSnapshot:
    resolved_cwd = str(Path(cwd or os.getcwd()).expanduser().resolve())
    project_root = str(get_project_root(resolved_cwd))
    runtime_state = _coerce_runtime_state(runtime)
    cache_key = _cache_key(resolved_cwd, project_root, runtime_state)
    if not refresh and cache_key in _CACHE:
        return _CACHE[cache_key]

    raw: dict[str, Any] = {}
    sources: list[SettingsSourceSnapshot] = []
    errors: list[SettingsError] = []

    defaults = _default_raw_settings()
    raw = _merge_settings(raw, defaults)
    sources.append(SettingsSourceSnapshot("defaults", None, defaults))

    for source in FILE_SETTING_SOURCES:
        path = get_settings_path_for_source(source, resolved_cwd)
        if path is None:
            continue
        if not path.is_file():
            continue
        result = _parse_settings_file(path, source)
        errors.extend(result.errors)
        if result.settings is None:
            continue
        raw = _merge_settings(raw, result.settings)
        if result.settings:
            sources.append(SettingsSourceSnapshot(source, str(path), result.settings))

    env_raw = _env_settings()
    if env_raw:
        raw = _merge_settings(raw, env_raw)
        sources.append(SettingsSourceSnapshot("envSettings", None, env_raw))

    runtime_raw = _runtime_settings(runtime_state)
    if runtime_raw:
        raw = _merge_settings(raw, runtime_raw)
        sources.append(SettingsSourceSnapshot("runtimeSettings", None, runtime_raw))

    settings, normalization_errors = _settings_from_raw(raw, "runtimeSettings", None)
    errors.extend(normalization_errors)
    snapshot = SettingsSnapshot(
        settings=settings,
        raw=raw,
        sources=sources,
        errors=errors,
        cwd=resolved_cwd,
        project_root=project_root,
        loaded_at=time.time(),
    )
    _CACHE[cache_key] = snapshot
    return snapshot


def get_setting(
    key: str,
    default: Any = None,
    *,
    cwd: str | Path | None = None,
    refresh: bool = False,
    runtime: RuntimeSettingsState | Mapping[str, Any] | None = None,
) -> Any:
    snapshot = get_settings_with_errors(cwd, refresh=refresh, runtime=runtime)
    return _get_path(snapshot.raw, key, snapshot.settings.get_path(key, default))


def update_setting(
    key: str,
    value: Any,
    *,
    cwd: str | Path | None = None,
    source: EditableSettingSource = "userSettings",
) -> SettingsSnapshot:
    if key not in _WRITABLE_SETTING_KEYS:
        raise ValueError(f"Unknown setting: {key}")
    existing = get_settings_for_source(source, cwd, strict=False) or {}
    update = _build_path_update(key, value)
    updated = _merge_for_update(existing, update)
    update_settings_for_source(source, updated, cwd=cwd)
    return notify_settings_changed(source, cwd)


def update_settings_for_source(
    source: EditableSettingSource,
    settings: Mapping[str, Any],
    cwd: str | Path | None = None,
) -> SettingsSnapshot:
    if source not in EDITABLE_SOURCES:
        raise ValueError(f"source {source!r} is not editable in OpenSpace")
    path = get_settings_path_for_source(source, cwd)
    if path is None:
        raise ValueError(f"no settings path defined for source {source!r}")
    data = dict(settings)
    parse = _validate_settings_data(data, source, str(path), strict=True)
    fatal_update_errors = [
        err
        for err in parse.errors
        if not err.message.startswith("Invalid permission rule")
        and not err.message.startswith("Non-string")
    ]
    if fatal_update_errors:
        first = fatal_update_errors[0]
        raise ValueError(f"{first.key_path}: {first.message}")
    _atomic_write_json(path, parse.settings or data)
    if source == "localSettings":
        _ensure_local_settings_gitignored(path)
    _INTERNAL_WRITES[str(path)] = time.time()
    return notify_settings_changed(source, cwd)


save_settings_for_source = update_settings_for_source


def get_effective_settings(cwd: str | Path | None = None) -> dict[str, Any]:
    return get_settings_with_errors(cwd).raw


def is_settings_file(path: str | Path, cwd: str | Path | None = None) -> bool:
    candidate = Path(path).expanduser()
    try:
        candidate = candidate.resolve()
    except OSError:
        candidate = candidate.absolute()

    for source in FILE_SETTING_SOURCES:
        source_path = get_settings_path_for_source(source, cwd)
        if source_path is not None and candidate == source_path.resolve():
            return True

    # Match OpenSpace's broad "settings under config dir" guard for FileEdit, while
    # keeping ordinary project files named settings.json editable.
    return (
        candidate.name in {"settings.json", "settings.local.json"}
        and candidate.parent.name == ".openspace"
    )


def validate_settings_edit(
    path: str | Path,
    new_content: str,
    old_content: str | None = None,
    *,
    cwd: str | Path | None = None,
) -> str | None:
    if not is_settings_file(path, cwd):
        return None
    if old_content is not None:
        before = _validate_settings_content(
            old_content,
            "localSettings",
            str(path),
            strict=True,
        )
        if before.settings is None or before.errors:
            return None

    after = _validate_settings_content(
        new_content,
        "localSettings",
        str(path),
        strict=True,
    )
    if after.settings is not None and not after.errors:
        return None
    lines = [
        "OpenSpace settings.json validation failed after edit:",
        *[f"- {err.key_path or '<root>'}: {err.message}" for err in after.errors[:8]],
        "Do not update settings unless the final file is valid JSON matching the OpenSpace settings schema.",
    ]
    if len(after.errors) > 8:
        lines.insert(-1, f"... {len(after.errors) - 8} more validation errors")
    return "\n".join(lines)


def subscribe_settings(
    callback: Callable[[SettingsSnapshot], Awaitable[None] | None],
) -> Callable[[], None]:
    _SUBSCRIBERS.append(callback)

    def unsubscribe() -> None:
        if callback in _SUBSCRIBERS:
            _SUBSCRIBERS.remove(callback)

    return unsubscribe


def start_settings_watcher(
    cwd: str | Path | None = None,
    *,
    interval_s: float = 1.0,
) -> None:
    global _WATCH_TASK, _WATCH_CWD, _WATCH_INTERVAL_S, _WATCH_MTIMES
    if _WATCH_TASK is not None and not _WATCH_TASK.done():
        return
    _WATCH_CWD = str(Path(cwd or os.getcwd()).expanduser().resolve())
    _WATCH_INTERVAL_S = max(float(interval_s), 0.1)
    _WATCH_MTIMES = {
        str(path): _file_mtime(path)
        for path in _watched_paths(_WATCH_CWD)
    }
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _WATCH_TASK = loop.create_task(_watch_settings_loop())


async def stop_settings_watcher() -> None:
    global _WATCH_TASK
    task = _WATCH_TASK
    _WATCH_TASK = None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def notify_settings_changed(
    source: SettingSource,
    cwd: str | Path | None = None,
) -> SettingsSnapshot:
    del source
    reset_settings_cache()
    snapshot = get_settings_with_errors(cwd, refresh=True)
    for callback in list(_SUBSCRIBERS):
        try:
            result = callback(snapshot)
            if asyncio.iscoroutine(result):
                try:
                    asyncio.get_running_loop().create_task(result)
                except RuntimeError:
                    pass
        except Exception:
            logger.debug("Settings subscriber failed", exc_info=True)
    return snapshot


def reset_settings_cache() -> None:
    _CACHE.clear()


def _watched_paths(cwd: str | Path | None) -> list[Path]:
    return [
        path
        for source in FILE_SETTING_SOURCES
        for path in [get_settings_path_for_source(source, cwd)]
        if path is not None
    ]


async def _watch_settings_loop() -> None:
    while True:
        await asyncio.sleep(_WATCH_INTERVAL_S)
        cwd = _WATCH_CWD or os.getcwd()
        changed = False
        for path in _watched_paths(cwd):
            key = str(path)
            current = _file_mtime(path)
            previous = _WATCH_MTIMES.get(key)
            if current != previous:
                _WATCH_MTIMES[key] = current
                changed = True
        if changed:
            notify_settings_changed("localSettings", cwd)


def _file_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


@dataclass(slots=True)
class _ParseResult:
    settings: dict[str, Any] | None
    errors: list[SettingsError]


def _parse_settings_file(
    path: Path,
    source: SettingSource,
    *,
    strict: bool = False,
) -> _ParseResult:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        if strict:
            raise
        logger.warning("Failed to read settings file %s: %s", path, exc)
        return _ParseResult(None, [])
    return _validate_settings_content(content, source, str(path), strict=strict)


def _validate_settings_content(
    content: str,
    source: SettingSource,
    path: str | None,
    *,
    strict: bool,
) -> _ParseResult:
    stripped = content.strip()
    if not stripped:
        return _ParseResult({}, [])
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        return _ParseResult(
            None,
            [
                SettingsError(
                    source=source,
                    path=path,
                    key_path="",
                    message=f"Invalid JSON: {exc.msg}",
                )
            ],
        )
    if not isinstance(data, dict):
        return _ParseResult(
            None,
            [
                SettingsError(
                    source=source,
                    path=path,
                    key_path="",
                    message="Settings file must contain a JSON object",
                    invalid_value=data,
                )
            ],
        )
    return _validate_settings_data(data, source, path, strict=strict)


def _validate_settings_data(
    data: Mapping[str, Any],
    source: SettingSource,
    path: str | None,
    *,
    strict: bool,
) -> _ParseResult:
    working = dict(data)
    errors: list[SettingsError] = []
    _filter_attachment_settings(working, source, path, errors, strict=strict)
    _filter_permission_rules(working, source, path, errors)
    known_errors = _validate_known_schema(working, source, path, strict=strict)
    errors.extend(known_errors)
    fatal_errors = [
        err
        for err in known_errors
        if not (err.message.startswith("Unrecognized field") and not strict)
    ]
    if fatal_errors:
        return _ParseResult(None, errors)
    if source == "projectSettings" and "autoMemoryDirectory" in working:
        working = dict(working)
        working.pop("autoMemoryDirectory", None)
        errors.append(
            SettingsError(
                source=source,
                path=path,
                key_path="autoMemoryDirectory",
                message="autoMemoryDirectory is ignored in projectSettings for security",
            )
        )
    return _ParseResult(working, errors)


def _filter_attachment_settings(
    data: MutableMapping[str, Any],
    source: SettingSource,
    path: str | None,
    errors: list[SettingsError],
    *,
    strict: bool,
) -> None:
    if strict:
        return
    attachments = data.get("attachments")
    if not isinstance(attachments, dict):
        return
    copied: dict[str, Any] = {}
    for key, value in attachments.items():
        if key == "todoReminderEnabled":
            copied[key] = value
            continue
        errors.append(
            SettingsError(
                source=source,
                path=path,
                key_path=f"attachments.{key}",
                message=(
                    "Unsupported attachment setting ignored; only "
                    "attachments.todoReminderEnabled is public"
                ),
                invalid_value=value,
            )
        )
    data["attachments"] = copied


def _filter_permission_rules(
    data: MutableMapping[str, Any],
    source: SettingSource,
    path: str | None,
    errors: list[SettingsError],
) -> None:
    permissions = data.get("permissions")
    if not isinstance(permissions, dict):
        return
    try:
        from openspace.grounding.core.permissions.types import parse_rule_value
    except Exception:
        parse_rule_value = None  # type: ignore[assignment]

    copied = dict(permissions)
    for behavior in ("allow", "deny", "ask"):
        raw_rules = copied.get(behavior)
        if not isinstance(raw_rules, list):
            continue
        filtered: list[str] = []
        for rule in raw_rules:
            if not isinstance(rule, str):
                errors.append(
                    SettingsError(
                        source=source,
                        path=path,
                        key_path=f"permissions.{behavior}",
                        message=f"Non-string value in {behavior} array was removed",
                        invalid_value=rule,
                    )
                )
                continue
            if parse_rule_value is not None:
                try:
                    parse_rule_value(rule)
                except ValueError as exc:
                    errors.append(
                        SettingsError(
                            source=source,
                            path=path,
                            key_path=f"permissions.{behavior}",
                            message=f'Invalid permission rule "{rule}" was skipped: {exc}',
                            invalid_value=rule,
                        )
                    )
                    continue
            filtered.append(rule)
        copied[behavior] = filtered
    data["permissions"] = copied


def _validate_known_schema(
    data: Mapping[str, Any],
    source: SettingSource,
    path: str | None,
    *,
    strict: bool,
) -> list[SettingsError]:
    errors: list[SettingsError] = []
    known_top = set(_default_raw_settings()) | {"$schema"}
    if strict:
        for key in data:
            if key not in known_top:
                errors.append(
                    SettingsError(
                        source=source,
                        path=path,
                        key_path=key,
                        message=f"Unrecognized field: {key}",
                        invalid_value=data.get(key),
                    )
                )

    def err(key_path: str, message: str, value: Any) -> None:
        errors.append(SettingsError(source, path, key_path, message, value))

    _expect_enum(data, "theme", THEME_OPTIONS, err)
    _expect_enum(data, "editorMode", EDITOR_MODE_OPTIONS, err)
    _expect_bool(data, "verbose", err)
    _expect_enum(data, "preferredNotifChannel", NOTIFICATION_CHANNEL_OPTIONS, err)
    _expect_bool(data, "autoCompactEnabled", err)
    _expect_bool(data, "fileCheckpointingEnabled", err)
    _expect_bool(data, "showTurnDuration", err)
    _expect_bool(data, "terminalProgressBarEnabled", err)
    _expect_bool(data, "todoFeatureEnabled", err)
    _expect_enum(data, "teammateMode", TEAMMATE_MODE_OPTIONS, err)
    _expect_optional_string(data, "model", err)
    _expect_optional_bool(data, "alwaysThinkingEnabled", err)
    _expect_optional_string(data, "language", err)
    _expect_bool(data, "autoMemoryEnabled", err)
    _expect_optional_string(data, "autoMemoryDirectory", err)
    _expect_optional_string(data, "outputStyle", err)
    _expect_optional_string(data, "$schema", err)

    permissions = data.get("permissions")
    if permissions is not None:
        if not isinstance(permissions, Mapping):
            err("permissions", "Expected object", permissions)
        else:
            for behavior in ("allow", "deny", "ask"):
                _expect_string_list(permissions, behavior, err, f"permissions.{behavior}")
            _expect_enum(
                permissions,
                "defaultMode",
                PERMISSION_MODE_OPTIONS,
                err,
                "permissions.defaultMode",
            )
            _expect_string_list(
                permissions,
                "additionalDirectories",
                err,
                "permissions.additionalDirectories",
            )

    hooks = data.get("hooks")
    if hooks is not None:
        _validate_hooks_schema(hooks, err)

    auto_dream = data.get("autoDream")
    if auto_dream is not None:
        if not isinstance(auto_dream, Mapping):
            err("autoDream", "Expected object", auto_dream)
        else:
            _expect_bool(auto_dream, "enabled", err, "autoDream.enabled")
            _expect_positive_number(auto_dream, "minHours", err, "autoDream.minHours")
            _expect_positive_int(auto_dream, "minSessions", err, "autoDream.minSessions")
            _expect_positive_number(
                auto_dream,
                "scanIntervalSeconds",
                err,
                "autoDream.scanIntervalSeconds",
            )
            _expect_optional_string(auto_dream, "model", err, "autoDream.model")

    memory = data.get("memory")
    if memory is not None:
        if not isinstance(memory, Mapping):
            err("memory", "Expected object", memory)
        else:
            _expect_enum(memory, "mode", MEMORY_MODE_OPTIONS, err, "memory.mode")
            daily = memory.get("dailyLog")
            if daily is not None:
                if not isinstance(daily, Mapping):
                    err("memory.dailyLog", "Expected object", daily)
                else:
                    _expect_positive_int(
                        daily,
                        "retentionDays",
                        err,
                        "memory.dailyLog.retentionDays",
                    )
                    _expect_bool(
                        daily,
                        "consolidateOnDream",
                        err,
                        "memory.dailyLog.consolidateOnDream",
                    )

    attachments = data.get("attachments")
    if attachments is not None:
        if not isinstance(attachments, Mapping):
            err("attachments", "Expected object", attachments)
        else:
            if strict:
                for key in attachments:
                    if key != "todoReminderEnabled":
                        err(
                            f"attachments.{key}",
                            f"Unrecognized field: {key}",
                            attachments.get(key),
                        )
            _expect_bool(attachments, "todoReminderEnabled", err, "attachments.todoReminderEnabled")
    sandbox = data.get("sandbox")
    if sandbox is not None:
        _validate_sandbox_schema(sandbox, err)
    return errors


def _validate_sandbox_schema(
    sandbox: Any,
    err: Callable[[str, str, Any], None],
) -> None:
    if not isinstance(sandbox, Mapping):
        err("sandbox", "Expected object", sandbox)
        return
    _expect_bool(sandbox, "enabled", err, "sandbox.enabled")
    _expect_bool(sandbox, "failIfUnavailable", err, "sandbox.failIfUnavailable")
    if "enabledPlatforms" in sandbox:
        value = sandbox["enabledPlatforms"]
        if not isinstance(value, list) or any(
            item not in SANDBOX_PLATFORM_OPTIONS for item in value
        ):
            err(
                "sandbox.enabledPlatforms",
                "Expected array containing only: "
                + ", ".join(SANDBOX_PLATFORM_OPTIONS),
                value,
            )
    _expect_bool(
        sandbox,
        "autoAllowBashIfSandboxed",
        err,
        "sandbox.autoAllowBashIfSandboxed",
    )
    _expect_bool(
        sandbox,
        "allowUnsandboxedCommands",
        err,
        "sandbox.allowUnsandboxedCommands",
    )
    _expect_bool(
        sandbox,
        "enableWeakerNestedSandbox",
        err,
        "sandbox.enableWeakerNestedSandbox",
    )
    _expect_bool(
        sandbox,
        "enableWeakerNetworkIsolation",
        err,
        "sandbox.enableWeakerNetworkIsolation",
    )
    _expect_string_list(
        sandbox,
        "excludedCommands",
        err,
        "sandbox.excludedCommands",
    )
    ignore = sandbox.get("ignoreViolations")
    if ignore is not None:
        if not isinstance(ignore, Mapping):
            err("sandbox.ignoreViolations", "Expected object", ignore)
        else:
            for key, value in ignore.items():
                if not isinstance(key, str) or not isinstance(value, list) or any(
                    not isinstance(item, str) for item in value
                ):
                    err(
                        "sandbox.ignoreViolations",
                        "Expected object mapping strings to arrays of strings",
                        ignore,
                    )
                    break

    network = sandbox.get("network")
    if network is not None:
        if not isinstance(network, Mapping):
            err("sandbox.network", "Expected object", network)
        else:
            _expect_string_list(
                network,
                "allowedDomains",
                err,
                "sandbox.network.allowedDomains",
            )
            _expect_bool(
                network,
                "allowManagedDomainsOnly",
                err,
                "sandbox.network.allowManagedDomainsOnly",
            )
            _expect_string_list(
                network,
                "allowUnixSockets",
                err,
                "sandbox.network.allowUnixSockets",
            )
            _expect_bool(
                network,
                "allowAllUnixSockets",
                err,
                "sandbox.network.allowAllUnixSockets",
            )
            _expect_bool(
                network,
                "allowLocalBinding",
                err,
                "sandbox.network.allowLocalBinding",
            )
            _expect_non_negative_int(
                network,
                "httpProxyPort",
                err,
                "sandbox.network.httpProxyPort",
            )
            _expect_non_negative_int(
                network,
                "socksProxyPort",
                err,
                "sandbox.network.socksProxyPort",
            )

    filesystem = sandbox.get("filesystem")
    if filesystem is not None:
        if not isinstance(filesystem, Mapping):
            err("sandbox.filesystem", "Expected object", filesystem)
        else:
            for key in ("allowWrite", "denyWrite", "denyRead", "allowRead"):
                _expect_string_list(
                    filesystem,
                    key,
                    err,
                    f"sandbox.filesystem.{key}",
                )
            _expect_bool(
                filesystem,
                "allowManagedReadPathsOnly",
                err,
                "sandbox.filesystem.allowManagedReadPathsOnly",
            )

    ripgrep = sandbox.get("ripgrep")
    if ripgrep is not None:
        if not isinstance(ripgrep, Mapping):
            err("sandbox.ripgrep", "Expected object", ripgrep)
        else:
            _expect_optional_string(
                ripgrep,
                "command",
                err,
                "sandbox.ripgrep.command",
            )
            _expect_string_list(ripgrep, "args", err, "sandbox.ripgrep.args")


def _validate_hooks_schema(
    hooks: Any,
    err: Callable[[str, str, Any], None],
) -> None:
    if not isinstance(hooks, Mapping):
        err("hooks", "Expected object", hooks)
        return
    try:
        from openspace.services.tooling.hooks import is_hook_event
    except Exception:
        is_hook_event = lambda value: isinstance(  # type: ignore[assignment]
            value,
            str,
        )

    for event_name, matchers in hooks.items():
        event_path = f"hooks.{event_name}"
        if not isinstance(event_name, str) or not is_hook_event(event_name):
            err(event_path, f"Unsupported hook event: {event_name}", matchers)
            continue
        if not isinstance(matchers, list):
            err(event_path, "Expected array of matcher objects", matchers)
            continue
        for index, matcher in enumerate(matchers):
            matcher_path = f"{event_path}.{index}"
            if not isinstance(matcher, Mapping):
                err(matcher_path, "Expected matcher object", matcher)
                continue
            if "matcher" in matcher and not isinstance(
                matcher.get("matcher"),
                str,
            ):
                err(
                    f"{matcher_path}.matcher",
                    "Expected string",
                    matcher.get("matcher"),
                )
            hook_items = matcher.get("hooks")
            if not isinstance(hook_items, list):
                err(f"{matcher_path}.hooks", "Expected array of hooks", hook_items)
                continue
            for hook_index, hook in enumerate(hook_items):
                hook_path = f"{matcher_path}.hooks.{hook_index}"
                if not isinstance(hook, Mapping):
                    err(hook_path, "Expected hook object", hook)
                    continue
                hook_type = str(hook.get("type") or "command")
                if hook_type not in {"command", "prompt", "http", "agent"}:
                    err(
                        f"{hook_path}.type",
                        "Expected command, prompt, http, or agent",
                        hook.get("type"),
                    )
                if hook_type == "command" and not isinstance(hook.get("command"), str):
                    err(
                        f"{hook_path}.command",
                        "Expected string",
                        hook.get("command"),
                    )
                if hook_type in {"prompt", "agent"} and not isinstance(
                    hook.get("prompt"),
                    str,
                ):
                    err(f"{hook_path}.prompt", "Expected string", hook.get("prompt"))
                if hook_type == "http" and not isinstance(hook.get("url"), str):
                    err(f"{hook_path}.url", "Expected string", hook.get("url"))


def _settings_from_raw(
    raw: Mapping[str, Any],
    source: SettingSource,
    path: str | None,
) -> tuple[Settings, list[SettingsError]]:
    parse = _validate_settings_data(raw, source, path, strict=False)
    data = parse.settings or _default_raw_settings()
    permissions_raw = _as_mapping(data.get("permissions"))
    auto_dream_raw = _as_mapping(data.get("autoDream"))
    memory_raw = _as_mapping(data.get("memory"))
    daily_log_raw = _as_mapping(memory_raw.get("dailyLog"))
    attachments_raw = _as_mapping(data.get("attachments"))
    sandbox_raw = _as_mapping(data.get("sandbox"))
    sandbox_network_raw = _as_mapping(sandbox_raw.get("network"))
    sandbox_filesystem_raw = _as_mapping(sandbox_raw.get("filesystem"))
    sandbox_ripgrep_raw = _as_mapping(sandbox_raw.get("ripgrep"))

    auto_dream = AutoDreamSettings(
        enabled=bool(_value_or(auto_dream_raw.get("enabled"), False)),
        min_hours=float(_value_or(auto_dream_raw.get("minHours"), 24.0)),
        min_sessions=int(_value_or(auto_dream_raw.get("minSessions"), 5)),
        scan_interval_seconds=float(
            _value_or(auto_dream_raw.get("scanIntervalSeconds"), 600.0)
        ),
        model=_optional_str(auto_dream_raw.get("model")),
    )
    settings = Settings(
        ui=UISettings(
            theme=str(_value_or(data.get("theme"), "dark")),
            editor_mode=str(_value_or(data.get("editorMode"), "normal")),
            verbose=bool(_value_or(data.get("verbose"), False)),
            preferred_notif_channel=str(
                _value_or(data.get("preferredNotifChannel"), "auto")
            ),
            show_turn_duration=bool(_value_or(data.get("showTurnDuration"), True)),
            terminal_progress_bar_enabled=bool(
                _value_or(data.get("terminalProgressBarEnabled"), True)
            ),
        ),
        engine=EngineSettings(
            auto_compact_enabled=bool(
                _value_or(data.get("autoCompactEnabled"), True)
            ),
            file_checkpointing_enabled=bool(
                _value_or(data.get("fileCheckpointingEnabled"), True)
            ),
            todo_feature_enabled=bool(_value_or(data.get("todoFeatureEnabled"), True)),
            model=_optional_str(data.get("model")),
            always_thinking_enabled=_optional_bool(data.get("alwaysThinkingEnabled")),
            permissions=PermissionSettings(
                allow=_string_list(permissions_raw.get("allow")),
                deny=_string_list(permissions_raw.get("deny")),
                ask=_string_list(permissions_raw.get("ask")),
                default_mode=str(
                    _value_or(permissions_raw.get("defaultMode"), "default")
                ),
                additional_directories=_string_list(
                    permissions_raw.get("additionalDirectories")
                ),
            ),
            language=_optional_str(data.get("language")),
            auto_memory_enabled=bool(_value_or(data.get("autoMemoryEnabled"), True)),
            auto_memory_directory=_optional_str(data.get("autoMemoryDirectory")),
            auto_dream=auto_dream,
            memory=MemorySettings(
                mode=str(_value_or(memory_raw.get("mode"), "direct")),
                daily_log=DailyLogSettings(
                    retention_days=int(
                        _value_or(daily_log_raw.get("retentionDays"), 90)
                    ),
                    consolidate_on_dream=bool(
                        _value_or(daily_log_raw.get("consolidateOnDream"), True)
                    ),
                ),
            ),
            sandbox=SandboxSettings(
                enabled=bool(_value_or(sandbox_raw.get("enabled"), False)),
                fail_if_unavailable=bool(
                    _value_or(sandbox_raw.get("failIfUnavailable"), False)
                ),
                enabled_platforms=(
                    _string_list(sandbox_raw.get("enabledPlatforms"))
                    if isinstance(sandbox_raw.get("enabledPlatforms"), list)
                    else None
                ),
                auto_allow_bash_if_sandboxed=bool(
                    _value_or(sandbox_raw.get("autoAllowBashIfSandboxed"), True)
                ),
                allow_unsandboxed_commands=bool(
                    _value_or(sandbox_raw.get("allowUnsandboxedCommands"), True)
                ),
                network=SandboxNetworkSettings(
                    allowed_domains=_string_list(
                        sandbox_network_raw.get("allowedDomains")
                    ),
                    allow_managed_domains_only=bool(
                        _value_or(
                            sandbox_network_raw.get("allowManagedDomainsOnly"),
                            False,
                        )
                    ),
                    allow_unix_sockets=_string_list(
                        sandbox_network_raw.get("allowUnixSockets")
                    ),
                    allow_all_unix_sockets=bool(
                        _value_or(sandbox_network_raw.get("allowAllUnixSockets"), False)
                    ),
                    allow_local_binding=bool(
                        _value_or(sandbox_network_raw.get("allowLocalBinding"), False)
                    ),
                    http_proxy_port=_optional_int(
                        sandbox_network_raw.get("httpProxyPort")
                    ),
                    socks_proxy_port=_optional_int(
                        sandbox_network_raw.get("socksProxyPort")
                    ),
                ),
                filesystem=SandboxFilesystemSettings(
                    allow_write=_string_list(sandbox_filesystem_raw.get("allowWrite")),
                    deny_write=_string_list(sandbox_filesystem_raw.get("denyWrite")),
                    deny_read=_string_list(sandbox_filesystem_raw.get("denyRead")),
                    allow_read=_string_list(sandbox_filesystem_raw.get("allowRead")),
                    allow_managed_read_paths_only=bool(
                        _value_or(
                            sandbox_filesystem_raw.get("allowManagedReadPathsOnly"),
                            False,
                        )
                    ),
                ),
                ignore_violations=_ignore_violations(
                    sandbox_raw.get("ignoreViolations")
                ),
                enable_weaker_nested_sandbox=bool(
                    _value_or(sandbox_raw.get("enableWeakerNestedSandbox"), False)
                ),
                enable_weaker_network_isolation=bool(
                    _value_or(sandbox_raw.get("enableWeakerNetworkIsolation"), False)
                ),
                excluded_commands=_string_list(sandbox_raw.get("excludedCommands")),
                ripgrep_command=_optional_str(sandbox_ripgrep_raw.get("command")),
                ripgrep_args=_string_list(sandbox_ripgrep_raw.get("args")),
            ),
        ),
        experimental=ExperimentalSettings(
            teammate_mode=str(_value_or(data.get("teammateMode"), "auto")),
            output_style=str(_value_or(data.get("outputStyle"), "default")),
            attachments=AttachmentSettings(
                todo_reminder_enabled=bool(
                    _value_or(attachments_raw.get("todoReminderEnabled"), True)
                ),
            ),
        ),
        schema=_optional_str(data.get("$schema")),
    )
    return settings, parse.errors


def _default_raw_settings() -> dict[str, Any]:
    return {
        "theme": "dark",
        "editorMode": "normal",
        "verbose": False,
        "preferredNotifChannel": "auto",
        "autoCompactEnabled": True,
        "fileCheckpointingEnabled": True,
        "showTurnDuration": True,
        "terminalProgressBarEnabled": True,
        "todoFeatureEnabled": True,
        "teammateMode": "auto",
        "model": None,
        "alwaysThinkingEnabled": None,
        "permissions": {
            "allow": [],
            "deny": [],
            "ask": [],
            "defaultMode": "default",
            "additionalDirectories": [],
        },
        "hooks": {},
        "language": None,
        "autoMemoryEnabled": True,
        "autoMemoryDirectory": None,
        "autoDream": {
            "enabled": False,
            "minHours": 24.0,
            "minSessions": 5,
            "scanIntervalSeconds": 600.0,
            "model": None,
        },
        "memory": {
            "mode": "direct",
            "dailyLog": {
                "retentionDays": 90,
                "consolidateOnDream": True,
            },
        },
        "sandbox": {
            "enabled": False,
            "failIfUnavailable": False,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": True,
            "network": {
                "allowedDomains": [],
                "allowManagedDomainsOnly": False,
                "allowUnixSockets": [],
                "allowAllUnixSockets": False,
                "allowLocalBinding": False,
                "httpProxyPort": None,
                "socksProxyPort": None,
            },
            "filesystem": {
                "allowWrite": [],
                "denyWrite": [],
                "denyRead": [],
                "allowRead": [],
                "allowManagedReadPathsOnly": False,
            },
            "ignoreViolations": {},
            "enableWeakerNestedSandbox": False,
            "enableWeakerNetworkIsolation": False,
            "excludedCommands": [],
        },
        "outputStyle": "default",
        "attachments": {
            "todoReminderEnabled": True,
        },
    }


def _env_settings() -> dict[str, Any]:
    out: dict[str, Any] = {}
    model = os.environ.get("OPENSPACE_MODEL")
    if model:
        out["model"] = model
    theme = os.environ.get("OPENSPACE_THEME")
    if theme:
        out["theme"] = theme
    auto_compact = _env_bool("OPENSPACE_AUTO_COMPACT_ENABLED")
    if auto_compact is not None:
        out["autoCompactEnabled"] = auto_compact
    if _is_truthy(os.environ.get("DISABLE_COMPACT")) or _is_truthy(
        os.environ.get("DISABLE_AUTO_COMPACT")
    ):
        out["autoCompactEnabled"] = False

    auto_mem_disabled = os.environ.get("OPENSPACE_DISABLE_AUTO_MEMORY")
    if _is_truthy(auto_mem_disabled):
        out["autoMemoryEnabled"] = False
    elif _is_defined_falsy(auto_mem_disabled):
        out["autoMemoryEnabled"] = True
    if _is_truthy(os.environ.get("OPENSPACE_SIMPLE")):
        out["autoMemoryEnabled"] = False
    auto_mem_dir = os.environ.get("OPENSPACE_AUTO_MEMORY_DIRECTORY")
    if auto_mem_dir:
        out["autoMemoryDirectory"] = auto_mem_dir

    auto_dream = _env_bool("OPENSPACE_AUTO_DREAM_ENABLED")
    disabled_dream = os.environ.get("OPENSPACE_DISABLE_AUTO_DREAM")
    if _is_truthy(disabled_dream):
        auto_dream = False
    elif _is_defined_falsy(disabled_dream):
        auto_dream = True
    auto_dream_obj: dict[str, Any] = {}
    if auto_dream is not None:
        auto_dream_obj["enabled"] = auto_dream
    _env_number(
        "OPENSPACE_AUTO_DREAM_MIN_HOURS",
        auto_dream_obj,
        "minHours",
        int_only=False,
    )
    _env_number(
        "OPENSPACE_AUTO_DREAM_MIN_SESSIONS",
        auto_dream_obj,
        "minSessions",
        int_only=True,
    )
    _env_number(
        "OPENSPACE_AUTO_DREAM_SCAN_INTERVAL_SECONDS",
        auto_dream_obj,
        "scanIntervalSeconds",
        int_only=False,
    )
    dream_model = os.environ.get("OPENSPACE_MEMORY_DREAM_MODEL")
    if dream_model:
        auto_dream_obj["model"] = dream_model
    if auto_dream_obj:
        out["autoDream"] = auto_dream_obj

    memory_obj: dict[str, Any] = {}
    memory_mode = os.environ.get("OPENSPACE_MEMORY_MODE")
    if memory_mode:
        memory_obj["mode"] = _normalize_memory_mode(memory_mode)
    daily_obj: dict[str, Any] = {}
    _env_number(
        "OPENSPACE_MEMORY_DAILY_LOG_RETENTION_DAYS",
        daily_obj,
        "retentionDays",
        int_only=True,
    )
    daily_consolidate = _env_bool("OPENSPACE_MEMORY_DAILY_LOG_CONSOLIDATE_ON_DREAM")
    if daily_consolidate is not None:
        daily_obj["consolidateOnDream"] = daily_consolidate
    if daily_obj:
        memory_obj["dailyLog"] = daily_obj
    if memory_obj:
        out["memory"] = memory_obj

    if _is_truthy(os.environ.get("OPENSPACE_DISABLE_THINKING")):
        out["alwaysThinkingEnabled"] = False
    thinking = _env_bool("OPENSPACE_ALWAYS_THINKING_ENABLED")
    if thinking is not None:
        out["alwaysThinkingEnabled"] = thinking

    todo = _env_bool("OPENSPACE_TODO_FEATURE_ENABLED")
    if todo is not None:
        out["todoFeatureEnabled"] = todo
    sandbox_obj: dict[str, Any] = {}
    sandbox_enabled = _env_bool("OPENSPACE_SANDBOX_ENABLED")
    if sandbox_enabled is not None:
        sandbox_obj["enabled"] = sandbox_enabled
    fail_unavailable = _env_bool("OPENSPACE_SANDBOX_FAIL_IF_UNAVAILABLE")
    if fail_unavailable is not None:
        sandbox_obj["failIfUnavailable"] = fail_unavailable
    if sandbox_obj:
        out["sandbox"] = sandbox_obj
    return out


def _runtime_settings(runtime: RuntimeSettingsState) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if runtime.cli_model:
        out["model"] = runtime.cli_model
    if runtime.permission_mode:
        out["permissions"] = {"defaultMode": runtime.permission_mode}
    if runtime.always_thinking_enabled is not None:
        out["alwaysThinkingEnabled"] = runtime.always_thinking_enabled
    return out


def _coerce_runtime_state(
    runtime: RuntimeSettingsState | Mapping[str, Any] | None,
) -> RuntimeSettingsState:
    if runtime is None:
        return RuntimeSettingsState(
            remote_session=_is_truthy(os.environ.get("OPENSPACE_REMOTE")),
            remote_memory_dir=(
                Path(os.environ["OPENSPACE_REMOTE_MEMORY_DIR"]).expanduser().resolve()
                if os.environ.get("OPENSPACE_REMOTE_MEMORY_DIR")
                else None
            ),
        )
    if isinstance(runtime, RuntimeSettingsState):
        return runtime
    remote_memory_dir = runtime.get("remote_memory_dir")
    return RuntimeSettingsState(
        remote_session=bool(runtime.get("remote_session", False)),
        remote_memory_dir=(
            Path(remote_memory_dir).expanduser().resolve()
            if remote_memory_dir
            else None
        ),
        cli_model=_optional_str(runtime.get("cli_model")),
        permission_mode=_optional_str(runtime.get("permission_mode")),
        always_thinking_enabled=_optional_bool(runtime.get("always_thinking_enabled")),
    )


def _cache_key(
    cwd: str,
    project_root: str,
    runtime: RuntimeSettingsState,
) -> tuple[Any, ...]:
    file_state = tuple(
        (str(path), _file_mtime(path))
        for path in _watched_paths(cwd)
    )
    env_state = tuple(
        (key, os.environ.get(key))
        for key in (
            "OPENSPACE_CONFIG_HOME",
            "OPENSPACE_MODEL",
            "OPENSPACE_THEME",
            "DISABLE_COMPACT",
            "DISABLE_AUTO_COMPACT",
            "OPENSPACE_AUTO_COMPACT_ENABLED",
            "OPENSPACE_DISABLE_AUTO_MEMORY",
            "OPENSPACE_SIMPLE",
            "OPENSPACE_AUTO_MEMORY_DIRECTORY",
            "OPENSPACE_AUTO_DREAM_ENABLED",
            "OPENSPACE_DISABLE_AUTO_DREAM",
            "OPENSPACE_AUTO_DREAM_MIN_HOURS",
            "OPENSPACE_AUTO_DREAM_MIN_SESSIONS",
            "OPENSPACE_AUTO_DREAM_SCAN_INTERVAL_SECONDS",
            "OPENSPACE_MEMORY_DREAM_MODEL",
            "OPENSPACE_MEMORY_MODE",
            "OPENSPACE_MEMORY_DAILY_LOG_RETENTION_DAYS",
            "OPENSPACE_MEMORY_DAILY_LOG_CONSOLIDATE_ON_DREAM",
            "OPENSPACE_DISABLE_THINKING",
            "OPENSPACE_ALWAYS_THINKING_ENABLED",
            "OPENSPACE_TODO_FEATURE_ENABLED",
            "OPENSPACE_SANDBOX_ENABLED",
            "OPENSPACE_SANDBOX_FAIL_IF_UNAVAILABLE",
        )
    )
    runtime_state = (
        runtime.remote_session,
        str(runtime.remote_memory_dir) if runtime.remote_memory_dir else None,
        runtime.cli_model,
        runtime.permission_mode,
        runtime.always_thinking_enabled,
    )
    return (cwd, project_root, file_state, env_state, runtime_state)


def _atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".settings-",
        suffix=".json.tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(data), handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _ensure_local_settings_gitignored(path: Path) -> None:
    gitignore = path.parent.parent / ".gitignore"
    rel = ".openspace/settings.local.json"
    try:
        existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
        if rel not in {line.strip() for line in existing.splitlines()}:
            with gitignore.open("a", encoding="utf-8") as handle:
                if existing and not existing.endswith("\n"):
                    handle.write("\n")
                handle.write(rel + "\n")
    except OSError:
        logger.debug("Failed to update %s", gitignore, exc_info=True)


def _merge_settings(base: Mapping[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in update.items():
        old_value = result.get(key)
        if isinstance(old_value, Mapping) and isinstance(value, Mapping):
            result[key] = _merge_settings(old_value, value)
        elif isinstance(old_value, list) and isinstance(value, list):
            result[key] = _dedupe([*old_value, *value])
        else:
            result[key] = value
    return result


def _merge_for_update(base: Mapping[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in update.items():
        if value is None:
            result.pop(key, None)
            continue
        old_value = result.get(key)
        if isinstance(old_value, Mapping) and isinstance(value, Mapping):
            nested = _merge_for_update(old_value, value)
            if nested:
                result[key] = nested
            else:
                result.pop(key, None)
        else:
            result[key] = value
    return result


def _build_path_update(key: str, value: Any) -> dict[str, Any]:
    parts = [part for part in key.split(".") if part]
    if not parts:
        raise ValueError("setting key must not be empty")
    current: dict[str, Any] = {parts[-1]: value}
    for part in reversed(parts[:-1]):
        current = {part: current}
    return current


def _get_path(data: Mapping[str, Any], key: str, default: Any = None) -> Any:
    current: Any = data
    for part in key.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            return default
    return current


def _dedupe(values: Sequence[Any]) -> list[Any]:
    result: list[Any] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _string_list(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _ignore_violations(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, list[str]] = {}
    for key, entries in value.items():
        if isinstance(key, str):
            out[key] = _string_list(entries)
    return out


def _value_or(value: Any, default: Any) -> Any:
    return default if value is None else value


def _normalize_memory_mode(value: str) -> str:
    raw = value.strip().lower()
    if raw in {"daily-log", "dailylog", "logs"}:
        return "daily_log"
    return raw


def _is_truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


def _is_defined_falsy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"0", "false", "no", "off", ""}


def _env_bool(name: str) -> bool | None:
    value = os.environ.get(name)
    if _is_truthy(value):
        return True
    if _is_defined_falsy(value):
        return False
    return None


def _env_number(name: str, target: dict[str, Any], key: str, *, int_only: bool) -> None:
    raw = os.environ.get(name)
    if not raw:
        return
    try:
        parsed: Any = int(raw) if int_only else float(raw)
    except ValueError:
        return
    if parsed > 0:
        target[key] = parsed


def _expect_bool(
    data: Mapping[str, Any],
    key: str,
    err: Callable[[str, str, Any], None],
    key_path: str | None = None,
) -> None:
    if key in data and not isinstance(data[key], bool):
        err(key_path or key, "Expected boolean", data[key])


def _expect_optional_bool(
    data: Mapping[str, Any],
    key: str,
    err: Callable[[str, str, Any], None],
    key_path: str | None = None,
) -> None:
    if key in data and data[key] is not None and not isinstance(data[key], bool):
        err(key_path or key, "Expected boolean or null", data[key])


def _expect_optional_string(
    data: Mapping[str, Any],
    key: str,
    err: Callable[[str, str, Any], None],
    key_path: str | None = None,
) -> None:
    if key in data and data[key] is not None and not isinstance(data[key], str):
        err(key_path or key, "Expected string or null", data[key])


def _expect_enum(
    data: Mapping[str, Any],
    key: str,
    options: Sequence[str],
    err: Callable[[str, str, Any], None],
    key_path: str | None = None,
) -> None:
    if key in data and data[key] not in options:
        err(key_path or key, f"Invalid value. Expected one of: {', '.join(options)}", data[key])


def _expect_string_list(
    data: Mapping[str, Any],
    key: str,
    err: Callable[[str, str, Any], None],
    key_path: str | None = None,
) -> None:
    if key not in data:
        return
    value = data[key]
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        err(key_path or key, "Expected array of strings", value)


def _expect_positive_number(
    data: Mapping[str, Any],
    key: str,
    err: Callable[[str, str, Any], None],
    key_path: str | None = None,
) -> None:
    if key not in data:
        return
    value = data[key]
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        err(key_path or key, "Expected positive number", value)


def _expect_positive_int(
    data: Mapping[str, Any],
    key: str,
    err: Callable[[str, str, Any], None],
    key_path: str | None = None,
) -> None:
    if key not in data:
        return
    value = data[key]
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        err(key_path or key, "Expected positive integer", value)


def _expect_non_negative_int(
    data: Mapping[str, Any],
    key: str,
    err: Callable[[str, str, Any], None],
    key_path: str | None = None,
) -> None:
    if key not in data or data[key] is None:
        return
    value = data[key]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        err(key_path or key, "Expected non-negative integer or null", value)


__all__ = [
    "AttachmentSettings",
    "AutoDreamSettings",
    "DailyLogSettings",
    "EditableSettingSource",
    "EngineSettings",
    "ExperimentalSettings",
    "MemorySettings",
    "PermissionSettings",
    "RuntimeSettingsState",
    "SandboxFilesystemSettings",
    "SandboxNetworkSettings",
    "SandboxSettings",
    "SettingSource",
    "Settings",
    "SettingsError",
    "SettingsSnapshot",
    "SettingsSourceSnapshot",
    "UISettings",
    "get_effective_settings",
    "get_openspace_config_home_dir",
    "get_project_root",
    "get_setting",
    "get_settings",
    "get_settings_for_source",
    "get_settings_path_for_source",
    "get_settings_with_errors",
    "is_settings_file",
    "notify_settings_changed",
    "reset_settings_cache",
    "save_settings_for_source",
    "start_settings_watcher",
    "stop_settings_watcher",
    "subscribe_settings",
    "update_setting",
    "update_settings_for_source",
    "validate_settings_edit",
]
