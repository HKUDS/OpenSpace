"""HTTP transport boundary for OpenSpace cloud clients."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol
import urllib.error
import urllib.request


@dataclass(frozen=True)
class CloudRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None = None
    timeout: int = 30


@dataclass(frozen=True)
class CloudResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes


class CloudTransport(Protocol):
    def send(self, request: CloudRequest) -> CloudResponse:
        """Send one cloud HTTP request and return the raw response."""


class UrllibCloudTransport:
    """Default synchronous transport backed by urllib."""

    def send(self, request: CloudRequest) -> CloudResponse:
        req = urllib.request.Request(
            request.url,
            data=request.body,
            headers=dict(request.headers),
            method=request.method,
        )
        try:
            with urllib.request.urlopen(req, timeout=request.timeout) as resp:
                return CloudResponse(
                    status_code=resp.status,
                    headers=dict(resp.headers.items()),
                    body=resp.read(),
                )
        except urllib.error.HTTPError as exc:
            return CloudResponse(
                status_code=exc.code,
                headers=dict(exc.headers.items()) if exc.headers else {},
                body=exc.read(),
            )
        except urllib.error.URLError as exc:
            from openspace.cloud.client import CloudError

            raise CloudError(f"Connection failed: {exc.reason}") from exc
