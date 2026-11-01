"""Stop-hook and CLI-home configuration, byte-compatible with EdgeBench.

EdgeBench Base (``sforge/harness/agent/{claude_code,codex}.py``) installs an
unconditional block hook so the CLI cannot end its session voluntarily; the
outer loop then only ever sees abnormal exits (crash / context death), which
it resumes.  We reproduce the exact hook payloads in a run-private tool
directory and point metadata in the run-private CLI home
(``CLAUDE_CONFIG_DIR`` / ``CODEX_HOME``) at its guest path, so runs cannot
see each other's sessions or the operator's config.

The retained native-session setup was validated with
``@anthropic-ai/claude-code@2.1.159`` and ``@openai/codex@0.144.6`` — Codex
hooks are a new feature, so the installed CLI version must be recorded with
the run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

STOP_HOOK_REASON = "Do not stop. Continue working on the implementation."

_HOOK_SCRIPT = (
    "#!/bin/bash\n"
    "cat >/dev/null\n"
    f'echo \'{{"decision":"block","reason":"{STOP_HOOK_REASON}"}}\'\n'
)


def _install_stop_hook(path: Path) -> Path:
    """Install the immutable agent-facing hook payload at ``path``."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_HOOK_SCRIPT)
    path.chmod(0o755)
    return path


def install_claude_home(
    config_home: Path,
    *,
    hook_path: Optional[Path] = None,
    hook_command: Optional[str] = None,
    install_stop_hook: bool = True,
) -> Path:
    """Write hook + settings into a private ``CLAUDE_CONFIG_DIR``.

    ``hook_path`` is the host installation location.  ``hook_command`` may
    name the corresponding guest path when the home is mounted into an outer
    sandbox.  Keeping these two namespaces explicit prevents a host path from
    being baked into the candidate-visible settings.
    """

    config_home = Path(config_home)
    config_home.mkdir(parents=True, exist_ok=True)
    settings: dict[str, object] = {}
    if install_stop_hook:
        installed_hook = _install_stop_hook(hook_path or config_home / "stop-hook.sh")
        command = hook_command or str(installed_hook)
        settings["hooks"] = {
            "Stop": [{"hooks": [{"type": "command", "command": command}]}]
        }
    (config_home / "settings.json").write_text(json.dumps(settings, indent=2))
    return config_home


def install_codex_home(
    config_home: Path,
    *,
    hook_path: Optional[Path] = None,
    hook_command: Optional[str] = None,
    openrouter_model: Optional[str] = None,
    provider_model: Optional[str] = None,
    provider_name: Optional[str] = None,
    provider_base_url: Optional[str] = None,
    provider_env_key: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    secret_env_names: tuple[str, ...] = (),
    install_stop_hook: bool = True,
) -> Path:
    """Write hook metadata and config into a private ``CODEX_HOME``."""

    config_home = Path(config_home)
    config_home.mkdir(parents=True, exist_ok=True)
    if install_stop_hook:
        installed_hook = _install_stop_hook(hook_path or config_home / "stop-hook.sh")
        command = hook_command or str(installed_hook)
        hooks = {
            "hooks": {
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": command,
                                "timeout": 30,
                                "statusMessage": "Arena stop hook",
                            }
                        ]
                    }
                ]
            }
        }
        (config_home / "hooks.json").write_text(json.dumps(hooks, indent=2))
    config_path = config_home / "config.toml"
    sections: list[str] = []
    if openrouter_model and provider_model:
        raise ValueError("Codex provider model is ambiguous")
    if reasoning_effort is not None and (
        not isinstance(reasoning_effort, str) or not reasoning_effort.strip()
    ):
        raise ValueError("Codex reasoning effort must be a non-empty string")
    if openrouter_model:
        provider_model = openrouter_model
        provider_name = "OpenRouter"
        provider_base_url = "https://openrouter.ai/api/v1"
        provider_env_key = "OPENROUTER_API_KEY"
    if provider_model:
        if not provider_name or not provider_base_url or not provider_env_key:
            raise ValueError("Codex custom provider configuration is incomplete")
        provider_id = (
            "openrouter"
            if provider_name == "OpenRouter"
            and provider_base_url == "https://openrouter.ai/api/v1"
            and provider_env_key == "OPENROUTER_API_KEY"
            else "arena_endpoint"
        )
        # This is the run-private, user-level CODEX_HOME config.  Codex's
        # custom-provider settings are intentionally not written into the
        # candidate repository (project config ignores these keys anyway).
        sections.extend(
            [
                f"model_provider = {json.dumps(provider_id)}\n"
                f"model = {json.dumps(provider_model)}\n"
                + (
                    "model_reasoning_effort = " + json.dumps(reasoning_effort) + "\n"
                    if reasoning_effort is not None
                    else ""
                ),
                f"[model_providers.{provider_id}]\n"
                f"name = {json.dumps(provider_name)}\n"
                f"base_url = {json.dumps(provider_base_url)}\n"
                'wire_api = "responses"\n'
                f"env_key = {json.dumps(provider_env_key)}\n",
            ]
        )
    elif reasoning_effort is not None:
        raise ValueError("Codex reasoning effort requires a provider model")
    if secret_env_names:
        sections.append(
            "[shell_environment_policy]\n"
            f"exclude = {json.dumps(sorted(set(secret_env_names)))}\n"
        )
    sections.append(
        "[features]\n"
        "hooks = "
        + ("true" if install_stop_hook else "false")
        + "\nshell_snapshot = false\n"
    )
    config_path.write_text("\n".join(sections))
    return config_home
