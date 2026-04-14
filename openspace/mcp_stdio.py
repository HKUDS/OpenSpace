from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TextIO


_STDERR_CAPTURE_HANDLE: TextIO | None = None


def maybe_redirect_stderr_to_file(log_dir: Path, filename: str) -> TextIO | None:
    """Redirect stderr to a log file when running as a non-interactive MCP child.

    Codex and similar MCP hosts typically do not surface or continuously drain
    child-process stderr. Leaving verbose transport logs attached to a pipe can
    back up the buffer and stall stdio tool calls. For interactive terminals we
    keep stderr unchanged so local debugging still behaves normally.
    """
    global _STDERR_CAPTURE_HANDLE

    if os.environ.get("OPENSPACE_MCP_CAPTURE_STDERR", "").strip().lower() in {
        "0",
        "false",
        "no",
    }:
        return None

    if _STDERR_CAPTURE_HANDLE is not None:
        return _STDERR_CAPTURE_HANDLE

    try:
        if sys.stderr is not None and sys.stderr.isatty():
            return None
    except Exception:
        pass

    log_dir.mkdir(parents=True, exist_ok=True)
    handle = (log_dir / filename).open("a", encoding="utf-8", buffering=1)
    sys.stderr = handle
    _STDERR_CAPTURE_HANDLE = handle
    return handle
