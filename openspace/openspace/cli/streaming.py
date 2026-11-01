"""Compatibility re-exports for the Core/TUI event protocol.

The canonical event definitions live in :mod:`openspace.protocol.events`.
"""

from __future__ import annotations

from openspace.protocol.events import (
    ALL_EVENT_TYPES,
    CoreToTuiEvent,
    PayloadValidationResult,
    StreamEvent,
    TuiToCoreEvent,
    get_all_event_types,
    get_core_to_tui_events,
    get_payload_schema,
    get_payload_type_map,
    get_tui_to_core_events,
    get_unknown_event_strategy,
    is_known_event_type,
    load_event_manifest,
    make_protocol_warning_event,
    validate_event_payload,
)

__all__ = [
    "ALL_EVENT_TYPES",
    "CoreToTuiEvent",
    "PayloadValidationResult",
    "StreamEvent",
    "TuiToCoreEvent",
    "get_all_event_types",
    "get_core_to_tui_events",
    "get_payload_schema",
    "get_payload_type_map",
    "get_tui_to_core_events",
    "get_unknown_event_strategy",
    "is_known_event_type",
    "load_event_manifest",
    "make_protocol_warning_event",
    "validate_event_payload",
]
