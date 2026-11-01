"""Minimal CLI bootstrap that contains import-time application failures."""

from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Callable


_ERROR_PAYLOAD = {
    "error": {
        "code": "internal_error",
        "message": "the runtime failed unexpectedly",
    },
}


def _load_cli_main() -> Callable[[], int]:
    module = importlib.import_module("agentpilot.app.cli")
    entrypoint = getattr(module, "main")
    if not callable(entrypoint):
        raise TypeError("agentpilot.app.cli.main must be callable")
    return entrypoint


def main() -> int:
    try:
        cli_main = _load_cli_main()
    except Exception:
        print(json.dumps(_ERROR_PAYLOAD, sort_keys=True), file=sys.stderr)
        return 1
    return cli_main()


__all__ = ["main"]
