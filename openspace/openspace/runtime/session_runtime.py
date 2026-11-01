from __future__ import annotations

from typing import Any

from openspace.persistence import RuntimeSessionAPI


class SessionRuntime:
    """Thin runtime wrapper over the persistence public API."""

    def __init__(
        self,
        api: RuntimeSessionAPI | None = None,
    ) -> None:
        self._api = api or RuntimeSessionAPI()

    @classmethod
    def from_persistence_api(cls, api: RuntimeSessionAPI) -> "SessionRuntime":
        return cls(api)

    @classmethod
    def from_runtime(cls, runtime: Any) -> "SessionRuntime":
        return cls.from_persistence_api(RuntimeSessionAPI.from_runtime(runtime))

    async def prepare(self, execution_context: dict[str, Any]) -> str:
        if self._api.prepare_session is None:
            raise RuntimeError("Session prepare handler is not configured")
        return await self._api.prepare_session(execution_context)

    async def persist(
        self,
        final_result: dict[str, Any],
        execution_context: dict[str, Any],
    ) -> None:
        if self._api.persist_session is None:
            raise RuntimeError("Session persist handler is not configured")
        await self._api.persist_session(final_result, execution_context)

    async def restore(self, session_id: str) -> dict[str, Any]:
        if self._api.restore_session is None:
            raise RuntimeError("Session restore handler is not configured")
        return await self._api.restore_session(session_id)

    async def fork(self, session_id: str) -> dict[str, Any]:
        if self._api.fork_session is None:
            raise RuntimeError("Session fork handler is not configured")
        return await self._api.fork_session(session_id)

    async def rewind(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if self._api.rewind_session is None:
            raise RuntimeError("Session rewind handler is not configured")
        return await self._api.rewind_session(session_id, messages)

    async def discover(self, **kwargs: Any) -> dict[str, Any]:
        if self._api.discover_sessions is None:
            raise RuntimeError("Session discovery handler is not configured")
        return await self._api.discover_sessions(**kwargs)
