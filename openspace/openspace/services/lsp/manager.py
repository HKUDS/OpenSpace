from __future__ import annotations

import asyncio
import os
from typing import Literal

from openspace.services.lsp.passive_feedback import register_lsp_notification_handlers
from openspace.services.lsp.server_manager import LSPServerManager
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

InitializationState = Literal["not-started", "pending", "success", "failed"]

_lsp_manager: LSPServerManager | None = None
_initialization_state: InitializationState = "not-started"
_initialization_error: Exception | None = None
_initialization_generation = 0
_initialization_task: asyncio.Task[None] | None = None


def get_lsp_server_manager() -> LSPServerManager | None:
    if _initialization_state == "failed":
        return None
    return _lsp_manager


def get_initialization_status() -> dict[str, object]:
    if _initialization_state == "failed":
        return {"status": "failed", "error": _initialization_error or RuntimeError("Initialization failed")}
    return {"status": _initialization_state}


def is_lsp_connected() -> bool:
    manager = get_lsp_server_manager()
    if manager is None:
        return False
    servers = manager.get_all_servers()
    return bool(servers) and any(server.state != "error" for server in servers.values())


async def wait_for_initialization() -> None:
    task = _initialization_task
    if _initialization_state == "pending" and task is not None:
        await task


def initialize_lsp_server_manager(*, cwd: str | None = None, bare: bool | None = None) -> None:
    """Create the manager and initialize configs in the background.

    This mirrors OpenSpace ``initializeLspServerManager``: no server process is spawned
    here; configured servers are instantiated only and start lazily on use.
    """

    global _lsp_manager, _initialization_state, _initialization_error
    global _initialization_generation, _initialization_task

    if bare is None:
        bare = os.environ.get("OPENSPACE_BARE") == "1" or os.environ.get("OPENSPACE_HEADLESS") == "1"
    if bare:
        return
    if _lsp_manager is not None and _initialization_state != "failed":
        return

    if _initialization_state == "failed":
        _lsp_manager = None
        _initialization_error = None

    manager = LSPServerManager(cwd=cwd)
    _lsp_manager = manager
    _initialization_state = "pending"
    _initialization_generation += 1
    generation = _initialization_generation

    async def _initialize() -> None:
        global _lsp_manager, _initialization_state, _initialization_error
        try:
            await manager.initialize()
            if generation == _initialization_generation:
                _initialization_state = "success"
                register_lsp_notification_handlers(manager)
        except Exception as exc:
            if generation == _initialization_generation:
                _initialization_state = "failed"
                _initialization_error = exc
                _lsp_manager = None
                logger.debug("Failed to initialize LSP manager: %s", exc)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # CLI/tests can instantiate the module outside a loop.  Leave manager in
        # not-started state; the agent runtime calls this again inside a loop.
        _lsp_manager = None
        _initialization_state = "not-started"
        return
    _initialization_task = loop.create_task(_initialize())


def reinitialize_lsp_server_manager(*, cwd: str | None = None) -> None:
    global _lsp_manager, _initialization_state, _initialization_error, _initialization_generation
    if _initialization_state == "not-started":
        return
    old = _lsp_manager
    if old is not None:
        try:
            asyncio.get_running_loop().create_task(old.shutdown())
        except Exception:
            pass
    _lsp_manager = None
    _initialization_state = "not-started"
    _initialization_error = None
    _initialization_generation += 1
    initialize_lsp_server_manager(cwd=cwd)


async def shutdown_lsp_server_manager() -> None:
    global _lsp_manager, _initialization_state, _initialization_error, _initialization_task
    manager = _lsp_manager
    try:
        if manager is not None:
            await manager.shutdown()
    except Exception as exc:
        logger.debug("Failed to shutdown LSP manager: %s", exc)
    finally:
        _lsp_manager = None
        _initialization_state = "not-started"
        _initialization_error = None
        _initialization_task = None


def _reset_lsp_manager_for_testing() -> None:
    global _lsp_manager, _initialization_state, _initialization_error, _initialization_generation, _initialization_task
    _lsp_manager = None
    _initialization_state = "not-started"
    _initialization_error = None
    _initialization_generation += 1
    _initialization_task = None
