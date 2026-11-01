from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

ToolsSpec = list[str] | Literal["*"]
SystemPromptFactory = Callable[..., str]


class AgentSource(str, Enum):
    """Source buckets for agent definition precedence.

    Implementation: ``SettingSource`` plus ``built-in`` and ``plugin`` in
    ``tools/AgentTool/loadAgentsDir.ts``.  OpenSpace exposes only source names
    it actually loads today; enterprise flag/policy sources are not modeled.
    """

    BUILT_IN = "built-in"
    PLUGIN = "plugin"
    USER_SETTINGS = "userSettings"
    PROJECT_SETTINGS = "projectSettings"
    LOCAL_SETTINGS = "localSettings"
    CUSTOM = "custom"


@dataclass(slots=True)
class AgentMcpServerSpec:
    """MCP server requirement or inline config for an agent.

    OpenSpace supports either a server-name string or an inline ``{name: config}``
    object.  OS stores both in one small dataclass; execution wiring is part of
    later multi-agent steps.
    """

    name: str | None = None
    config: dict[str, Any] | None = None


@dataclass(slots=True)
class AgentDefinition:
    """A selectable subagent type.

    Mirrors OpenSpace ``BaseAgentDefinition`` / ``BuiltInAgentDefinition`` /
    ``CustomAgentDefinition`` from ``tools/AgentTool/loadAgentsDir.ts`` while
    adding the OpenSpace-only fields needed by Grounding backend filtering.
    """

    agent_type: str
    when_to_use: str
    get_system_prompt: str | SystemPromptFactory
    source: AgentSource | str = AgentSource.BUILT_IN
    base_dir: str | None = None

    # Tool control.  OpenSpace uses ``undefined`` or ``["*"]`` for all tools; OS uses
    # the explicit "*" sentinel internally.
    tools: ToolsSpec = "*"
    disallowed_tools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    allowed_agent_types: list[str] | None = None

    # MCP/hooks/settings fields parsed now, consumed by later multi-agent steps.
    mcp_servers: list[AgentMcpServerSpec | str | dict[str, Any]] = field(
        default_factory=list
    )
    required_mcp_servers: list[str] = field(default_factory=list)
    hooks: dict[str, Any] | None = None

    # Model and runtime behavior.
    model: str | None = None
    effort: str | int | None = None
    permission_mode: str | None = None
    max_turns: int | None = None
    background: bool = False
    initial_prompt: str | None = None
    memory: str | None = None
    isolation: str | None = None
    omit_system_context: bool = False
    critical_system_reminder: str | None = None

    # OpenSpace-specific execution scoping.
    backend_scope: list[str] | None = None
    is_read_only: bool = False

    # Metadata/UI.
    filename: str | None = None
    color: str | None = None
    description: str = ""
    plugin: str | None = None

    @property
    def name(self) -> str:
        return self.agent_type

    def system_prompt(self, **kwargs: Any) -> str:
        prompt = self.get_system_prompt
        if isinstance(prompt, str):
            return prompt
        try:
            return prompt(**kwargs)
        except TypeError:
            return prompt()

    def to_summary_dict(self) -> dict[str, Any]:
        """Return the model/tool-facing summary shape used by AgentTool prompts."""

        payload: dict[str, Any] = {
            "agent_type": self.agent_type,
            "when_to_use": self.when_to_use,
            "source": str(self.source.value if isinstance(self.source, AgentSource) else self.source),
            "tools": self.tools,
        }
        if self.disallowed_tools:
            payload["disallowed_tools"] = list(self.disallowed_tools)
        if self.model:
            payload["model"] = self.model
        if self.effort is not None:
            payload["effort"] = self.effort
        if self.permission_mode:
            payload["permission_mode"] = self.permission_mode
        if self.max_turns is not None:
            payload["max_turns"] = self.max_turns
        if self.background:
            payload["background"] = True
        if self.backend_scope:
            payload["backend_scope"] = list(self.backend_scope)
        if self.color:
            payload["color"] = self.color
        if self.description:
            payload["description"] = self.description
        return payload


@dataclass(slots=True)
class AgentDefinitionsResult:
    active_agents: list[AgentDefinition]
    all_agents: list[AgentDefinition]
    failed_files: list[dict[str, str]] = field(default_factory=list)
    allowed_agent_types: list[str] | None = None


