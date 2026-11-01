"""Default subprocess entrypoint for skill behavior eval.

The worker reads ``OPENSPACE_REPLAY_CONTEXT`` and emits one JSON replay result
to stdout. It intentionally does not approve static-only checks.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from .behavior_eval import evaluate_replay_context


def main(argv: list[str] | None = None) -> int:
    del argv
    context_path = os.environ.get("OPENSPACE_REPLAY_CONTEXT", "").strip()
    if not context_path:
        return _emit(
            {
                "passed": False,
                "failures": ["missing_replay_context"],
                "runner": "default_eval_worker",
            },
            status=2,
        )
    try:
        context = json.loads(Path(context_path).read_text(encoding="utf-8"))
    except Exception as exc:
        return _emit(
            {
                "passed": False,
                "failures": [f"invalid_replay_context:{str(exc)[:300]}"],
                "runner": "default_eval_worker",
            },
            status=2,
        )
    if not isinstance(context, dict):
        return _emit(
            {
                "passed": False,
                "failures": ["replay_context_not_object"],
                "runner": "default_eval_worker",
            },
            status=2,
        )
    result = evaluate_replay_context(context)
    result.setdefault("runner", "default_eval_worker")
    return _emit(result, status=0)


def _emit(payload: dict[str, Any], *, status: int) -> int:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str))
    sys.stdout.write("\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
