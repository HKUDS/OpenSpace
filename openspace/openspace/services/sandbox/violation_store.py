"""In-memory store for recent process sandbox violations."""

from __future__ import annotations

import time
from collections import deque
from typing import Iterable

from .sandbox_utils import split_xml_safe
from .types import SandboxViolation


class SandboxViolationStore:
    """Small ring buffer mirroring OpenSpace sandbox-runtime's violation store role."""

    def __init__(self, max_items: int = 200) -> None:
        self._items: deque[SandboxViolation] = deque(maxlen=max_items)

    def add(self, violation: SandboxViolation) -> SandboxViolation:
        if not violation.timestamp_ms:
            violation.timestamp_ms = time.time() * 1000
        self._items.append(violation)
        return violation

    def clear(self) -> None:
        self._items.clear()

    def recent(self, *, limit: int | None = None) -> list[SandboxViolation]:
        items = list(self._items)
        if limit is not None:
            items = items[-limit:]
        return items

    def for_command_tag(self, command_tag: str) -> list[SandboxViolation]:
        return [item for item in self._items if item.command_tag == command_tag]

    def for_command(self, command: str) -> list[SandboxViolation]:
        return [item for item in self._items if item.command == command]


def violations_to_xml(violations: Iterable[SandboxViolation]) -> str:
    items = list(violations)
    if not items:
        return ""
    lines = ["<sandbox_violations>"]
    for violation in items:
        attrs = [
            f'operation="{split_xml_safe(violation.operation)}"',
            f'platform="{split_xml_safe(str(violation.platform))}"',
        ]
        if violation.path:
            attrs.append(f'path="{split_xml_safe(violation.path)}"')
        if violation.domain:
            attrs.append(f'domain="{split_xml_safe(violation.domain)}"')
        message = violation.raw_message or "Sandbox blocked this operation."
        lines.append(f"<violation {' '.join(attrs)}>")
        lines.append(split_xml_safe(message))
        lines.append("</violation>")
    lines.append("</sandbox_violations>")
    return "\n".join(lines)


__all__ = ["SandboxViolationStore", "violations_to_xml"]