_SOURCE_PRECEDENCE: dict[str, int] = {
    AgentSource.BUILT_IN.value: 0,
    AgentSource.PLUGIN.value: 1,
    AgentSource.USER_SETTINGS.value: 2,
    AgentSource.PROJECT_SETTINGS.value: 3,
    AgentSource.LOCAL_SETTINGS.value: 4,
    AgentSource.CUSTOM.value: 3,
}

_LIST_FIELDS = {
    "tools",
    "disallowedTools",
    "skills",
    "mcpServers",
    "requiredMcpServers",
    "backendScope",
    "allowedAgentTypes",
}


def _source_key(source: AgentSource | str) -> str:
    return source.value if isinstance(source, AgentSource) else str(source)


def _normalise_source(source: AgentSource | str) -> AgentSource | str:
    if isinstance(source, AgentSource):
        return source
    try:
        return AgentSource(source)
    except ValueError:
        return str(source)


def get_active_agents_from_list(
    all_agents: Iterable[AgentDefinition],
) -> list[AgentDefinition]:
    """Resolve duplicate agent types using OpenSpace's source precedence.

    OpenSpace groups by source in this order, then writes each group into a map:
    built-in -> plugin -> user -> project -> flag -> managed.  Later entries
    replace earlier entries with the same ``agentType``.
    """

    ordered = sorted(
        list(all_agents),
        key=lambda agent: _SOURCE_PRECEDENCE.get(_source_key(agent.source), 50),
    )
    by_type: dict[str, AgentDefinition] = {}
    for agent in ordered:
        by_type[agent.agent_type] = agent
    return list(by_type.values())


def has_required_mcp_servers(
    agent: AgentDefinition,
    available_servers: Iterable[str],
) -> bool:
    if not agent.required_mcp_servers:
        return True
    available = [server.lower() for server in available_servers]
    return all(
        any(pattern.lower() in server for server in available)
        for pattern in agent.required_mcp_servers
    )


def filter_agents_by_mcp_requirements(
    agents: Iterable[AgentDefinition],
    available_servers: Iterable[str],
) -> list[AgentDefinition]:
    return [
        agent for agent in agents
        if has_required_mcp_servers(agent, available_servers)
    ]


class AgentDefinitionRegistry:
    """Registry for built-in, custom, and plugin agent definitions."""

    def __init__(self, *, load_built_ins: bool = True) -> None:
        self._definitions: list[AgentDefinition] = []
        self._failed_files: list[dict[str, str]] = []
        if load_built_ins:
            self._load_built_in_agents()

    def _load_built_in_agents(self) -> None:
        from openspace.agents.built_in_agents import get_built_in_agents

        for agent_def in get_built_in_agents():
            self.register(agent_def)

    def register(self, agent_def: AgentDefinition) -> None:
        self._definitions = [
            existing
            for existing in self._definitions
            if not (
                existing.agent_type == agent_def.agent_type
                and _source_key(existing.source) == _source_key(agent_def.source)
            )
        ]
        agent_def.source = _normalise_source(agent_def.source)
        self._definitions.append(agent_def)

    def get(self, agent_type: str, *, active_only: bool = True) -> AgentDefinition | None:
        agents = self.list_active() if active_only else self.list_all()
        for agent in agents:
            if agent.agent_type == agent_type:
                return agent
        return None

    def list_all(self) -> list[AgentDefinition]:
        return list(self._definitions)

    def list_active(self) -> list[AgentDefinition]:
        return get_active_agents_from_list(self._definitions)

    def result(
        self,
        *,
        allowed_agent_types: list[str] | None = None,
        available_mcp_servers: Iterable[str] | None = None,
    ) -> AgentDefinitionsResult:
        active = self.list_active()
        if allowed_agent_types is not None:
            allowed = set(allowed_agent_types)
            active = [agent for agent in active if agent.agent_type in allowed]
        if available_mcp_servers is not None:
            active = filter_agents_by_mcp_requirements(active, available_mcp_servers)
        return AgentDefinitionsResult(
            active_agents=active,
            all_agents=self.list_all(),
            failed_files=list(self._failed_files),
            allowed_agent_types=allowed_agent_types,
        )

    def clear(self, *, reload_built_ins: bool = True) -> None:
        self._definitions.clear()
        self._failed_files.clear()
        if reload_built_ins:
            self._load_built_in_agents()

    def load_custom_agents(
        self,
        agents_dir: str | os.PathLike[str],
        *,
        source: AgentSource | str = AgentSource.PROJECT_SETTINGS,
    ) -> list[AgentDefinition]:
        """Load custom agents from ``.openspace/agents`` style directories.

        Supports OpenSpace's markdown shape (frontmatter + markdown body) and JSON
        settings shape (``{name: {description, prompt, ...}}``).
        """

        path = Path(agents_dir)
        loaded: list[AgentDefinition] = []
        if not path.exists():
            return loaded

        files = sorted(
            candidate
            for candidate in path.rglob("*")
            if candidate.is_file() and candidate.suffix.lower() in {".md", ".json"}
        )
        for file_path in files:
            try:
                if file_path.suffix.lower() == ".md":
                    agent = parse_agent_from_markdown_file(file_path, source=source)
                    if agent is not None:
                        self.register(agent)
                        loaded.append(agent)
                else:
                    for agent in parse_agents_from_json_file(file_path, source=source):
                        self.register(agent)
                        loaded.append(agent)
            except Exception as exc:  # pragma: no cover - defensive guard
                self._record_failed_file(file_path, str(exc))
                logger.warning(f"Failed to load agent definition from {file_path}: {exc}")
        return loaded

    def _record_failed_file(self, file_path: Path, error: str) -> None:
        self._failed_files.append({"path": str(file_path), "error": error})


