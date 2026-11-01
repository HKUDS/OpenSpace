"""Host skill installation contracts, also loaded by the Python 3.9 bootstrap.

Keep this module standard-library-only and independent of the runtime package.
These directory contracts do not imply a verified model session in each host.
AutoSwarm's setup.py is the integration reference; documentation links below
identify each host's own skill discovery contract.
"""

import json
import os
from pathlib import Path


AUTOSWARM_REFERENCE = "https://github.com/HKUDS/AutoSwarm/blob/main/src/autoswarm/setup.py"
HOSTS = {
    "codex": {
        "name": "Codex", "default_dir": ".agents/skills", "invocation": "${name}",
        "documentation": "https://learn.chatgpt.com/docs/build-skills",
        "reload_note": "Select the skill with /skills or $name in Codex CLI. If it is not visible, start a new chat or restart Codex.",
    },
    "claude-code": {
        "name": "Claude Code", "default_dir": ".claude/skills", "invocation": "/{name}",
        "documentation": "https://code.claude.com/docs/en/skills",
        "reload_note": "Invoke /name in Claude Code. If the new skills directory is not discovered, run /reload-skills or start a new session.",
    },
    "opencode": {
        "name": "OpenCode", "default_dir": ".config/opencode/skills", "invocation": "/{name}",
        "root_env": "XDG_CONFIG_HOME", "env_suffix": "opencode/skills",
        "documentation": "https://docs.opencode.ai/docs/skills/",
        "reload_note": "Invoke /name in current OpenCode CLI (verified with 1.19.5), or ask it to load the named skill. V2 also supports @name. Restart the session if the new skill is not discovered.",
    },
    "cursor": {
        "name": "Cursor", "default_dir": ".cursor/skills", "invocation": "/{name}",
        "documentation": "https://cursor.com/help/customization/skills",
        "reload_note": "Invoke /name in Cursor chat or select the skill with @. Confirm the skill is present in Customize > Skills.",
    },
    "copilot": {
        "name": "GitHub Copilot CLI", "default_dir": ".copilot/skills", "invocation": "/{name}",
        "documentation": "https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-skills",
        "reload_note": "Start a Copilot CLI session or run /skills reload, then invoke /name.",
    },
    "muse": {
        "name": "Meta Muse Code", "default_dir": ".agents/skills", "invocation": "/{name}",
        "documentation": "https://dev.meta.ai/docs/muse-code/extending",
        "reload_note": "Check discovery with muse skills list, then invoke the skill's slash shortcut in Muse Code. This integration is for Meta Muse Code, not muse.ai.",
    },
    "codebuddy": {
        "name": "CodeBuddy Code", "default_dir": ".codebuddy/skills", "invocation": "/{name}",
        "documentation": "https://www.codebuddy.ai/docs/cli/skills",
        "reload_note": "Invoke /name in CodeBuddy Code after skill discovery. CodeBuddy and WorkBuddy have separate installation paths.",
    },
    "hermes": {
        "name": "Hermes Agent", "default_dir": ".hermes/skills", "invocation": "/{name}",
        "root_env": "HERMES_HOME", "env_suffix": "skills",
        "documentation": "https://hermes-agent.nousresearch.com/docs/user-guide/features/skills",
        "reload_note": "Invoke /name in Hermes; use /reload-skills if it is not visible. Use the active HERMES_HOME or an explicit --skill-dir for a custom profile.",
    },
    "nanobot": {
        "name": "nanobot", "default_dir": ".nanobot/workspace/skills", "invocation": "${name}",
        "documentation": "https://github.com/HKUDS/nanobot/blob/main/nanobot/agent/skills.py",
        "reload_note": "Invoke $name in nanobot to load the skill from its workspace. Default setup reads ~/.nanobot/config.json; for an instance selected with --home or --config, pass that instance's workspace/skills as --skill-dir.",
    },
    "dsh": {
        "name": "DeepSeek Harness", "default_dir": ".agents/skills", "invocation": "/{name}",
        "root_env": "DSH_AGENTS_HOME", "env_suffix": "skills",
        "documentation": "https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/skill/skill-filesystem/README.md",
        "reload_note": "Invoke /name in DSH after discovery. Its skill provider and consumer must be enabled and include the installed skill directory.",
    },
    "grok": {
        "name": "Grok Build", "default_dir": ".grok/skills", "invocation": "/{name}",
        "documentation": "https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/08-skills.md",
        "reload_note": "Start a Grok Build session and invoke /name. This installs a local Grok Build skill, not a Grok Bot cloud integration.",
    },
    "workbuddy": {
        "name": "WorkBuddy", "default_dir": None,
        "documentation": "https://open.workbuddy.cn/docs/skill",
        "reload_note": "Import the skill package or select the installed skill in WorkBuddy's skill picker. Verify it can run local commands in the project.",
    },
    "generic": {
        "name": "Other local skill host", "default_dir": None,
        "documentation": "https://agentskills.io/specification",
        "reload_note": "Install into the host's documented skills directory with --skill-dir, then load the named skill. Verify the host can read and edit project files and run local commands; installation alone does not establish compatibility.",
    },
}
SUPPORTED_HOSTS = tuple(HOSTS)


