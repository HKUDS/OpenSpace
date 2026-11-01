from typing import Any
from yarl import URL
import aiohttp

from ..task_managers import AioHttpConnectionManager
from .base import BaseConnector
from openspace.utils.logging import Logger
from pydantic import BaseModel

logger = Logger.get_logger(__name__)


class AioHttpConnector(BaseConnector[aiohttp.ClientSession]):
    """Generic HTTP-based connector with auto-reconnect & helper methods."""

    def __init__(self, base_url: str, **session_kw):
        connection_manager = AioHttpConnectionManager(base_url, **session_kw)
        super().__init__(connection_manager)
        self.base_url = base_url.rstrip("/")
        
    async def connect(self) -> None:
        await super().connect()
        try:
            async with self._connection.get(self.base_url, timeout=5) as resp:
                if resp.status >= 500:
                    raise ConnectionError(f"HTTP {resp.status}")
        except Exception as e:
            await self.disconnect()
            raise ConnectionError(f"Ping {self.base_url} failed: {e}")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any | BaseModel | None = None, 
        data: Any | None = None,
        params: dict[str, Any] | None = None,
        **kw,
    ) -> aiohttp.ClientResponse:
        if not self.is_connected:
            await self.connect()

        assert self._connection is not None            # for mypy
        url = URL(self.base_url) / path.lstrip("/")
        logger.debug("%s %s", method.upper(), url)
        return await self._connection.request(
            method.upper(),
            url,
            json=self._to_json_compatible(json), 
            data=data,
            params=params,
            **kw,
        )

    async def get_json(self, path: str, **kw) -> Any:
        response_model: type[BaseModel] | None = kw.pop("response_model", None)
        resp = await self._request("GET", path, **kw)
        resp.raise_for_status()
        data = await resp.json()
        return self._parse_as(data, response_model)

    async def get_bytes(self, path: str, **kw) -> bytes:
        resp = await self._request("GET", path, **kw)
        resp.raise_for_status()
        return await resp.read()
    
    async def post_json(
        self,
        path: str,
        payload: Any | BaseModel,
        *,
        response_model: type[BaseModel] | None = None,
        **kw,
    ) -> Any | BaseModel:
        resp = await self._request("POST", path, json=payload, **kw)
        
        try:
            data = await resp.json()
        except Exception:
            data = None
        
        if resp.status >= 400:
            # Extract detailed error from response body
            detail = ""
            if data:
                detail = data.get("output") or data.get("message") or data.get("error") or ""
            error_msg = f"{resp.status}, message='{resp.reason}'"
            if detail:
                error_msg += f", detail='{detail}'"
            raise aiohttp.ClientResponseError(
                resp.request_info,
                resp.history,
                status=resp.status,
                message=error_msg,
            )
        
        return self._parse_as(data, response_model)

    async def request(self, method: str, path: str, **kw) -> aiohttp.ClientResponse:
        return await self._request(method, path, **kw)

    async def invoke(self, name: str, params: dict[str, Any]) -> Any:
        """
        Generic tool-invocation mapping for HTTP back-ends.

        name rule (case-insensitive):
        - "GET /path"          -> GET, return JSON
        - "GET_TEXT /path"     -> GET, return str
        - "GET_BYTES /path"    -> GET, return bytes
        - "POST /path"         -> POST, payload = params (JSON)
        - other                -> default POST /{name}, payload = params
        
        If PUT/PATCH/DELETE is needed in the future, it can be reused in _handle_other_json.
        """
        verb_path = name.strip().split(maxsplit=1)
        verb = verb_path[0].upper()
        path = verb_path[1] if len(verb_path) == 2 else verb_path[0]

        if verb == "GET_BYTES":
            return await self.get_bytes(path, params=params)

        if verb == "GET_TEXT":
            resp = await self._request("GET", path, params=params)
            resp.raise_for_status()
            return await resp.text()

        if verb in {"GET", "POST"} and len(verb_path) == 2:
            if verb == "GET":
                return await self.get_json(path, params=params)
            return await self.post_json(path, payload=params)

        if verb in {"PUT", "PATCH", "DELETE"} and len(verb_path) == 2:
            return await self._handle_other_json(verb, path, params)

        return await self.post_json(name, payload=params)

    async def _handle_other_json(self, method: str, path: str, params: dict[str, Any]):
        """Fallback implementation for PUT/PATCH/DELETE returning JSON/text, can be overridden by subclasses."""
        resp = await self._request(method, path, json=params)
        resp.raise_for_status()
        try:
            return await resp.json()
        except Exception:
            return await resp.text()