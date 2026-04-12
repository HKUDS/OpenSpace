from __future__ import annotations

import argparse
import importlib
from types import SimpleNamespace

import pytest


ENTRYPOINT_MODULES = [
    "openspace.mcp_server",
    "openspace.evolution_mcp_server",
]


@pytest.mark.parametrize("module_name", ENTRYPOINT_MODULES)
def test_stdio_entrypoint_uses_stdio_transport(module_name, monkeypatch) -> None:
    module = importlib.import_module(module_name)
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    watchdog_calls: list[bool] = []

    monkeypatch.setattr(
        argparse.ArgumentParser,
        "parse_args",
        lambda self: SimpleNamespace(transport="stdio", port=9123),
    )
    monkeypatch.setattr(
        module.mcp,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        module,
        "_maybe_start_idle_watchdog",
        lambda: watchdog_calls.append(True),
    )

    module.run_mcp_server()

    assert watchdog_calls == [True]
    assert calls == [((), {"transport": "stdio"})]
    assert module.mcp.settings.port == 9123


@pytest.mark.parametrize("module_name", ENTRYPOINT_MODULES)
def test_sse_entrypoint_does_not_forward_sse_params(module_name, monkeypatch) -> None:
    module = importlib.import_module(module_name)
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    watchdog_calls: list[bool] = []

    monkeypatch.setattr(
        argparse.ArgumentParser,
        "parse_args",
        lambda self: SimpleNamespace(transport="sse", port=9123),
    )
    monkeypatch.setattr(
        module.mcp,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        module,
        "_maybe_start_idle_watchdog",
        lambda: watchdog_calls.append(True),
    )

    module.run_mcp_server()

    assert watchdog_calls == []
    assert calls == [((), {"transport": "sse"})]
    assert module.mcp.settings.port == 9123


@pytest.mark.parametrize("module_name", ENTRYPOINT_MODULES)
def test_streamable_http_entrypoint_uses_watchdog_for_daemon(
    module_name,
    monkeypatch,
) -> None:
    module = importlib.import_module(module_name)
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    watchdog_calls: list[bool] = []

    monkeypatch.setattr(
        argparse.ArgumentParser,
        "parse_args",
        lambda self: SimpleNamespace(transport="streamable-http", port=9234),
    )
    monkeypatch.setattr(
        module.mcp,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        module,
        "_maybe_start_idle_watchdog",
        lambda: watchdog_calls.append(True),
    )
    monkeypatch.setenv("OPENSPACE_MCP_DAEMON", "1")

    module.run_mcp_server()

    assert watchdog_calls == [True]
    assert calls == [((), {"transport": "streamable-http"})]
    assert module.mcp.settings.port == 9234
