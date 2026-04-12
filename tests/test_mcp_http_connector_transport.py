from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_FILE = REPO_ROOT / "openspace/grounding/backends/mcp/transport/connectors/base.py"
CORE_TM_BASE_FILE = REPO_ROOT / "openspace/grounding/core/transport/task_managers/base.py"
HTTP_FILE = REPO_ROOT / "openspace/grounding/backends/mcp/transport/connectors/http.py"


class _DummyStreamableHttpConnectionManager:
    instances: list["_DummyStreamableHttpConnectionManager"] = []

    def __init__(self, url, headers, timeout, read_timeout):
        self.url = url
        self.headers = headers
        self.timeout = timeout
        self.read_timeout = read_timeout
        self.started = False
        self.stopped = False
        _DummyStreamableHttpConnectionManager.instances.append(self)

    async def start(self, timeout=None):
        self.started = True
        self.timeout_used = timeout
        return "read-stream", "write-stream"

    def get_streams(self):
        return ("read-stream", "write-stream")

    async def stop(self):
        self.stopped = True


class _ForbiddenSseConnectionManager:
    def __init__(self, *args, **kwargs):
        raise AssertionError(
            "SSE fallback should not be constructed when streamable HTTP succeeds"
        )


class _DummyClientSession:
    def __init__(self, read_stream, write_stream, sampling_callback=None):
        self.read_stream = read_stream
        self.write_stream = write_stream
        self.sampling_callback = sampling_callback
        self.entered = False
        self.initialized = False
        self.tools_listed = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def initialize(self):
        self.initialized = True

    async def list_tools(self):
        self.tools_listed = True
        return []

    async def __aexit__(self, exc_type, exc, tb):
        self.exited = True


def _install_package_stub(
    monkeypatch,
    module_name: str,
    **attributes,
) -> ModuleType:
    module = ModuleType(module_name)
    module.__path__ = []  # mark as package
    for key, value in attributes.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, module_name, module)
    return module


class _BaseConnectorStub:
    @classmethod
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, connection_manager):
        self._connection_manager = connection_manager
        self._connection = None
        self._connected = False

    async def _cleanup_on_connect_failure(self):
        if self._connection_manager and hasattr(self._connection_manager, "stop"):
            maybe_awaitable = self._connection_manager.stop()
            if hasattr(maybe_awaitable, "__await__"):
                await maybe_awaitable
        self._connection = None

    async def _after_disconnect(self):
        return None


class _BaseConnectionManagerStub:
    @classmethod
    def __class_getitem__(cls, item):
        return cls


class _AsyncContextConnectionManagerStub(_BaseConnectionManagerStub):
    pass


class _PlaceholderConnectionManagerStub:
    def __init__(self, *args, **kwargs):
        self._connection = None

    async def start(self, timeout=None):
        return self._connection

    async def stop(self, timeout=5.0):
        return None

    def get_streams(self):
        return self._connection


def _load_module(module_name: str, file_path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_http_module(monkeypatch) -> ModuleType:
    # Stub the package layers so we can load the target file without
    # importing the broader MCP package tree and its optional deps.
    _install_package_stub(
        monkeypatch,
        "openspace.grounding.core.transport.connectors",
        BaseConnector=_BaseConnectorStub,
    )
    _install_package_stub(
        monkeypatch,
        "openspace.grounding.core.transport.task_managers",
        BaseConnectionManager=_BaseConnectionManagerStub,
        AsyncContextConnectionManager=_AsyncContextConnectionManagerStub,
        PlaceholderConnectionManager=_PlaceholderConnectionManagerStub,
    )
    _install_package_stub(
        monkeypatch,
        "openspace.utils.logging",
        Logger=type(
            "Logger",
            (),
            {"get_logger": staticmethod(logging.getLogger)},
        ),
    )
    _install_package_stub(
        monkeypatch,
        "openspace.grounding.backends.mcp.transport.task_managers",
        SseConnectionManager=type("SseConnectionManager", (), {}),
        StreamableHttpConnectionManager=type(
            "StreamableHttpConnectionManager", (), {}
        ),
    )
    _install_package_stub(
        monkeypatch,
        "openspace.grounding.backends.mcp.transport.connectors",
    )
    _install_package_stub(
        monkeypatch,
        "openspace.grounding.backends.mcp.transport",
    )
    _install_package_stub(
        monkeypatch,
        "openspace.grounding.backends.mcp",
    )

    _load_module(
        "openspace.grounding.backends.mcp.transport.connectors.base",
        BASE_FILE,
    )
    _load_module(
        "openspace.grounding.core.transport.task_managers.base",
        CORE_TM_BASE_FILE,
    )
    return _load_module(
        "openspace.grounding.backends.mcp.transport.connectors.http",
        HTTP_FILE,
    )


@pytest.mark.asyncio
async def test_http_connector_prefers_streamable_http(monkeypatch) -> None:
    http_module = _load_http_module(monkeypatch)

    monkeypatch.setattr(
        http_module,
        "StreamableHttpConnectionManager",
        _DummyStreamableHttpConnectionManager,
    )
    monkeypatch.setattr(
        http_module,
        "SseConnectionManager",
        _ForbiddenSseConnectionManager,
    )
    monkeypatch.setattr(http_module, "ClientSession", _DummyClientSession)

    connector = http_module.HttpConnector("http://127.0.0.1:8123/mcp")

    await connector.connect()

    assert connector.transport_type == "streamable HTTP"
    assert isinstance(
        connector._connection_manager, _DummyStreamableHttpConnectionManager
    )
    assert connector._connection == ("read-stream", "write-stream")
    assert connector.client_session.entered is True
    assert connector.client_session.initialized is True
    assert connector.client_session.tools_listed is True

    client_session = connector.client_session
    await connector.disconnect()

    assert client_session.exited is True
    assert connector._connected is False
    assert connector._connection is None
    assert connector._connection_manager.stopped is True
