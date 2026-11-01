from __future__ import annotations

import asyncio
import copy
import inspect
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Iterable, Mapping

from openspace.grounding.core.tool.base import BaseTool

if TYPE_CHECKING:
    from openspace.services.tooling.hooks import HookRegistry
    from openspace.grounding.core.permissions.types import ToolPermissionContext

EventSink = Callable[[str, dict[str, Any]], Awaitable[None] | None]


@dataclass(slots=True)
class ReadFileEntry:
    """State cached after a file read — consumed by FileEditTool mtime check.

    Implementation: the value type of ``readFileState`` Map in Tool.ts::

        readFileState.set(path, {
            content: string,
            timestamp: number,
            offset: number | undefined,
            limit: number | undefined,
            isPartialView?: boolean,
        })

    ``content`` is stored so that on platforms where mtime can change without
    a real content modification (Windows cloud sync, antivirus touch, etc.)
    we can fall back to a byte-level comparison before rejecting the edit.
    """

    content: str
    timestamp: float
    offset: int | None = None
    limit: int | None = None
    is_partial_view: bool = False


@dataclass(slots=True)
class SkillInvocationRecord:
    """A full skill snapshot loaded through the OpenSpace Skill tool."""

    skill_id: str
    name: str
    path: str
    content: str
    args: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    model: str | None = None
    effort: str | None = None
    execution_context: str = "inline"
    invoked_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class SkillInvocationScope:
    """Runtime contract activated by a SkillTool invocation.

    OpenSpace's SkillTool returns a contextModifier for inline skills.  OS stores the
    same effect explicitly so allowed-tools/model/effort changes are visible to
    the agent loop and permission engine without mutating global settings.
    """

    scope_id: str
    skill_id: str
    name: str
    args: str = ""
    source: str = "project"
    loaded_from: str = "skills"
    execution_mode: str = "inline"
    allowed_tools_delta: list[str] = field(default_factory=list)
    model_override: str | None = None
    effort_override: str | None = None
    agent_type: str | None = None
    hooks_enabled: bool = False
    hook_registrations: list[Any] = field(default_factory=list)
    shell: str | None = None
    invocation_tool_use_id: str | None = None
    skill_event_ref_id: str | None = None
    created_turn: int = 0
    expires_after: str = "current_task"
    permission_decision: str = "allow"


def clone_skill_invocation_scopes(scopes: Mapping[str, Any] | None) -> dict[str, Any]:
    """Clone skill runtime scopes without sharing hook registrations.

    Hook registrations belong to the context that installed them. Sharing scope
    objects across auxiliary/subagent contexts lets child cleanup unregister
    parent hooks, so cloned scopes always start with an empty registration list.
    """

    cloned: dict[str, Any] = {}
    for key, scope in (scopes or {}).items():
        try:
            cloned[str(key)] = replace(scope, hook_registrations=[])
        except Exception:
            cloned[str(key)] = copy.copy(scope)
            try:
                cloned[str(key)].hook_registrations = []
            except Exception:
                pass
    return cloned


def active_skill_scope_payload(context: Any) -> dict[str, Any]:
    """Return stable active skill scope fields for runtime evidence payloads."""

    scopes = getattr(context, "active_skill_scopes", None) or {}
    summaries: list[dict[str, Any]] = []
    for scope in scopes.values():
        skill_id = str(getattr(scope, "skill_id", "") or "").strip()
        scope_id = str(getattr(scope, "scope_id", "") or "").strip()
        if not skill_id or not scope_id:
            continue
        summary = {
            "skill_id": skill_id,
            "skill_scope_id": scope_id,
            "name": str(getattr(scope, "name", "") or ""),
            "execution_mode": str(getattr(scope, "execution_mode", "") or ""),
        }
        invocation_tool_use_id = str(
            getattr(scope, "invocation_tool_use_id", "") or ""
        ).strip()
        if invocation_tool_use_id:
            summary["invocation_tool_use_id"] = invocation_tool_use_id
        skill_event_ref_id = str(getattr(scope, "skill_event_ref_id", "") or "").strip()
        if skill_event_ref_id:
            summary["skill_event_ref_id"] = skill_event_ref_id
        summaries.append(summary)

    if not summaries:
        return {}

    summaries.sort(key=lambda item: (item["skill_scope_id"], item["skill_id"]))
    skill_ids = _stable_text_list(item["skill_id"] for item in summaries)
    scope_ids = _stable_text_list(item["skill_scope_id"] for item in summaries)
    skill_event_ref_ids = _stable_text_list(
        item.get("skill_event_ref_id") for item in summaries
    )
    payload: dict[str, Any] = {
        "active_skill_ids": skill_ids,
        "skill_scope_ids": scope_ids,
        "active_skill_scopes": summaries,
    }
    if skill_event_ref_ids:
        payload["skill_event_ref_ids"] = skill_event_ref_ids
    if len(summaries) == 1:
        payload["active_skill_id"] = summaries[0]["skill_id"]
        payload["skill_id"] = summaries[0]["skill_id"]
        payload["skill_scope_id"] = summaries[0]["skill_scope_id"]
        payload["skill_invocation_scope_id"] = summaries[0]["skill_scope_id"]
        if summaries[0].get("skill_event_ref_id"):
            payload["skill_event_ref_id"] = summaries[0]["skill_event_ref_id"]
    return payload


