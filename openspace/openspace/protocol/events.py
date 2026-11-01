"""Canonical Core/TUI event protocol definitions."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any

_SCHEMA_PATH = Path(__file__).with_name("schema") / "events.json"


@dataclass(frozen=True, slots=True)
class PayloadValidationResult:
    event_type: str
    valid: bool
    errors: tuple[str, ...] = ()
    malformed_strategy: str = "reject"

    @property
    def fail_closed(self) -> bool:
        return self.malformed_strategy == "fail_closed"


@lru_cache(maxsize=1)
def load_event_manifest() -> dict[str, Any]:
    manifest = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    if manifest.get("manifest_schema_version") != 1:
        raise ValueError("events.json manifest_schema_version must be 1")
    for key in ("tui_to_core", "core_to_tui"):
        values = manifest.get(key)
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ValueError(f"events.json {key} must be a list of strings")
        if len(values) != len(set(values)):
            raise ValueError(f"events.json {key} must not contain duplicate events")

    payload_types = manifest.get("payload_types", {})
    if not isinstance(payload_types, dict):
        raise ValueError("events.json payload_types must be an object")
    for event_type in (*manifest["tui_to_core"], *manifest["core_to_tui"]):
        if event_type not in payload_types:
            raise ValueError(f"events.json payload_types missing {event_type!r}")

    payload_schemas = manifest.get("payload_schemas", {})
    if not isinstance(payload_schemas, dict):
        raise ValueError("events.json payload_schemas must be an object")
    for event_type, schema in payload_schemas.items():
        if event_type not in payload_types:
            raise ValueError(f"events.json payload_schemas has unknown event {event_type!r}")
        if not isinstance(schema, dict):
            raise ValueError(f"events.json payload schema for {event_type!r} must be an object")
    return manifest


def get_tui_to_core_events() -> tuple[str, ...]:
    return tuple(load_event_manifest()["tui_to_core"])


def get_core_to_tui_events() -> tuple[str, ...]:
    return tuple(load_event_manifest()["core_to_tui"])


def get_all_event_types() -> tuple[str, ...]:
    return get_tui_to_core_events() + get_core_to_tui_events()


def get_payload_type_map() -> dict[str, str]:
    return dict(load_event_manifest()["payload_types"])


def get_payload_schema(event_type: str) -> dict[str, Any] | None:
    schema = load_event_manifest().get("payload_schemas", {}).get(event_type)
    return dict(schema) if isinstance(schema, dict) else None


def get_unknown_event_strategy() -> dict[str, Any]:
    strategy = load_event_manifest().get("unknown_event", {})
    if not isinstance(strategy, dict):
        return {}
    return dict(strategy)


def _enum_member_name(value: str) -> str:
    name = re.sub(r"[^0-9A-Za-z]+", "_", value).strip("_").upper()
    if not name:
        raise ValueError("event value cannot produce enum member name")
    if name[0].isdigit():
        name = f"EVENT_{name}"
    return name


def _build_event_enum(name: str, values: tuple[str, ...]) -> type[Enum]:
    members = {_enum_member_name(value): value for value in values}
    return Enum(name, members, type=str, module=__name__)


TuiToCoreEvent = _build_event_enum("TuiToCoreEvent", get_tui_to_core_events())
CoreToTuiEvent = _build_event_enum("CoreToTuiEvent", get_core_to_tui_events())
ALL_EVENT_TYPES: set[str] = set(get_all_event_types())


def is_known_event_type(event_type: str) -> bool:
    return event_type in ALL_EVENT_TYPES


def make_protocol_warning_event(
    *,
    source_event_type: str | None,
    reason: str,
    raw: Any | None = None,
) -> "StreamEvent":
    """Return the unified warning event for unknown or malformed protocol input."""

    strategy = get_unknown_event_strategy()
    warning_event = str(strategy.get("event") or "notification")
    level = str(strategy.get("level") or "warn")
    title = str(strategy.get("title") or "Protocol warning")
    data: dict[str, Any] = {
        "level": level,
        "title": title,
        "message": reason,
    }
    if source_event_type:
        data["event_type"] = source_event_type
    if raw is not None:
        data["raw"] = raw
    return StreamEvent(type=warning_event, data=data)


def _matches_type(value: Any, expected: Any) -> bool:
    if isinstance(expected, list):
        return any(_matches_type(value, item) for item in expected)
    if isinstance(expected, dict):
        if "enum" in expected:
            options = expected["enum"]
            return isinstance(options, list) and value in options
        expected_type = expected.get("type")
        if expected_type is not None:
            return _matches_type(value, expected_type)
        return True
    if expected == "any":
        return True
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "null":
        return value is None
    return True


def validate_event_payload(event_type: str, payload: Any) -> PayloadValidationResult:
    schema = get_payload_schema(event_type)
    strategy = "reject"
    if schema is not None:
        strategy = str(schema.get("malformed_strategy") or strategy)

    errors: list[str] = []
    if schema is None:
        return PayloadValidationResult(
            event_type=event_type,
            valid=True,
            malformed_strategy=strategy,
        )

    if schema.get("type", "object") == "object" and not isinstance(payload, dict):
        return PayloadValidationResult(
            event_type=event_type,
            valid=False,
            errors=("payload must be an object",),
            malformed_strategy=strategy,
        )

    if not isinstance(payload, dict):
        return PayloadValidationResult(
            event_type=event_type,
            valid=True,
            malformed_strategy=strategy,
        )

    required = schema.get("required", {})
    if not isinstance(required, dict):
        raise ValueError(f"payload schema for {event_type!r} has invalid required map")
    for field_name, expected in required.items():
        if field_name not in payload:
            errors.append(f"missing required field {field_name!r}")
            continue
        if not _matches_type(payload[field_name], expected):
            errors.append(f"field {field_name!r} has invalid type or value")

    optional = schema.get("optional", {})
    if not isinstance(optional, dict):
        raise ValueError(f"payload schema for {event_type!r} has invalid optional map")
    for field_name, expected in optional.items():
        if field_name in payload and not _matches_type(payload[field_name], expected):
            errors.append(f"field {field_name!r} has invalid type or value")

    return PayloadValidationResult(
        event_type=event_type,
        valid=not errors,
        errors=tuple(errors),
        malformed_strategy=strategy,
    )


@dataclass(slots=True)
class StreamEvent:
    """Unified IPC message envelope exchanged between Python Core and TS TUI."""

    type: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=lambda: time.time() * 1000)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "data": self.data, "timestamp": self.timestamp}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "StreamEvent":
        if not isinstance(raw, dict):
            return make_protocol_warning_event(
                source_event_type=None,
                reason="Malformed protocol event: envelope must be an object.",
                raw=raw,
            )

        raw_type = raw.get("type")
        if not isinstance(raw_type, str) or not raw_type:
            return make_protocol_warning_event(
                source_event_type=None,
                reason="Malformed protocol event: missing string type.",
                raw=raw,
            )

        if not is_known_event_type(raw_type):
            return make_protocol_warning_event(
                source_event_type=raw_type,
                reason=f"Unknown protocol event type: {raw_type}",
                raw=raw,
            )

        data = raw.get("data", {})
        validation = validate_event_payload(raw_type, data)
        if not validation.valid:
            return make_protocol_warning_event(
                source_event_type=raw_type,
                reason=(
                    f"Malformed protocol event payload for {raw_type}: "
                    + "; ".join(validation.errors)
                ),
                raw=raw if validation.fail_closed else None,
            )

        return cls(
            type=raw_type,
            data=data,
            timestamp=raw.get("timestamp", raw.get("ts", time.time() * 1000)),
        )

    def is_valid(self) -> bool:
        return self.type in ALL_EVENT_TYPES

    @property
    def ts(self) -> float:
        """Backward-compatible alias for older test fixtures."""
        return self.timestamp
