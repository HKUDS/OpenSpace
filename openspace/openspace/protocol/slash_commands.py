from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

_SCHEMA_PATH = Path(__file__).with_name("schema") / "slash_commands.json"


@dataclass(frozen=True, slots=True)
class SlashCommandArg:
    name: str
    required: bool
    description: str


@dataclass(frozen=True, slots=True)
class CoreSlashCommand:
    name: str
    summary: str
    usage: str
    category: str
    aliases: tuple[str, ...] = ()
    args: tuple[SlashCommandArg, ...] = ()
    tui_visible: bool = True


@lru_cache(maxsize=1)
def load_slash_command_manifest() -> dict[str, Any]:
    manifest = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    if manifest.get("manifest_schema_version") != 1:
        raise ValueError("slash_commands.json manifest_schema_version must be 1")
    commands = manifest.get("commands")
    if not isinstance(commands, list):
        raise ValueError("slash_commands.json commands must be a list")
    names: set[str] = set()
    for raw in commands:
        if not isinstance(raw, dict):
            raise ValueError("slash command entries must be objects")
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("slash command entries must include a non-empty name")
        if name in names:
            raise ValueError(f"duplicate slash command {name!r}")
        names.add(name)
        if raw.get("handler") not in {"local", "core"}:
            raise ValueError(f"slash command {name!r} handler must be local or core")
        if not isinstance(raw.get("category"), str) or not raw["category"]:
            raise ValueError(f"slash command {name!r} category must be a non-empty string")
        aliases = raw.get("aliases", [])
        if not isinstance(aliases, list) or not all(isinstance(alias, str) for alias in aliases):
            raise ValueError(f"slash command {name!r} aliases must be a list of strings")
        args = raw.get("args", [])
        if not isinstance(args, list):
            raise ValueError(f"slash command {name!r} args must be a list")
        for arg in args:
            if not isinstance(arg, dict):
                raise ValueError(f"slash command {name!r} args must be objects")
            if not isinstance(arg.get("name"), str) or not arg["name"]:
                raise ValueError(f"slash command {name!r} arg names must be non-empty strings")
            if not isinstance(arg.get("required"), bool):
                raise ValueError(f"slash command {name!r} arg required must be boolean")
            if not isinstance(arg.get("description"), str):
                raise ValueError(f"slash command {name!r} arg description must be string")
    return manifest


def get_slash_command_categories() -> dict[str, str]:
    categories = load_slash_command_manifest().get("categories", {})
    if not isinstance(categories, dict):
        raise ValueError("slash_commands.json categories must be an object")
    return {str(name): str(label) for name, label in categories.items()}


def get_slash_commands() -> tuple[dict[str, Any], ...]:
    return tuple(dict(command) for command in load_slash_command_manifest()["commands"])


def get_core_slash_commands() -> tuple[CoreSlashCommand, ...]:
    parsed: list[CoreSlashCommand] = []
    for raw in load_slash_command_manifest()["commands"]:
        if raw["handler"] != "core":
            continue
        aliases = raw.get("aliases", [])
        args = raw.get("args", [])
        parsed.append(
            CoreSlashCommand(
                name=str(raw["name"]),
                summary=str(raw["summary"]),
                usage=str(raw["usage"]),
                category=str(raw["category"]),
                aliases=tuple(str(alias) for alias in aliases),
                args=tuple(
                    SlashCommandArg(
                        name=str(arg["name"]),
                        required=bool(arg["required"]),
                        description=str(arg["description"]),
                    )
                    for arg in args
                ),
                tui_visible=bool(raw.get("tui_visible", True)),
            )
        )
    return tuple(parsed)