def _stable_text_list(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


@dataclass(slots=True)
class ToolUseContext:
    """Turn-scoped runtime context for tool execution orchestration."""

    # Immutable execution config
    tools: list[BaseTool]
    model: str
    cwd: str
    agent_id: str
    original_cwd: str | None = None
    llm_client: Any | None = None
    agent_type: str | None = None
    max_result_size_chars: int = 50_000
    # Full tool universe for this turn. ``tools`` is the schema-sent active
    # subset; ``all_tools`` includes deferred tools discoverable by tool_search.
    all_tools: list[BaseTool] = field(default_factory=list)

    # Control signals and transcript
    abort_event: asyncio.Event | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)

    # Shared mutable state — OpenSpace readFileState equivalent (path → entry)
    read_file_state: dict[str, ReadFileEntry] = field(default_factory=dict)
    # OpenSpace nestedMemoryAttachmentTriggers: paths read by FileReadTool that should
    # trigger nearby OPENSPACE.md discovery before the next model call.
    nested_memory_triggers: set[str] = field(default_factory=set)
    # OpenSpace loadedNestedMemoryPaths: non-evicting dedupe for nested OPENSPACE.md
    # attachments. Unlike read_file_state, this survives read-state churn.
    loaded_nested_memory_paths: set[str] = field(default_factory=set)
    # File paths that caused nested memory discovery.  Compact can clear
    # read_file_state and still rebuild nested-memory attachments from here,
    # including non-text reads that never stored a ReadFileEntry.
    nested_memory_source_paths: set[str] = field(default_factory=set)
    tool_results_token_count: int = 0

    # Permissions and hooks
    permission_engine: Any | None = None
    permission_mode: str = "default"
    pre_plan_mode: str | None = None
    plan_file_path: str | None = None
    plan_mode_exit_pending: bool = False
    plan_mode_exited_in_session: bool = False
    # Permission context snapshot for the current turn.
    # Populated by ``GroundingAgent._create_tool_use_context`` from
    # ``loader.load_tool_permission_context(cwd, mode)``.  Each tool's
    # ``check_permissions`` receives this via ``context.permission_context``.
    # Runtime entrypoints must populate this before tool execution.  Missing
    # permission context is a wiring error and the permission engine fails closed.
    permission_context: "ToolPermissionContext | None" = None
    base_permission_context: "ToolPermissionContext | None" = None
    hook_registry: HookRegistry | None = None
    # OpenSpace session hooks are ephemeral JSON hook specs scoped to this
    # runtime context. Programmatic callbacks still live in HookRegistry.
    session_hooks: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # Optional OS policy gates for skill HTTP hooks. HTTP hooks are blocked by
    # default unless a URL allowlist is supplied here or via environment.
    http_hook_allowed_urls: list[str] | None = None
    http_hook_allowed_env_vars: list[str] | None = None
    # Whether the current turn has TUI access; disabled for headless
    # subagents so ``ask`` decisions auto-deny (see Q5 = C).
    tui_available: bool = True
    # Whether this context belongs to an async/background subagent; OpenSpace
    # uses this to route ``ask`` decisions through ``DecisionReasonAsyncAgent``.
    is_async_agent: bool = False

    # Eventing and observability
    event_sink: EventSink | None = None
    recording_manager: Any | None = None
    quality_manager: Any | None = None
    quality_recorded_tool_use_ids: set[str] = field(default_factory=set)
    cost_tracker: Any | None = None
    scheduler: Any | None = None
    ensure_scheduler: Callable[[Any | None], Awaitable[Any] | Any] | None = None
    notification_service: Any | None = None
    approval_service: Any | None = None
    lsp_manager: Any | None = None
    diagnostic_tracker: Any | None = None
    background_hook_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    background_task_ids: dict[str, str] = field(default_factory=dict)
    async_rewake_queue: asyncio.Queue[Any] | None = None
    channel_context: dict[str, Any] = field(default_factory=dict)

    # Multi-agent bookkeeping
    parent_task_id: str | None = None
    task_id: str | None = None
    task_description: str = ""
    task_manager: Any | None = None
    multi_agent_orchestrator: Any | None = None
    # OpenSpace AppState.todos equivalent: todo_key (agent_id or session_id) →
    # list[TodoItem dict].  Kept in ToolUseContext so TodoWriteTool works
    # without a global AppState singleton and so parent/child agents can share
    # a session-scoped map.
    todo_state: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    coordinator_mode: Any | None = None
    coordinator_mode_enabled: bool = False
    coordinator_notification_queue: asyncio.Queue[Any] | None = None
    coordinator_worker_tools: list[BaseTool] = field(default_factory=list)
    current_iteration: int = 0
    max_iterations: int = 0
    agent_definitions: Any | None = None
    allowed_agent_types: list[str] | None = None
    session_id: str | None = None
    session_dir: str | None = None
    tool_results_dir: str | None = None
    session_storage: Any | None = None
    file_history: Any | None = None
    memory_mode: str = "direct"
    response_style: str = "normal"
    append_system_message: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None
    capability_profile: str = ""
    backend_scope: tuple[str, ...] = ()
    deferred_tool_names: set[str] = field(default_factory=set)
    discovered_tool_names: set[str] = field(default_factory=set)
    tool_schema_cache_telemetry_enabled: bool = True
    tool_schema_cache_events: list[dict[str, Any]] = field(default_factory=list)
    mcp_clients: list[Any] = field(default_factory=list)

    # Skill Protocol state. Listing/discovery expose names and descriptions;
    # full SKILL.md content enters context only through the Skill tool.
    skill_registry: Any | None = None
    skill_store: Any | None = None
    sent_skill_names_by_agent: dict[str, set[str]] = field(default_factory=dict)
    discovered_skill_names: set[str] = field(default_factory=set)
    skill_metadata_only_discovery: bool = False
    invoked_skills_by_agent: dict[str, list[SkillInvocationRecord]] = field(default_factory=dict)
    skill_listing_suppressed_once: bool = False
    active_skill_scopes: dict[str, SkillInvocationScope] = field(default_factory=dict)
    skill_model_override: str | None = None
    skill_effort_override: str | None = None
    dynamic_skill_path_triggers: set[str] = field(default_factory=set)
    sent_dynamic_skill_keys: set[str] = field(default_factory=set)
    path_activated_skill_names: set[str] = field(default_factory=set)
    skills_disabled: bool = False

    def __post_init__(self) -> None:
        if self.original_cwd is None:
            self.original_cwd = self.cwd

    async def emit_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Emit a runtime event if an event sink is available.

        Failures are intentionally swallowed to keep tooling side effects from
        breaking the agent loop.
        """

        if self.event_sink is None:
            return

        try:
            payload = dict(data)
            if not payload.get("session_id"):
                payload["session_id"] = self.session_id
            if not payload.get("agent_id"):
                payload["agent_id"] = self.agent_id
            if payload.get("task_id") and payload.get("task_id") != self.task_id:
                if not payload.get("parent_task_id"):
                    payload["parent_task_id"] = self.task_id or self.parent_task_id
            else:
                if not payload.get("task_id"):
                    payload["task_id"] = self.task_id
                if not payload.get("parent_task_id"):
                    payload["parent_task_id"] = self.parent_task_id
            if "current_iteration" not in payload:
                payload["current_iteration"] = self.current_iteration
            result = self.event_sink(event_type, payload)
            if inspect.isawaitable(result):
                await result
        except Exception:
            pass

    def is_aborted(self) -> bool:
        """Return whether an external abort signal has been triggered."""

        return self.abort_event is not None and self.abort_event.is_set()

    def replace_messages(self, messages: list[dict[str, Any]]) -> None:
        """Update the shared message list reference for the current turn."""

        self.messages = messages

    def mark_tools_discovered(self, names: Iterable[str]) -> None:
        """Record deferred tool schemas that should be included next turn."""

        for name in names:
            if name:
                self.discovered_tool_names.add(str(name))

    def mark_skills_discovered(self, names: Iterable[str]) -> None:
        """Record skill names surfaced by discovery attachments/tools."""

        for name in names:
            if name:
                self.discovered_skill_names.add(str(name))

    def mark_path_activated_skills(self, names: Iterable[str]) -> None:
        """Record conditional ``paths`` skills activated by touched files."""

        for name in names:
            if name:
                self.path_activated_skill_names.add(str(name))

    def record_invoked_skill(
        self,
        record: SkillInvocationRecord,
        *,
        agent_id: str | None = None,
    ) -> None:
        """Remember a fully loaded skill for compact/resume retention."""

        agent_key = agent_id or self.agent_id or "primary"
        records = self.invoked_skills_by_agent.setdefault(agent_key, [])
        records[:] = [item for item in records if item.skill_id != record.skill_id]
        records.append(record)

    def clone_active_skill_scopes(self) -> dict[str, Any]:
        """Return active skill scopes safe to attach to another context."""

        return clone_skill_invocation_scopes(self.active_skill_scopes)

    def activate_skill_scope(self, scope: SkillInvocationScope) -> None:
        """Activate a skill runtime scope for this task-local context."""

        if self.base_permission_context is None:
            self.base_permission_context = self.permission_context
        self.active_skill_scopes[scope.scope_id] = scope
        self._refresh_skill_runtime_overrides()
        self.rebuild_skill_permission_context()

    def deactivate_skill_scope(self, scope_id: str) -> None:
        """Deactivate one skill runtime scope and rebuild scoped overrides."""

        scope = self.active_skill_scopes.pop(scope_id, None)
        self._unregister_skill_scope_hooks(scope)
        self._refresh_skill_runtime_overrides()
        self.rebuild_skill_permission_context()

    def deactivate_all_skill_scopes(self) -> None:
        """Clear all skill scopes at task/session end."""

        for scope in list(self.active_skill_scopes.values()):
            self._unregister_skill_scope_hooks(scope)
        self.active_skill_scopes.clear()
        self._refresh_skill_runtime_overrides()
        self.rebuild_skill_permission_context()

    def rebuild_skill_permission_context(self) -> None:
        """Rebuild permission context from base rules plus active skill grants."""

        base = self.base_permission_context or self.permission_context
        if base is None:
            return

        allow_rules = {
            source: list(rules)
            for source, rules in base.always_allow_rules.items()
        }
        command_rules = allow_rules.setdefault("command", [])
        for scope in self.active_skill_scopes.values():
            for rule in scope.allowed_tools_delta:
                raw = str(rule).strip()
                if raw and raw not in command_rules:
                    command_rules.append(raw)

        from openspace.grounding.core.permissions.types import ToolPermissionContext

        self.permission_context = ToolPermissionContext(
            mode=base.mode,
            additional_working_directories=base.additional_working_directories,
            always_allow_rules={
                source: tuple(rules)
                for source, rules in allow_rules.items()
            },
            always_deny_rules=base.always_deny_rules,
            always_ask_rules=base.always_ask_rules,
            is_bypass_permissions_mode_available=base.is_bypass_permissions_mode_available,
            stripped_dangerous_rules=base.stripped_dangerous_rules,
            should_avoid_permission_prompts=base.should_avoid_permission_prompts,
            await_automated_checks_before_dialog=base.await_automated_checks_before_dialog,
            pre_plan_mode=base.pre_plan_mode,
        )

    def _refresh_skill_runtime_overrides(self) -> None:
        self.skill_model_override = None
        self.skill_effort_override = None
        for scope in self.active_skill_scopes.values():
            if scope.model_override:
                self.skill_model_override = scope.model_override
            if scope.effort_override:
                self.skill_effort_override = scope.effort_override

    def _unregister_skill_scope_hooks(self, scope: SkillInvocationScope | None) -> None:
        if scope is None or self.hook_registry is None:
            return
        for registration in list(scope.hook_registrations):
            try:
                self.hook_registry.unregister(registration)
            except Exception:
                pass
        scope.hook_registrations.clear()

    def mark_dynamic_skill_path(self, path: str) -> None:
        """Record a touched file path for OpenSpace dynamic skill discovery."""

        if path:
            self.dynamic_skill_path_triggers.add(str(path))
