"""Product-owned shared inner-agent selection syntax and frozen identity."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from rsi_arena.contracts import canonical_digest
from rsi_arena.experiment import JSONObject
from rsi_arena.run_layout import RunValidationError

INNER_AGENT_SELECTION_SCHEMA = "rsi-arena-inner-agent-selection"
INNER_AGENT_CALL_INTERFACE = "rsi-arena-inner-agent-call"

_PYTHON_REF = re.compile(
    r"python:(?P<module>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*):"
    r"(?P<callable>[A-Za-z_]\w*)\Z"
)
_BUILTIN_REF = re.compile(r"builtin:(?P<name>[a-z0-9][a-z0-9-]*)\Z")
_BUILTIN_SUPPORT = {
    "builtin:terminus-2": frozenset({"aime", "tb2", "swe", "browsecomp_plus"}),
    "builtin:mini-swe-agent": frozenset({"swe"}),
}
_PYTHON_SUPPORT = frozenset({"aime", "tb2", "swe", "browsecomp_plus"})


class InnerAgentSelectionError(ValueError):
    """The product cannot freeze or bind the requested shared inner agent."""


@dataclass(frozen=True)
class PythonInnerAgentRef:
    """The parsed value of one explicit candidate-carried Python reference."""

    module: str
    callable: str

    @property
    def top_level(self) -> str:
        return self.module.split(".", 1)[0]


def parse_python_inner_agent_ref(ref: object) -> PythonInnerAgentRef | None:
    """Parse the sole Python-ref grammar without importing candidate code."""

    if not isinstance(ref, str):
        return None
    match = _PYTHON_REF.fullmatch(ref)
    if match is None:
        return None
    return PythonInnerAgentRef(
        module=match.group("module"),
        callable=match.group("callable"),
    )


def _json_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise InnerAgentSelectionError(f"{label} must be a JSON object")
    try:
        copied = json.loads(json.dumps(dict(value), allow_nan=False))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise InnerAgentSelectionError(f"{label} must be finite JSON") from exc
    assert isinstance(copied, dict)
    return copied


def _validate_ref(ref: object) -> str:
    if not isinstance(ref, str) or not ref:
        raise InnerAgentSelectionError("benchmark_runtime.inner_agent.ref is required")
    if _BUILTIN_REF.fullmatch(ref) is not None:
        if ref not in _BUILTIN_SUPPORT:
            raise InnerAgentSelectionError(
                f"unknown builtin inner-agent preset: {ref!r}"
            )
        return ref
    if parse_python_inner_agent_ref(ref) is not None:
        return ref
    raise InnerAgentSelectionError(
        "inner-agent ref must be builtin:<preset> or python:<module>:<callable>"
    )


@dataclass(frozen=True)
class FrozenInnerAgentSelection:
    """One cell-wide candidate inner-agent ref/config identity."""

    ref: str
    config: Mapping[str, object]

    @property
    def is_python(self) -> bool:
        return parse_python_inner_agent_ref(self.ref) is not None

    @property
    def identity(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "schema": INNER_AGENT_SELECTION_SCHEMA,
                "ref": self.ref,
                "config_digest": "sha256:" + canonical_digest(dict(self.config)),
                "interface": INNER_AGENT_CALL_INTERFACE,
            }
        )


def freeze_inner_agent_selection(
    resolved: JSONObject,
    *,
    resumed: bool,
) -> FrozenInnerAgentSelection:
    """Freeze syntax/config only; candidate source is runtime candidate state."""

    runtime = resolved.get("benchmark_runtime")
    if not isinstance(runtime, dict):
        raise InnerAgentSelectionError("benchmark_runtime must be a JSON object")
    inner_agent = runtime.get("inner_agent")
    if not isinstance(inner_agent, dict):
        raise InnerAgentSelectionError(
            "benchmark_runtime.inner_agent must be a JSON object"
        )
    ref = _validate_ref(inner_agent.get("ref"))
    config = _json_object(
        inner_agent.get("config", {}),
        "benchmark_runtime.inner_agent.config",
    )
    selection = FrozenInnerAgentSelection(ref, MappingProxyType(config))

    expected = dict(selection.identity)
    stored = inner_agent.get("resolved", {})
    if not isinstance(stored, dict):
        raise InnerAgentSelectionError(
            "benchmark_runtime.inner_agent.resolved must be a JSON object"
        )
    if resumed:
        if stored != expected:
            raise RunValidationError("stored inner-agent selection identity changed")
    else:
        if stored:
            raise InnerAgentSelectionError(
                "benchmark_runtime.inner_agent.resolved is product-written"
            )
        inner_agent["resolved"] = expected
    return selection


def validate_inner_agent_package_support(
    selection: FrozenInnerAgentSelection,
    benchmark_ids: tuple[str, ...],
) -> None:
    """Reject cells whose active packages lack the selected thin adapter."""

    supported = (
        _PYTHON_SUPPORT if selection.is_python else _BUILTIN_SUPPORT[selection.ref]
    )
    unsupported = tuple(
        benchmark_id for benchmark_id in benchmark_ids if benchmark_id not in supported
    )
    if unsupported:
        raise InnerAgentSelectionError(
            f"inner agent {selection.ref!r} has no package adapter for: "
            + ", ".join(unsupported)
        )


__all__ = [
    "INNER_AGENT_CALL_INTERFACE",
    "INNER_AGENT_SELECTION_SCHEMA",
    "FrozenInnerAgentSelection",
    "InnerAgentSelectionError",
    "PythonInnerAgentRef",
    "freeze_inner_agent_selection",
    "parse_python_inner_agent_ref",
    "validate_inner_agent_package_support",
]
