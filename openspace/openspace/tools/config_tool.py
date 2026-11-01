"""ConfigTool.

Reading a setting is auto-allowed, setting a value asks the user, keys are
constrained to a supported whitelist, and setting-source values are read from
the effective settings merge while writes land in user settings.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping, Sequence

from openspace.grounding.core.permissions.types import PermissionAllow, PermissionAsk
from openspace.grounding.core.tool.base import BaseTool
from openspace.grounding.core.types import BackendType, ToolResult, ToolSchema, ToolStatus
from openspace.services.runtime_support.settings import (
    get_effective_settings,
    get_setting,
    get_settings_for_source,
    get_settings_path_for_source,
    update_setting,
)


CONFIG_TOOL_NAME = "config"
CONFIG_TOOL_ALIAS = "Config"
CONFIG_MAX_RESULT_SIZE_CHARS = 100_000

SettingSource = Literal["global", "settings"]
SettingType = Literal["boolean", "string"]
SettingGroup = Literal["stable_engine", "stable_ui", "experimental"]
_NO_DEFAULT = object()

EDITOR_MODES = ("normal", "vim")
NOTIFICATION_CHANNELS = (
    "auto",
    "iterm2",
    "iterm2_with_bell",
    "terminal_bell",
    "kitty",
    "ghostty",
    "notifications_disabled",
)
TEAMMATE_MODES = ("auto", "tmux", "in-process")
OS_THEME_OPTIONS = ("dark", "light")
PERMISSION_DEFAULT_MODE_OPTIONS = ("default", "plan", "acceptEdits", "dontAsk")

DESCRIPTION = "Get or set OpenSpace configuration settings."


@dataclass(frozen=True, slots=True)
class SettingConfig:
    source: SettingSource
    group: SettingGroup
    type: SettingType
    description: str
    path: tuple[str, ...] | None = None
    options: tuple[str, ...] | None = None
    get_options: Callable[[], Sequence[str]] | None = None
    app_state_key: str | None = None
    validate_on_write: Callable[[Any], tuple[bool, str | None]] | None = None
    format_on_read: Callable[[Any], Any] | None = None
    default: Any = _NO_DEFAULT


def _validate_non_empty_string(value: Any, setting: str) -> tuple[bool, str | None]:
    if not isinstance(value, str) or not value.strip():
        return False, f"{setting} requires a non-empty string."
    return True, None


def _validate_model(value: Any) -> tuple[bool, str | None]:
    return _validate_non_empty_string(value, "model")


SUPPORTED_SETTINGS: dict[str, SettingConfig] = {
    "theme": SettingConfig(
        source="global",
        group="stable_ui",
        type="string",
        description="Color theme for the UI",
        options=OS_THEME_OPTIONS,
        default="dark",
    ),
    "editorMode": SettingConfig(
        source="global",
        group="stable_ui",
        type="string",
        description="Key binding mode",
        options=EDITOR_MODES,
        default="normal",
    ),
    "verbose": SettingConfig(
        source="global",
        group="stable_ui",
        type="boolean",
        description="Show detailed debug output",
        app_state_key="verbose",
        default=False,
    ),
    "preferredNotifChannel": SettingConfig(
        source="global",
        group="stable_ui",
        type="string",
        description="Preferred notification channel",
        options=NOTIFICATION_CHANNELS,
        default="auto",
    ),
    "autoCompactEnabled": SettingConfig(
        source="global",
        group="stable_engine",
        type="boolean",
        description="Auto-compact when context is full",
        default=True,
    ),
    "autoMemoryEnabled": SettingConfig(
        source="settings",
        group="stable_engine",
        type="boolean",
        description="Enable auto-memory",
    ),
    "autoDream.enabled": SettingConfig(
        source="settings",
        group="stable_engine",
        type="boolean",
        description="Enable background memory consolidation",
    ),
    "fileCheckpointingEnabled": SettingConfig(
        source="global",
        group="stable_engine",
        type="boolean",
        description="Enable file checkpointing for code rewind",
        default=True,
    ),
    "showTurnDuration": SettingConfig(
        source="global",
        group="stable_ui",
        type="boolean",
        description="Show turn duration message after responses",
        default=True,
    ),
    "terminalProgressBarEnabled": SettingConfig(
        source="global",
        group="stable_ui",
        type="boolean",
        description="Show OSC 9;4 progress indicator in supported terminals",
        default=True,
    ),
    "todoFeatureEnabled": SettingConfig(
        source="global",
        group="stable_engine",
        type="boolean",
        description="Enable todo/task tracking",
        default=True,
    ),
    "model": SettingConfig(
        source="settings",
        group="stable_engine",
        type="string",
        description="Override the default model",
        app_state_key="mainLoopModel",
        validate_on_write=_validate_model,
        format_on_read=lambda value: "default" if value is None else value,
    ),
    "alwaysThinkingEnabled": SettingConfig(
        source="settings",
        group="stable_engine",
        type="boolean",
        description="Enable extended thinking",
        app_state_key="thinkingEnabled",
    ),
    "permissions.defaultMode": SettingConfig(
        source="settings",
        group="stable_engine",
        type="string",
        description="Default permission mode for tool usage",
        options=PERMISSION_DEFAULT_MODE_OPTIONS,
    ),
    "sandbox.enabled": SettingConfig(
        source="settings",
        group="stable_engine",
        type="boolean",
        description="Enable the process sandbox for Bash commands",
    ),
    "sandbox.autoAllowBashIfSandboxed": SettingConfig(
        source="settings",
        group="stable_engine",
        type="boolean",
        description="Auto-allow Bash when the process sandbox is active",
    ),
    "sandbox.allowUnsandboxedCommands": SettingConfig(
        source="settings",
        group="stable_engine",
        type="boolean",
        description="Allow explicit unsandboxed Bash bypasses",
    ),
    "sandbox.failIfUnavailable": SettingConfig(
        source="settings",
        group="stable_engine",
        type="boolean",
        description="Fail commands when sandbox dependencies are unavailable",
    ),
    "language": SettingConfig(
        source="settings",
        group="stable_engine",
        type="string",
        description="Preferred language for OpenSpace responses and voice dictation",
    ),
    "teammateMode": SettingConfig(
        source="global",
        group="experimental",
        type="string",
        description="How to spawn teammates",
        options=TEAMMATE_MODES,
    ),
    "attachments.todoReminderEnabled": SettingConfig(
        source="settings",
        group="experimental",
        type="boolean",
        description="Enable the TodoWrite reminder attachment",
        default=True,
    ),
}

if os.environ.get("USER_TYPE") == "ant":
    SUPPORTED_SETTINGS["classifierPermissionsEnabled"] = SettingConfig(
        source="settings",
        group="experimental",
        type="boolean",
        description="Enable AI-based classification for Bash permission rules",
    )


def is_supported(key: str) -> bool:
    return key in SUPPORTED_SETTINGS


def get_config(key: str) -> SettingConfig | None:
    return SUPPORTED_SETTINGS.get(key)


def get_all_keys() -> list[str]:
    return list(SUPPORTED_SETTINGS)


def get_grouped_keys() -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {
        "stable_engine": [],
        "stable_ui": [],
        "experimental": [],
    }
    for key, config in SUPPORTED_SETTINGS.items():
        groups[config.group].append(key)
    return groups


def get_options_for_setting(key: str) -> list[str] | None:
    config = get_config(key)
    if config is None:
        return None
    if config.options is not None:
        return list(config.options)
    if config.get_options is not None:
        return [str(item) for item in config.get_options()]
    return None


def get_path(key: str) -> list[str]:
    config = get_config(key)
    if config is not None and config.path is not None:
        return list(config.path)
    return key.split(".")


def generate_prompt() -> str:
    grouped_lines: dict[str, list[str]] = {
        "stable_engine": [],
        "stable_ui": [],
        "experimental": [],
    }

    for key, config in SUPPORTED_SETTINGS.items():
        if key == "model":
            continue
        options = get_options_for_setting(key)
        line = f"- {key}"
        if options:
            line += ": " + ", ".join(_json_stringify(item) for item in options)
        elif config.type == "boolean":
            line += ": true/false"
        line += f" - {config.description}"
        grouped_lines[config.group].append(line)

    model_section = (
        "## Model\n"
        "- model - Override the default model. Use any non-empty OpenSpace/LiteLLM "
        'model ID such as "openrouter/anthropic/claude-sonnet-4.5", '
        '"openai/gpt-5", or another configured provider model.'
    )

    return f"""Get or set OpenSpace configuration settings.

