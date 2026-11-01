from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

RuntimeEventSink = Callable[[str, dict[str, Any]], Any]


class RuntimeEventBus:
    """Fan out runtime events without owning transport-specific behavior."""

    def __init__(self, dispatcher: RuntimeEventSink | None = None) -> None:
        self._dispatcher = dispatcher
        self._sinks: list[RuntimeEventSink] = []

    def register_sink(self, sink: RuntimeEventSink) -> None:
        self._sinks.append(sink)

    def unregister_sink(self, sink: RuntimeEventSink) -> None:
        if sink in self._sinks:
            self._sinks.remove(sink)

    async def emit(self, event_type: str, data: dict[str, Any]) -> None:
        if self._dispatcher is not None:
            await self._call_sink(self._dispatcher, event_type, data)
        for sink in list(self._sinks):
            await self._call_sink(sink, event_type, data)

    async def _call_sink(
        self,
        sink: RuntimeEventSink,
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        result = sink(event_type, dict(data))
        if inspect.isawaitable(result):
            await result
