from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class RuntimePoolGateIssue:
    gate: str
    subject: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "subject": self.subject,
            "message": self.message,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class RuntimePoolGateReport:
    checked_tools: int
    issues: tuple[RuntimePoolGateIssue, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "checked_tools": self.checked_tools,
            "issues": [issue.to_dict() for issue in self.issues],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class RuntimePoolAgentGateReport:
    checked_agents: int
    issues: tuple[RuntimePoolGateIssue, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "checked_agents": self.checked_agents,
            "issues": [issue.to_dict() for issue in self.issues],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class RuntimePoolRuntimeGateReport:
    checked_runtimes: int
    checked_agents: int
    checked_tools: int
    issues: tuple[RuntimePoolGateIssue, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "checked_runtimes": self.checked_runtimes,
            "checked_agents": self.checked_agents,
            "checked_tools": self.checked_tools,
            "issues": [issue.to_dict() for issue in self.issues],
            "metadata": dict(self.metadata),
        }


def inspect_runtime_pool_tool_gate(
    tools: Sequence[Any],
    *,
    metadata: Mapping[str, Any] | None = None,
) -> RuntimePoolGateReport:
    """Check whether tool instances are safe to reuse in a future RuntimePool.

    This does not enable pooling. It verifies that tool executable instances
    are cloneable or resettable before a warmed runtime can be leased to
    another session.
    """

    issues: list[RuntimePoolGateIssue] = []
    for tool in tools:
        subject = _tool_subject(tool)
        issues.extend(_tool_state_issues(tool, subject))
        if not _has_clone_or_reset_contract(tool):
            issues.append(
                RuntimePoolGateIssue(
                    gate="tool_clone_or_reset_contract",
                    subject=subject,
                    message=(
                        "Tool has no clone_for_runtime_pool() or "
                        "reset_for_runtime_pool() contract."
                    ),
                    evidence={"type": type(tool).__name__},
                )
            )
    return RuntimePoolGateReport(
        checked_tools=len(tools),
        issues=tuple(issues),
        metadata=dict(metadata or {}),
    )


def inspect_runtime_pool_agent_gate(
    agent: Any,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> RuntimePoolAgentGateReport:
    """Check whether a GroundingAgent-like object is safe to reuse in a pool."""

    subject = _agent_subject(agent)
    issues = _agent_state_issues(agent, subject)
    if not _has_clone_or_reset_contract(agent):
        issues.append(
            RuntimePoolGateIssue(
                gate="agent_clone_or_reset_contract",
                subject=subject,
                message=(
                    "Agent has no clone_for_runtime_pool() or "
                    "reset_for_runtime_pool() contract."
                ),
                evidence={"type": type(agent).__name__},
            )
        )
    return RuntimePoolAgentGateReport(
        checked_agents=1,
        issues=tuple(issues),
        metadata=dict(metadata or {}),
    )


def inspect_runtime_pool_runtime_gate(
    runtime: Any,
    *,
    agent: Any | None = None,
    tools: Sequence[Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> RuntimePoolRuntimeGateReport:
    """Check an OpenSpace-like runtime without starting providers or sessions."""

    subject = _runtime_subject(runtime)
    issues = _runtime_state_issues(runtime, subject)
    if not _has_clone_or_reset_contract(runtime):
        issues.append(
            RuntimePoolGateIssue(
                gate="runtime_clone_or_reset_contract",
                subject=subject,
                message=(
                    "Runtime has no clone_for_runtime_pool() or "
                    "reset_for_runtime_pool() contract."
                ),
                evidence={"type": type(runtime).__name__},
            )
        )

    runtime_agent = agent if agent is not None else _extract_runtime_agent(runtime)
    checked_agents = 0
    if runtime_agent is None:
        issues.append(
            RuntimePoolGateIssue(
                gate="runtime_agent_snapshot_unavailable",
                subject=subject,
                message="RuntimePool readiness cannot be proven without an agent snapshot.",
                evidence={"type": type(runtime).__name__},
            )
        )
    else:
        checked_agents = 1
        issues.extend(
            inspect_runtime_pool_agent_gate(runtime_agent).issues
        )

    if tools is None:
        runtime_tools = _collect_runtime_tools(runtime, runtime_agent)
        if not runtime_tools:
            issues.append(
                RuntimePoolGateIssue(
                    gate="runtime_tool_snapshot_unavailable",
                    subject=subject,
                    message=(
                        "RuntimePool readiness cannot be proven without a "
                        "tool snapshot."
                    ),
                    evidence={"type": type(runtime).__name__},
                )
            )
    else:
        runtime_tools = list(tools)

    if runtime_tools:
        issues.extend(inspect_runtime_pool_tool_gate(runtime_tools).issues)

    return RuntimePoolRuntimeGateReport(
        checked_runtimes=1,
        checked_agents=checked_agents,
        checked_tools=len(runtime_tools),
        issues=tuple(issues),
        metadata=dict(metadata or {}),
    )


def assert_runtime_pool_tool_gate(tools: Sequence[Any]) -> None:
    report = inspect_runtime_pool_tool_gate(tools)
    if report.allowed:
        return
    messages = "; ".join(
        f"{issue.subject}: {issue.message}"
        for issue in report.issues[:5]
    )
    if len(report.issues) > 5:
        messages += f"; +{len(report.issues) - 5} more"
    raise RuntimeError(f"RuntimePool tool gate failed: {messages}")


def assert_runtime_pool_agent_gate(agent: Any) -> None:
    report = inspect_runtime_pool_agent_gate(agent)
    if report.allowed:
        return
    messages = "; ".join(
        f"{issue.subject}: {issue.message}"
        for issue in report.issues[:5]
    )
    if len(report.issues) > 5:
        messages += f"; +{len(report.issues) - 5} more"
    raise RuntimeError(f"RuntimePool agent gate failed: {messages}")


def assert_runtime_pool_runtime_gate(
    runtime: Any,
    *,
    agent: Any | None = None,
    tools: Sequence[Any] | None = None,
) -> None:
    report = inspect_runtime_pool_runtime_gate(runtime, agent=agent, tools=tools)
    if report.allowed:
        return
    messages = "; ".join(
        f"{issue.subject}: {issue.message}"
        for issue in report.issues[:5]
    )
    if len(report.issues) > 5:
        messages += f"; +{len(report.issues) - 5} more"
    raise RuntimeError(f"RuntimePool runtime gate failed: {messages}")


def _tool_state_issues(tool: Any, subject: str) -> list[RuntimePoolGateIssue]:
    issues: list[RuntimePoolGateIssue] = []
    runtime_info = getattr(tool, "_runtime_info", None)
    if runtime_info is not None:
        issues.append(
            RuntimePoolGateIssue(
                gate="tool_bound_runtime_info",
                subject=subject,
                message="Tool is bound to runtime/session information.",
                evidence={
                    "runtime_info": repr(runtime_info),
                },
            )
        )
    should_defer_override = getattr(tool, "_should_defer_override", None)
    if should_defer_override is not None:
        issues.append(
            RuntimePoolGateIssue(
                gate="tool_defer_override",
                subject=subject,
                message="Tool has runtime-specific defer override state.",
                evidence={
                    "should_defer_override": bool(should_defer_override),
                },
            )
        )
    current_context = getattr(tool, "_current_context", None)
    if current_context is not None:
        issues.append(
            RuntimePoolGateIssue(
                gate="tool_current_context",
                subject=subject,
                message="Tool is bound to a turn-local context.",
                evidence={
                    "context_type": type(current_context).__name__,
                },
            )
        )
    return issues


def _runtime_state_issues(runtime: Any, subject: str) -> list[RuntimePoolGateIssue]:
    issues: list[RuntimePoolGateIssue] = []

    session_state_fields = _present_fields(
        runtime,
            (
                "_current_session_id",
                "_current_session_metadata",
                "_memory_cleanup_context",
                "_event_sinks",
                "_post_execution_tasks",
            ),
    )
    if session_state_fields:
        issues.append(
            RuntimePoolGateIssue(
                gate="runtime_session_state",
                subject=subject,
                message="Runtime has session or turn state that must be reset.",
                evidence={"fields": session_state_fields},
            )
        )

    session_reference_fields = _present_fields(
        runtime,
        (
            "_llm_client",
            "_grounding_client",
            "_multi_agent",
            "_recording_manager",
            "_skill_registry",
            "_skill_store",
            "_execution_analyzer",
            "_skill_evolver",
            "_scheduler",
            "_session_storage",
            "_file_history",
            "_cost_tracker",
        ),
    )
    if session_reference_fields:
        issues.append(
            RuntimePoolGateIssue(
                gate="runtime_session_reference",
                subject=subject,
                message="Runtime has session-owned service references.",
                evidence={"fields": session_reference_fields},
            )
        )

    return issues


def _agent_state_issues(agent: Any, subject: str) -> list[RuntimePoolGateIssue]:
    issues: list[RuntimePoolGateIssue] = []

    turn_local_fields = _present_fields(
        agent,
        (
            "_last_tools",
            "_loaded_nested_memory_paths",
            "_current_instruction",
        ),
    )
    if turn_local_fields:
        issues.append(
            RuntimePoolGateIssue(
                gate="agent_turn_local_state",
                subject=subject,
                message=(
                    "Agent has turn-local state that must be reset before "
                    "RuntimePool reuse."
                ),
                evidence={"fields": turn_local_fields},
            )
        )

    runtime_callback_fields = _present_fields(
        agent,
        (
            "_runtime_event_sink",
            "_tui_bridge",
        ),
    )
    if runtime_callback_fields:
        issues.append(
            RuntimePoolGateIssue(
                gate="agent_runtime_callback",
                subject=subject,
                message=(
                    "Agent has runtime callback or bridge state that may point "
                    "at a session."
                ),
                evidence={"fields": runtime_callback_fields},
            )
        )

    session_reference_fields = _present_fields(
        agent,
        (
            "_skill_registry",
            "_multi_agent_orchestrator",
            "_coordinator_mode",
        ),
    )
    if session_reference_fields:
        issues.append(
            RuntimePoolGateIssue(
                gate="agent_session_reference",
                subject=subject,
                message="Agent has session-owned service references.",
                evidence={"fields": session_reference_fields},
            )
        )

    context_fields = _present_fields(agent, ("_current_context",))
    if context_fields:
        issues.append(
            RuntimePoolGateIssue(
                gate="agent_current_context",
                subject=subject,
                message="Agent is bound to a turn-local current context.",
                evidence={"fields": context_fields},
            )
        )

    return issues


def _has_clone_or_reset_contract(tool: Any) -> bool:
    return callable(getattr(tool, "clone_for_runtime_pool", None)) or callable(
        getattr(tool, "reset_for_runtime_pool", None)
    )


def _tool_subject(tool: Any) -> str:
    name = getattr(tool, "name", None) or getattr(tool, "_name", None)
    if name:
        return str(name)
    return type(tool).__name__


def _agent_subject(agent: Any) -> str:
    name = getattr(agent, "name", None) or getattr(agent, "_name", None)
    if name:
        return str(name)
    return type(agent).__name__


def _runtime_subject(runtime: Any) -> str:
    name = getattr(runtime, "name", None) or getattr(runtime, "_name", None)
    if name:
        return str(name)
    config = getattr(runtime, "config", None)
    profile = getattr(config, "capability_profile", None)
    if profile:
        return f"{type(runtime).__name__}[{profile}]"
    return type(runtime).__name__


def _extract_runtime_agent(runtime: Any) -> Any | None:
    for attr_name in ("grounding_agent", "agent"):
        agent = getattr(runtime, attr_name, None)
        if agent is not None:
            return agent
    return None


def _collect_runtime_tools(runtime: Any, agent: Any | None) -> list[Any]:
    tools: list[Any] = []
    if agent is not None:
        tools.extend(_coerce_tool_sequence(getattr(agent, "_last_tools", None)))

    grounding_client = getattr(runtime, "grounding_client", None)
    if grounding_client is not None:
        tools.extend(_collect_grounding_client_cached_tools(grounding_client))
        sessions = getattr(grounding_client, "_sessions", None)
        if isinstance(sessions, Mapping):
            for session in sessions.values():
                tools.extend(_coerce_tool_sequence(getattr(session, "tools", None)))

    return _dedupe_by_identity(tools)


def _collect_grounding_client_cached_tools(grounding_client: Any) -> list[Any]:
    cache = getattr(grounding_client, "_tool_cache", None)
    if not isinstance(cache, Mapping):
        return []
    tools: list[Any] = []
    for value in cache.values():
        cached_tools = value[0] if isinstance(value, tuple) and value else value
        tools.extend(_coerce_tool_sequence(cached_tools))
    return tools


def _coerce_tool_sequence(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        return []
    if isinstance(value, Sequence):
        return list(value)
    return []


def _dedupe_by_identity(values: Sequence[Any]) -> list[Any]:
    seen: set[int] = set()
    deduped: list[Any] = []
    for value in values:
        marker = id(value)
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append(value)
    return deduped


def _present_fields(obj: Any, field_names: Sequence[str]) -> dict[str, dict[str, Any]]:
    present: dict[str, dict[str, Any]] = {}
    for field_name in field_names:
        if not hasattr(obj, field_name):
            continue
        value = getattr(obj, field_name)
        if not _value_is_present(value):
            continue
        present[field_name] = _field_evidence(value)
    return present


def _value_is_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (str, bytes, list, tuple, set, frozenset, dict)):
        return len(value) > 0
    return True


def _field_evidence(value: Any) -> dict[str, Any]:
    evidence: dict[str, Any] = {"type": type(value).__name__}
    if isinstance(value, (str, bytes, list, tuple, set, frozenset, dict)):
        evidence["size"] = len(value)
    text = repr(value)
    if len(text) > 160:
        text = text[:157] + "..."
    evidence["repr"] = text
    return evidence


__all__ = [
    "RuntimePoolAgentGateReport",
    "RuntimePoolGateIssue",
    "RuntimePoolGateReport",
    "RuntimePoolRuntimeGateReport",
    "assert_runtime_pool_agent_gate",
    "assert_runtime_pool_runtime_gate",
    "assert_runtime_pool_tool_gate",
    "inspect_runtime_pool_agent_gate",
    "inspect_runtime_pool_runtime_gate",
    "inspect_runtime_pool_tool_gate",
]
