"""OpenSpace cloud platform authentication.

Resolution order for OPENSPACE_API_KEY:
  1. ``OPENSPACE_API_KEY`` env var
  2. Auto-detect from host agent config (MCP env block)
  3. Empty (caller treats as "not configured").

Base URL resolution:
  1. ``OPENSPACE_API_BASE`` env var
  2. Default: ``https://open-space.cloud/api/v1``
"""

from __future__ import annotations

import logging
import os
from typing import Dict, Optional

logger = logging.getLogger("openspace.cloud")

OPENSPACE_DEFAULT_BASE = "https://open-space.cloud/api/v1"


def get_openspace_auth() -> tuple[Dict[str, str], str]:
    """Resolve OpenSpace credentials and base URL.

    Returns:
        ``(auth_headers, api_base)`` — headers dict ready for HTTP requests
        and the API base URL.  If no credentials are found, ``auth_headers``
        is empty.
    """
    from openspace.host_detection import read_host_mcp_env

    auth_headers: Dict[str, str] = {}
    api_base = OPENSPACE_DEFAULT_BASE

    # Tier 1: env vars
    env_key = os.environ.get("OPENSPACE_API_KEY", "").strip()
    env_base = os.environ.get("OPENSPACE_API_BASE", "").strip()

    if env_key:
        auth_headers["X-API-Key"] = env_key
        if env_base:
            api_base = env_base.rstrip("/")
        logger.info("OpenSpace auth: using OPENSPACE_API_KEY env var")
        return auth_headers, api_base

    # Tier 2: host agent config MCP env block
    mcp_env = read_host_mcp_env()
    cfg_key = str(mcp_env.get("OPENSPACE_API_KEY", "")).strip()
    cfg_base = str(mcp_env.get("OPENSPACE_API_BASE", "")).strip()

    if cfg_key:
        auth_headers["X-API-Key"] = cfg_key
        if cfg_base:
            api_base = cfg_base.rstrip("/")
        logger.info("OpenSpace auth: using OPENSPACE_API_KEY from host agent MCP env config")
        return auth_headers, api_base

    return auth_headers, api_base


def get_api_base(cli_override: Optional[str] = None) -> str:
    """Resolve OpenSpace API base URL (for CLI scripts).

    Priority: ``cli_override`` → env var → host agent config → default.
    """
    from openspace.host_detection import read_host_mcp_env

    if cli_override:
        return cli_override.rstrip("/")
    env_base = os.environ.get("OPENSPACE_API_BASE", "").strip()
    if env_base:
        return env_base.rstrip("/")
    mcp_env = read_host_mcp_env()
    cfg_base = str(mcp_env.get("OPENSPACE_API_BASE", "")).strip()
    if cfg_base:
        return cfg_base.rstrip("/")
    return OPENSPACE_DEFAULT_BASE


def get_auth_headers_or_exit() -> Dict[str, str]:
    """Resolve auth headers for CLI scripts.  Exits on failure."""
    import sys
    from openspace.host_detection import read_host_mcp_env

    env_key = os.environ.get("OPENSPACE_API_KEY", "").strip()
    if env_key:
        return {"X-API-Key": env_key}

    mcp_env = read_host_mcp_env()
    cfg_key = str(mcp_env.get("OPENSPACE_API_KEY", "")).strip()
    if cfg_key:
        return {"X-API-Key": cfg_key}

    print(
        "ERROR: No OPENSPACE_API_KEY configured.\n"
        "  Register at https://open-space.cloud to obtain a key, then add it to\n"
        "  your host agent config in the OpenSpace MCP env block.",
        file=sys.stderr,
    )
    sys.exit(1)