def host_details(host, components):
    if host not in HOSTS:
        raise ValueError("Unsupported host {!r}; choose {}".format(host, ", ".join(SUPPORTED_HOSTS)))
    details = HOSTS[host]
    invocation = details.get("invocation", "Use the {name} skill for this task.")
    return {
        "host_name": details["name"],
        "invocations": {name: invocation.format(name=name) for name in components},
        "reload_note": details["reload_note"],
        "documentation": details["documentation"],
    }


def _expand_user(value, home):
    value = str(value)
    if value == "~":
        return home
    if value.startswith("~/") or value.startswith("~\\"):
        return home / value[2:]
    return Path(value).expanduser()


def _nanobot_workspace(home, environment):
    config_path = home / ".nanobot/config.json"
    workspace = str(home / ".nanobot/workspace")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        agents = config.get("agents", {})
        configured_defaults = agents.get("defaults", {})
        # nanobot Config.settings_customise_sources prioritizes values loaded
        # from config.json over environment settings. The native SettingsConfig
        # uses case-insensitive NANOBOT_ keys with a double-underscore delimiter.
        environment = {key.upper(): value for key, value in environment.items()}
        defaults = {}
        if "NANOBOT_AGENTS" in environment:
            overrides = json.loads(environment["NANOBOT_AGENTS"])
            defaults = {**defaults, **overrides.get("defaults", {})}
        if "NANOBOT_AGENTS__DEFAULTS" in environment:
            defaults = {**defaults, **json.loads(environment["NANOBOT_AGENTS__DEFAULTS"])}
        workspace = defaults.get("workspace", workspace)
        workspace = environment.get("NANOBOT_AGENTS__DEFAULTS__WORKSPACE", workspace)
        workspace = configured_defaults.get("workspace", workspace)
        if not isinstance(workspace, str) or not workspace.strip():
            raise ValueError("invalid workspace")
    except (OSError, ValueError, TypeError, AttributeError) as error:
        raise ValueError("Cannot resolve nanobot's configured workspace; pass its workspace/skills directory with --skill-dir") from error
    return _expand_user(workspace, home)


def default_skill_dir(host, *, home=None, environ=None):
    """Return the host's skills parent directory without writing its settings."""
    host_details(host, ())  # Validate before interpreting any paths.
    home = Path(home).expanduser() if home is not None else Path.home()
    environment = os.environ if environ is None else environ
    details = HOSTS[host]
    if details["default_dir"] is None:
        return None
    if host == "nanobot":
        return _nanobot_workspace(home, environment) / "skills"
    root_variable = details.get("root_env")
    if root_variable and environment.get(root_variable):
        return _expand_user(environment[root_variable], home) / details["env_suffix"]
    return home / details["default_dir"]
