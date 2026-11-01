"""Fresh CLI conversation state without duplicating rotating login credentials.

This isolates automatic conversation/memory reuse, not filesystem access. Workers
still run with the configured host execution policy and receive explicit task data.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import shutil
import uuid


SESSION_CONTRACT = "agentpilot.worker-session.v1"
_profiles: ContextVar[list[Path] | None] = ContextVar("agentpilot_hermes_profiles", default=None)
CONTEXT_ENV = frozenset({
    "CODEX_THREAD_ID", "CODEX_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_RESUME",
    "CLAUDE_SESSION_ID", "CODEBUDDY_SESSION_ID", "CODEBUDDY_RESUME", "HERMES_SESSION_ID",
    "HERMES_PARENT_SESSION_ID", "HERMES_GATEWAY_SESSION_KEY", "HERMES_RESUME",
    "NANOBOT_SESSION_ID", "DSH_SESSION_ID",
})


def validate_session_command(backend: str, command: tuple[str, ...]) -> None:
    """A caller may choose a model/profile, but may not restore a conversation."""
    forbidden = {"--resume", "--continue", "--fork-session", "--session-id", "--session", "--resume-session", "--teleport", "--remote"}
    short = {
        "claude_code": {"-c", "-r"}, "codebuddy": {"-c", "-r"},
        "hermes": {"-c", "-r", "--in"}, "nanobot": {"-s"},
    }.get(backend, set())
    if backend != "command" and "--" in command[1:]:
        raise ValueError("worker command must not terminate option parsing before session isolation flags")
    # These Codex options consume an ordinary value, which can legitimately be
    # called 'resume' or 'fork'. Only positional subcommands restore a session.
    codex_values = {"-p", "--profile", "-m", "--model", "-c", "--config", "-C", "--cd",
                    "-s", "--sandbox", "--enable", "--disable", "--local-provider",
                    "--add-dir", "--thread-source", "--color", "--output-schema",
                    "-o", "--output-last-message"}
    index = 1
    while index < len(command):
        arg = command[index]
        flag = arg.split("=", 1)[0]
        # Custom commands may bind a fresh ID to these flags themselves.
        if backend == "command" and flag in {"--session", "--session-id"}:
            if arg == flag and index + 1 < len(command) and command[index + 1] == "{session_id}":
                index += 2
                continue
            raise ValueError("custom command session selectors must use {session_id}")
        if flag in forbidden or flag in short or (
            backend == "codex" and arg in {"resume", "fork"}
        ) or any(arg.startswith(s) and len(arg) > len(s) for s in short if len(s) == 2):
            raise ValueError(f"{backend} worker cannot restore or select an existing session ({flag})")
        if backend == "hermes" and (
            flag == "--profile" or arg == "-p" or arg.startswith("-p") and not arg.startswith("--")
        ):
            raise ValueError("set allowlisted HERMES_HOME to select Hermes login/model settings; --profile bypasses session isolation")
        index += 2 if backend == "codex" and arg in codex_values else 1


def write_private_json(path: Path, value) -> None:
    with open(path, "x", encoding="utf-8", opener=lambda p, f: os.open(p, f, 0o600)) as stream:
        json.dump(value, stream)


def session_values(private: Path) -> dict[str, str]:
    root = private / "session"
    root.mkdir(mode=0o700)
    return {"{session_id}": str(uuid.uuid4()), "{session_root}": str(root)}


def nanobot_config(env, private: Path, workspace: Path, provider, command):
    """Keep sessions outside the checkout and never use the login's workspace.

    Proposer workspaces are empty and disposable; execution workspaces contain
    only their explicit task checkout. Neither uses ~/.nanobot/workspace memory.
    """
    home = Path(env.get("HOME", str(Path.home()))) / ".nanobot"
    source = home / "config.json"
    for index, arg in enumerate(command[1:], 1):
        if arg == "--home" and index + 1 < len(command):
            source = Path(command[index + 1]).expanduser() / "config.json"
        elif arg.startswith("--home="):
            source = Path(arg.split("=", 1)[1]).expanduser() / "config.json"
    try:
        settings = json.loads(source.read_text())
    except FileNotFoundError:
        settings = {}
    if not isinstance(settings, dict):
        raise ValueError("nanobot config must be a JSON object")
    if provider:
        settings.setdefault("providers", {})["custom"] = {
            "apiKey": env[provider.api_key_env], "apiBase": provider.base_url,
        }
        settings.setdefault("modelPresets", {})["agentpilot_worker"] = {
            "provider": "custom", "model": provider.model,
        }
    defaults = settings.setdefault("agents", {}).setdefault("defaults", {})
    if provider:
        defaults.update(modelPreset="agentpilot_worker", provider="custom", model=provider.model)
    defaults["unifiedSession"] = False
    defaults.pop("unified_session", None)
    defaults["workspace"] = str(workspace)
    path = private / "nanobot.json"
    write_private_json(path, settings)
    return path


def hermes_home(env, workspace: Path, provider=None) -> Path:
    """Use a disposable profile with Hermes's native shared credential fallback.

    OAuth grants must stay in their original store: copying auth.json would fork
    refresh tokens. The global credential pool reads/writes the source root while
    state.db, memories and session files belong to this new profile only.
    """
    import yaml

    default = Path(env.get("HOME", str(Path.home()))) / (".hermes" + env.get("HERMES_DATA_DIR_SUFFIX", ""))
    source = Path(env.get("HERMES_HOME", str(default))).expanduser().resolve()
    if source.parent.name != "profiles":
        active = source / "active_profile"
        if active.is_file():
            name = active.read_text().strip()
            if name and name != "default":
                if Path(name).name != name or name in {".", ".."}:
                    raise ValueError("invalid Hermes active profile")
                source = source / "profiles" / name
                if not source.is_dir():
                    raise ValueError("Hermes active profile does not exist")
    # Named profiles can inherit the default root's credential pool themselves.
    credential_root = source
    if source.parent.name == "profiles" and not (source / "auth.json").exists():
        credential_root = source.parent.parent
    profiles = credential_root / "profiles"
    profiles.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = profiles / ("agentpilot-" + uuid.uuid4().hex)
    target.mkdir(mode=0o700)
    if (owned := _profiles.get()) is not None:
        owned.append(target)
    original = source / "config.yaml"
    try:
        settings = yaml.safe_load(original.read_text()) if original.is_file() else {}
    except yaml.YAMLError:
        # Parser errors may embed credential-bearing source lines.
        raise ValueError("Hermes config.yaml is not valid YAML") from None
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        raise ValueError("Hermes config must be a mapping")
    # Only model selection/generation and execution settings are inherited.
    keep = {"model", "providers", "custom_providers", "model_aliases", "fallback_model", "fallback_models", "reasoning", "agent", "compression", "terminal"}
    settings = {key: value for key, value in settings.items() if key in keep}
    settings["memory"] = {"enabled": False, "user_profile_enabled": False, "provider": "none"}
    settings.setdefault("terminal", {})["cwd"] = str(workspace)
    write_private_json(target / "config.yaml", settings)  # JSON is valid YAML.
    env["HERMES_HOME"] = str(target)
    # Force source to be treated as the custom credential root even when it is
    # itself a named profile under ~/.hermes. HOME and other CLI logins stay put.
    env["HERMES_DATA_DIR_SUFFIX"] = "-agentpilot-" + uuid.uuid4().hex
    # Hermes reloads .env with override=True. A symlink to the original file
    # could restore HERMES_HOME/session IDs, override an explicit provider key,
    # or let its normalizer rewrite the user's file. Project only safe values
    # into this private profile; rotating OAuth remains in the shared auth store.
    from dotenv import dotenv_values

    dotenv = source / ".env"
    values = dotenv_values(dotenv, interpolate=False, encoding="utf-8-sig") if dotenv.is_file() else {}
    protected = CONTEXT_ENV | {"HERMES_HOME", "HERMES_DATA_DIR_SUFFIX", "HERMES_PROFILE", "HERMES_PROFILE_NAME"}
    values = {key: value for key, value in values.items() if key not in protected and value is not None}
    if provider:
        values["OPENROUTER_API_KEY"] = env[provider.api_key_env]
    # Keep a user env file even when empty: Hermes then treats a project .env
    # as fill-only, so it cannot replace the isolated profile's environment.
    with open(target / ".env", "x", encoding="utf-8", opener=lambda p, f: os.open(p, f, 0o600)) as stream:
        for key, value in values.items():
            stream.write(f"{key}={json.dumps(value, ensure_ascii=False)}\n")
    return target


@contextmanager
def session_scope():
    owned: list[Path] = []
    token = _profiles.set(owned)
    try:
        yield
    finally:
        _profiles.reset(token)
        for target in owned:
            if target.exists():
                shutil.rmtree(target)


def dsh_patch(private: Path) -> Path:
    path = private / "dsh-session.json"
    write_private_json(path, [
        {"id": "session-persistence-jsonl", "config": {"root": str(private / "session" / "sessions")}},
        {"id": "session-query-sqlite", "config": {"path": ":memory:", "openAt": "never"}},
        {"id": "attachment-local", "config": {"dshHome": str(private / "session")}},
    ])
    return path