View or change OpenSpace settings. Use when the user requests configuration changes, asks about current settings, or when adjusting a setting would benefit them.

## Usage
- **Get current value:** Omit the "value" parameter
- **Set new value:** Include the "value" parameter

## Stable engine settings
These affect model/runtime behavior and are safe to present as ordinary user configuration:
{chr(10).join(grouped_lines["stable_engine"])}

## Stable UI settings
These affect CLI/TUI presentation only:
{chr(10).join(grouped_lines["stable_ui"])}

## Experimental settings
These are implemented but tied to features that are still evolving:
{chr(10).join(grouped_lines["experimental"])}

{model_section}
## Examples
- Get theme: {{ "setting": "theme" }}
- Set dark theme: {{ "setting": "theme", "value": "dark" }}
- Enable vim mode: {{ "setting": "editorMode", "value": "vim" }}
- Enable verbose: {{ "setting": "verbose", "value": true }}
- Enable auto dream: {{ "setting": "autoDream.enabled", "value": true }}
- Change model: {{ "setting": "model", "value": "openrouter/anthropic/claude-sonnet-4.5" }}
- Change permission mode: {{ "setting": "permissions.defaultMode", "value": "plan" }}
"""


def make_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "setting": {
                "type": "string",
                "description": 'The setting key (e.g., "theme", "model", "permissions.defaultMode")',
            },
            "value": {
                "type": ["string", "boolean", "number"],
                "description": "The new value. Omit to get current value.",
            },
        },
        "required": ["setting"],
        "additionalProperties": False,
    }


def make_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "success": {"type": "boolean"},
            "operation": {"type": "string", "enum": ["get", "set"]},
            "setting": {"type": "string"},
            "value": {},
            "previousValue": {},
            "newValue": {},
            "error": {"type": "string"},
        },
        "required": ["success"],
    }


class ConfigTool(BaseTool):
    _name = CONFIG_TOOL_NAME
    _description = DESCRIPTION
    backend_type = BackendType.META
    aliases = [CONFIG_TOOL_ALIAS]
    should_defer = True
    search_hint = "get or set OpenSpace settings (model, permissions, UI)"
    max_result_size_chars = CONFIG_MAX_RESULT_SIZE_CHARS

    def __init__(self) -> None:
        self._current_context: Any | None = None
        super().__init__(
            schema=ToolSchema(
                name=self._name,
                description=DESCRIPTION,
                parameters=make_input_schema(),
                return_schema=make_output_schema(),
                backend_type=self.backend_type,
            )
        )

    def set_context(self, context: Any) -> None:
        self._current_context = context

    def is_enabled(self) -> bool:
        return not _is_env_truthy(os.environ.get("OPENSPACE_DISABLE_CONFIG_TOOL"))

    def get_prompt(self, context: Any = None) -> str:
        return generate_prompt()

    def user_facing_name(self) -> str:
        return "Config"

    def is_read_only(self, input: Mapping[str, Any] | None = None) -> bool:
        return not isinstance(input, Mapping) or "value" not in input

    def is_concurrency_safe(self, input: Mapping[str, Any] | None = None) -> bool:
        return True

    def to_auto_classifier_input(self, input_data: Mapping[str, Any]) -> str:
        setting = str(input_data.get("setting", ""))
        if "value" not in input_data:
            return setting
        return f"{setting} = {input_data.get('value')}"

    async def check_permissions(
        self,
        input: dict[str, Any],
        context: Any = None,
    ) -> PermissionAllow | PermissionAsk:
        if "value" not in input:
            return PermissionAllow(updated_input=dict(input))
        return PermissionAsk(
            message=f"Set {input.get('setting')} to {_json_stringify(input.get('value'))}",
            updated_input=dict(input),
        )

    async def _arun(self, setting: str, **kwargs: Any) -> ToolResult:
        has_value = "value" in kwargs
        value = kwargs.get("value")
        data = await _handle_config_call(setting, value, has_value, self._current_context)
        is_error = not bool(data.get("success"))
        return ToolResult(
            status=ToolStatus.ERROR if is_error else ToolStatus.SUCCESS,
            content=_format_model_visible_result(data),
            error=data.get("error") if is_error else None,
            metadata={
                "tool": self.name,
                "data": data,
            },
        )


async def _handle_config_call(
    setting: str,
    value: Any,
    has_value: bool,
    context: Any | None,
) -> dict[str, Any]:
    if not is_supported(setting):
        return {"success": False, "error": f'Unknown setting: "{setting}"'}

    config = get_config(setting)
    assert config is not None
    path = get_path(setting)
    cwd = _context_cwd(context)

    if not has_value:
        current_value = get_value(config, path, cwd)
        display_value = (
            config.format_on_read(current_value)
            if config.format_on_read is not None
            else current_value
        )
        return {
            "success": True,
            "operation": "get",
            "setting": setting,
            "value": display_value,
        }

    final_value = value
    if config.type == "boolean":
        if isinstance(value, str):
            lowered = value.lower().strip()
            if lowered == "true":
                final_value = True
            elif lowered == "false":
                final_value = False
        if not isinstance(final_value, bool):
            return {
                "success": False,
                "operation": "set",
                "setting": setting,
                "error": f"{setting} requires true or false.",
            }

    options = get_options_for_setting(setting)
    if options is not None and str(final_value) not in options:
        return {
            "success": False,
            "operation": "set",
            "setting": setting,
            "error": f'Invalid value "{value}". Options: {", ".join(options)}',
        }

    if config.validate_on_write is not None:
        valid, error = config.validate_on_write(final_value)
        if not valid:
            return {
                "success": False,
                "operation": "set",
                "setting": setting,
                "error": error or f"Invalid value for {setting}.",
            }

    previous_value = get_value(config, path, cwd)
    try:
        set_value(config, path, final_value, cwd)
        await _sync_runtime_setting(context, setting, final_value)
    except Exception as exc:
        return {
            "success": False,
            "operation": "set",
            "setting": setting,
            "error": str(exc),
        }

    return {
        "success": True,
        "operation": "set",
        "setting": setting,
        "previousValue": previous_value,
        "newValue": final_value,
    }


def get_value(config: SettingConfig, path: Sequence[str], cwd: str) -> Any:
    value = get_setting(".".join(path), _MISSING, cwd=cwd)
    if value is not _MISSING:
        return value
    if config.default is not _NO_DEFAULT:
        return config.default
    return None


def set_value(config: SettingConfig, path: Sequence[str], value: Any, cwd: str) -> None:
    del config
    update_setting(".".join(path), value, cwd=cwd, source="userSettings")


async def _sync_runtime_setting(context: Any | None, setting: str, value: Any) -> None:
    if context is None:
        return
    llm_client = getattr(context, "llm_client", None)
    if setting == "model" and llm_client is not None:
        llm_client.model = str(value)
        try:
            context.model = str(value)
        except Exception:
            pass
    elif setting == "alwaysThinkingEnabled" and llm_client is not None:
        llm_client.enable_thinking = bool(value)

    emit = getattr(context, "emit_event", None)
    if callable(emit):
        await emit("config_changed", {"setting": setting, "value": value})


def _format_model_visible_result(data: Mapping[str, Any]) -> str:
    if data.get("success"):
        if data.get("operation") == "get":
            return f"{data.get('setting')} = {_json_stringify(data.get('value'))}"
        return f"Set {data.get('setting')} to {_json_stringify(data.get('newValue'))}"
    return f"Error: {data.get('error')}"


def _context_cwd(context: Any | None) -> str:
    cwd = getattr(context, "cwd", None)
    if cwd:
        return str(cwd)
    return os.getcwd()


_MISSING = object()


def _get_nested(data: Mapping[str, Any], path: Sequence[str]) -> Any:
    current: Any = data
    for key in path:
        if isinstance(current, Mapping) and key in current:
            current = current[key]
        else:
            return _MISSING
    return current


def _json_stringify(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _is_env_truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


__all__ = [
    "CONFIG_TOOL_ALIAS",
    "CONFIG_MAX_RESULT_SIZE_CHARS",
    "CONFIG_TOOL_NAME",
    "DESCRIPTION",
    "OS_THEME_OPTIONS",
    "PERMISSION_DEFAULT_MODE_OPTIONS",
    "SUPPORTED_SETTINGS",
    "ConfigTool",
    "SettingConfig",
    "generate_prompt",
    "get_all_keys",
    "get_config",
    "get_effective_settings",
    "get_grouped_keys",
    "get_options_for_setting",
    "get_path",
    "get_settings_for_source",
    "get_settings_path_for_source",
    "get_value",
    "is_supported",
    "make_input_schema",
    "make_output_schema",
    "set_value",
]
