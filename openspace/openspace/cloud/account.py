"""User and agent-key lifecycle client for OpenSpace cloud auth."""

from __future__ import annotations

import json
import urllib.parse
from typing import Any, Dict

from openspace.cloud.base import cloud_api_url
from openspace.cloud.config import CloudConfig, require_cloud_enabled
from openspace.cloud.client import CloudError
from openspace.cloud.transport import (
    CloudRequest,
    CloudResponse,
    CloudTransport,
    UrllibCloudTransport,
)


class OpenSpaceAccountClient:
    """Synchronous client for cloud auth and agent-key management endpoints."""

    _DEFAULT_UA = "OpenSpace-Client/1.0"

    def __init__(
        self,
        config: CloudConfig,
        transport: CloudTransport | None = None,
    ):
        self._config = require_cloud_enabled(config)
        self._transport = transport or UrllibCloudTransport()

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        version: str = "v1",
        payload: Dict[str, Any] | None = None,
        headers: Dict[str, str] | None = None,
        timeout: int = 30,
    ) -> Dict[str, Any] | list[Any]:
        body = None
        request_headers = {
            "User-Agent": self._DEFAULT_UA,
            **(headers or {}),
        }
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            request_headers["Content-Type"] = "application/json"

        response = self._transport.send(
            CloudRequest(
                method=method,
                url=cloud_api_url(self._config.base_url, version, path),
                headers=request_headers,
                body=body,
                timeout=timeout,
            )
        )
        self._raise_for_status(response)
        data = response.body

        if not data:
            return {}
        return json.loads(data.decode("utf-8"))

    @staticmethod
    def _raise_for_status(response: CloudResponse) -> None:
        if 200 <= response.status_code < 300:
            return
        body = response.body.decode("utf-8", errors="replace")
        raise CloudError(
            f"HTTP {response.status_code}: {body[:500]}",
            status_code=response.status_code,
            body=body,
        )

    @staticmethod
    def _bearer_headers(access_token: str) -> Dict[str, str]:
        return {"Authorization": f"Bearer {access_token}"}

    def register_user(
        self,
        *,
        email: str,
        password: str,
        name: str | None = None,
    ) -> Dict[str, Any]:
        """POST /api/v2/auth/users/register."""
        payload: Dict[str, Any] = {"email": email, "password": password}
        return self._request_json("POST", "/auth/users/register", version="v2", payload=payload)

    def login_user(self, *, email: str, password: str) -> Dict[str, Any]:
        """POST /api/v2/auth/users/login."""
        return self._request_json(
            "POST",
            "/auth/users/login",
            version="v2",
            payload={"email": email, "password": password},
        )

    def agent_bootstrap(
        self,
        *,
        email: str,
        password: str,
        agent_name: str,
    ) -> Dict[str, Any]:
        """POST /api/v2/auth/agent-bootstrap.

        This endpoint intentionally sends no Authorization, X-API-Key, or
        X-Admin-Key headers. The returned ``api_key`` is shown once by server
        contract and must be stored by the caller.
        """
        return self._request_json(
            "POST",
            "/auth/agent-bootstrap",
            version="v2",
            payload={
                "email": email,
                "password": password,
                "agent_name": agent_name,
            },
        )

    def create_agent(self, *, access_token: str, name: str) -> Dict[str, Any]:
        """POST /api/v1/agents and return the one-time agent API key."""
        return self._request_json(
            "POST",
            "/agents",
            payload={"name": name},
            headers=self._bearer_headers(access_token),
        )

    def list_agents(self, *, access_token: str) -> list[Dict[str, Any]]:
        """GET /api/v1/agents using a user bearer token."""
        result = self._request_json(
            "GET",
            "/agents",
            headers=self._bearer_headers(access_token),
        )
        if not isinstance(result, list):
            raise CloudError("GET /api/v1/agents returned a non-list response")
        return result

    def rotate_agent_key(self, *, access_token: str, agent_id: str) -> Dict[str, Any]:
        """POST /api/v1/agents/{agent_id}/rotate-key."""
        return self._request_json(
            "POST",
            f"/agents/{urllib.parse.quote(str(agent_id), safe='')}/rotate-key",
            headers=self._bearer_headers(access_token),
        )

    def me(
        self,
        *,
        access_token: str | None = None,
        api_key: str | None = None,
    ) -> Dict[str, Any]:
        """GET /api/v1/auth/me for either user bearer or agent API key."""
        if bool(access_token) == bool(api_key):
            raise CloudError("Provide exactly one of access_token or api_key")
        headers = (
            self._bearer_headers(access_token)
            if access_token
            else {"X-API-Key": str(api_key)}
        )
        return self._request_json("GET", "/auth/me", headers=headers)
