"""Shared path and config-name constants for OpenSpace.

``PROJECT_ROOT`` is the directory that *contains* the ``openspace`` package.
In a source checkout that is the repository root; after a normal ``pip install``
it resolves to ``site-packages``.  Mutable runtime state must never be written
there — use :func:`get_data_home` / :func:`get_default_db_path` instead.
"""

from __future__ import annotations

import os
from pathlib import Path

CONFIG_GROUNDING = "config_grounding.json"
CONFIG_SECURITY = "config_security.json"
CONFIG_MCP = "config_mcp.json"
CONFIG_DEV = "config_dev.json"
CONFIG_AGENTS = "config_agents.json"

LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# Directory that contains the ``openspace`` package (repo root or site-packages).
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Package directory itself (``.../openspace``).
PACKAGE_ROOT = Path(__file__).resolve().parent.parent

_DATA_HOME_ENV = "OPENSPACE_HOME"
_DATA_DIR_ENV = "OPENSPACE_DATA_DIR"


def is_source_checkout(root: Path | None = None) -> bool:
    """Return True when *root* looks like a git/source checkout of OpenSpace.

    Editable installs keep writing under the repo ``.openspace/`` directory.
    Wheel/site-package installs do not — those must use a user data home.
    """
    candidate = Path(root) if root is not None else PROJECT_ROOT
    return (candidate / "pyproject.toml").is_file() and (
        candidate / "openspace" / "__init__.py"
    ).is_file()


def get_data_home(*, create: bool = False) -> Path:
    """Resolve the writable OpenSpace data directory.

    Resolution order:
      1. ``OPENSPACE_HOME`` — explicit data home override
      2. ``OPENSPACE_DATA_DIR`` — alias for the same override
      3. ``<repo>/.openspace`` when running from a source/editable checkout
      4. ``~/.openspace`` for installed (site-packages) environments

    The returned path is the data home itself (already named ``.openspace`` or
    an explicit override). Callers should put databases/caches directly under it
    (for example ``get_data_home() / "openspace.db"``).
    """
    for env_name in (_DATA_HOME_ENV, _DATA_DIR_ENV):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            path = Path(raw).expanduser().resolve()
            if create:
                path.mkdir(parents=True, exist_ok=True)
            return path

    if is_source_checkout():
        path = (PROJECT_ROOT / ".openspace").resolve()
    else:
        path = (Path.home() / ".openspace").resolve()

    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def get_default_db_path(*, create: bool = True) -> Path:
    """Default shared SQLite path (``openspace.db`` under the data home)."""
    db_dir = get_data_home(create=create)
    return db_dir / "openspace.db"


def get_cache_dir(name: str, *, create: bool = False) -> Path:
    """Return a named cache directory under the data home."""
    path = get_data_home(create=create) / name
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def get_log_dir(*, create: bool = True) -> Path:
    """Writable log directory (never under site-packages)."""
    path = get_data_home(create=create) / "logs"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


__all__ = [
    "CONFIG_GROUNDING",
    "CONFIG_SECURITY",
    "CONFIG_MCP",
    "CONFIG_DEV",
    "CONFIG_AGENTS",
    "LOG_LEVELS",
    "PROJECT_ROOT",
    "PACKAGE_ROOT",
    "is_source_checkout",
    "get_data_home",
    "get_default_db_path",
    "get_cache_dir",
    "get_log_dir",
]