def parse_agent_from_markdown_file(
    file_path: str | os.PathLike[str],
    *,
    source: AgentSource | str = AgentSource.PROJECT_SETTINGS,
) -> AgentDefinition | None:
    path = Path(file_path)
    content = path.read_text(encoding="utf-8")
    frontmatter, body = _split_markdown_frontmatter(content)
    if not frontmatter:
        return None

    agent_type = _string(frontmatter.get("name"))
    when_to_use = _string(frontmatter.get("description"))
    if not agent_type or not when_to_use:
        return None

    return _agent_definition_from_mapping(
        agent_type,
        frontmatter,
        body.strip(),
        source=source,
        filename=path.stem,
        base_dir=str(path.parent),
    )


def parse_agents_from_json_file(
    file_path: str | os.PathLike[str],
    *,
    source: AgentSource | str = AgentSource.CUSTOM,
) -> list[AgentDefinition]:
    path = Path(file_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    return parse_agents_from_json(raw, source=source, base_dir=str(path.parent))


def parse_agents_from_json(
    raw: Any,
    *,
    source: AgentSource | str = AgentSource.CUSTOM,
    base_dir: str | None = None,
) -> list[AgentDefinition]:
    if not isinstance(raw, dict):
        return []

    if ("prompt" in raw or "system_prompt" in raw) and (
        "description" in raw or "when_to_use" in raw
    ):
        name = _string(raw.get("agent_type") or raw.get("name"))
        if not name:
            return []
        return [
            _agent_definition_from_mapping(
                name,
                raw,
                _string(raw.get("prompt") or raw.get("system_prompt")) or "",
                source=source,
                base_dir=base_dir,
            )
        ]

    agents: list[AgentDefinition] = []
    for name, definition in raw.items():
        if not isinstance(definition, dict):
            continue
        prompt = _string(definition.get("prompt") or definition.get("system_prompt")) or ""
        agent = _agent_definition_from_mapping(
            str(name),
            definition,
            prompt,
            source=source,
            base_dir=base_dir,
        )
        agents.append(agent)
    return agents


def _agent_definition_from_mapping(
    agent_type: str,
    data: dict[str, Any],
    system_prompt: str,
    *,
    source: AgentSource | str,
    filename: str | None = None,
    base_dir: str | None = None,
) -> AgentDefinition:
    when_to_use = (
        _string(data.get("description"))
        or _string(data.get("when_to_use"))
        or _string(data.get("whenToUse"))
        or ""
    )
    tools = parse_agent_tools(data.get("tools"))
    disallowed = parse_agent_tools(data.get("disallowedTools"), default=[])
    skills = parse_agent_tools(data.get("skills"), default=[])
    required_mcp = parse_agent_tools(data.get("requiredMcpServers"), default=[])
    backend_scope = parse_agent_tools(data.get("backendScope"), default=None)
    allowed_agent_types = parse_agent_tools(data.get("allowedAgentTypes"), default=None)

    return AgentDefinition(
        agent_type=agent_type,
        when_to_use=when_to_use,
        get_system_prompt=system_prompt,
        source=_normalise_source(source),
        base_dir=base_dir,
        tools=tools,
        disallowed_tools=disallowed or [],
        skills=skills or [],
        mcp_servers=_coerce_mcp_servers(data.get("mcpServers")),
        required_mcp_servers=required_mcp or [],
        hooks=data.get("hooks") if isinstance(data.get("hooks"), dict) else None,
        color=_string(data.get("color")),
        model=_normalise_model(_string(data.get("model"))),
        effort=_coerce_effort(data.get("effort")),
        permission_mode=_string(data.get("permissionMode") or data.get("permission_mode")),
        max_turns=_positive_int(data.get("maxTurns") or data.get("max_turns")),
        background=_bool(data.get("background")),
        initial_prompt=_string(data.get("initialPrompt") or data.get("initial_prompt")),
        memory=_string(data.get("memory")),
        isolation=_string(data.get("isolation")),
        omit_system_context=_bool(
            data.get("omitClaudeMd")
            if "omitClaudeMd" in data
            else data.get("omit_system_context")
        ),
        critical_system_reminder=_string(
            data.get("criticalSystemReminder_EXPERIMENTAL")
            or data.get("critical_system_reminder")
        ),
        backend_scope=backend_scope,
        allowed_agent_types=allowed_agent_types,
        filename=filename,
        description=when_to_use,
    )


def parse_agent_tools(value: Any, *, default: Any = "*") -> ToolsSpec | Any:
    """Parse skill frontmatter tool list semantics.

    Missing field means all tools.  ``*`` also means all tools.  Empty field
    means an empty list.
    """

    if value is None:
        return default
    values = _coerce_string_list(value)
    if values is None:
        return default
    if any(item == "*" for item in values):
        return "*"
    return values


def _split_markdown_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    return _parse_frontmatter_block(parts[1]), parts[2]


def _parse_frontmatter_block(raw: str) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    current_key: str | None = None
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line[:1].isspace() and current_key and stripped.startswith("- "):
            parsed.setdefault(current_key, [])
            if isinstance(parsed[current_key], list):
                parsed[current_key].append(_yaml_unquote(stripped[2:].strip()))
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if not key:
            continue
        current_key = key
        value = value.strip()
        if value == "" and key in _LIST_FIELDS:
            parsed[key] = []
        else:
            parsed[key] = _yaml_unquote(value)
    return parsed


def _coerce_string_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if value == "":
        return []
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        if raw.startswith("[") and raw.endswith("]"):
            raw = raw[1:-1]
        parts = [part.strip() for part in raw.replace("\n", ",").split(",")]
        return [_yaml_unquote(part) for part in parts if part]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _coerce_mcp_servers(value: Any) -> list[AgentMcpServerSpec | str | dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, dict):
        return [value]
    if isinstance(value, str):
        return _coerce_string_list(value) or []
    return []


def _yaml_unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        inner = value[1:-1]
        if value[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return value


def _normalise_model(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if stripped.lower() == "inherit":
        return "inherit"
    return stripped


def _coerce_effort(value: Any) -> str | int | None:
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return int(stripped)
        except ValueError:
            return stripped
    return None


def _positive_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


__all__ = [
    "AgentDefinition",
    "AgentDefinitionRegistry",
    "AgentDefinitionsResult",
    "AgentMcpServerSpec",
    "AgentSource",
    "ToolsSpec",
    "filter_agents_by_mcp_requirements",
    "get_active_agents_from_list",
    "has_required_mcp_servers",
    "parse_agent_from_markdown_file",
    "parse_agent_tools",
    "parse_agents_from_json",
    "parse_agents_from_json_file",
]
