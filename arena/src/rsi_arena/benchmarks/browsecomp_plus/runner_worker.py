"""Process-isolated worker for a configured BrowseComp Python runner."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import inspect
import json
import os
import stat
import sys
from collections.abc import Mapping
from pathlib import Path

from rsi_arena.benchmarks.harbor_runtime.agent_worker import AgentExecutionContext
from rsi_arena.contracts import canonical_digest

from .runtime import BrowseCompTaskEnvironment
from .search_transport import BrowseCompSearchClient
from .usage import USAGE_SIDECAR_ENV


def _object(path: Path) -> dict[str, object]:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("BrowseComp runner request is invalid")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("BrowseComp runner request is invalid")
    return value


def _resolve(value: object) -> object:
    if not inspect.isawaitable(value):
        return value
    return asyncio.run(value)


def _file_digest(path: Path) -> str:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("BrowseComp runner source is invalid")
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _closure_digest(root: Path) -> str:
    info = root.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("BrowseComp runner closure is invalid")
    entries = []
    for child in sorted(
        root.rglob("*.py"), key=lambda item: item.relative_to(root).as_posix()
    ):
        child_info = child.lstat()
        if stat.S_ISLNK(child_info.st_mode) or not stat.S_ISREG(child_info.st_mode):
            raise RuntimeError("BrowseComp runner closure is invalid")
        entries.append(
            {
                "path": child.relative_to(root).as_posix(),
                "mode": stat.S_IMODE(child_info.st_mode),
                "sha256": _file_digest(child),
            }
        )
    if not entries:
        raise RuntimeError("BrowseComp runner closure is empty")
    return "sha256:" + canonical_digest(entries)


def _verified_runner(module_name: str, callable_name: str, identity: object):
    if not isinstance(identity, Mapping) or set(identity) != {
        "ref",
        "module_path",
        "module_sha256",
        "callable_sha256",
        "closure_root",
        "closure_sha256",
    }:
        raise RuntimeError("BrowseComp runner identity is invalid")
    spec = importlib.util.find_spec(module_name)
    if spec is None or spec.origin is None:
        raise RuntimeError("BrowseComp runner module is unavailable")
    source_path = Path(spec.origin).resolve(strict=True)
    closure_root = Path(str(identity["closure_root"])).resolve(strict=True)
    if (
        str(source_path) != identity["module_path"]
        or _file_digest(source_path) != identity["module_sha256"]
        or _closure_digest(closure_root) != identity["closure_sha256"]
        or source_path.parent != closure_root
    ):
        raise RuntimeError("BrowseComp runner source changed after freeze")
    runner = getattr(importlib.import_module(module_name), callable_name)
    try:
        callable_digest = "sha256:" + canonical_digest(inspect.getsource(runner))
    except (OSError, TypeError) as exc:
        raise RuntimeError("BrowseComp runner callable source is unavailable") from exc
    if callable_digest != identity["callable_sha256"]:
        raise RuntimeError("BrowseComp runner callable changed after freeze")
    return runner


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 2:
        return 2
    request_path = Path(arguments[0]).resolve(strict=True)
    result_path = Path(arguments[1]).absolute()
    request = _object(request_path)
    required = {
        "runner_module",
        "runner_callable",
        "candidate_checkout",
        "agent_ref",
        "model_runtime",
        "config",
        "benchmark_id",
        "instruction",
        "search_endpoint",
        "usage_sidecar",
        "runner_identity",
    }
    if set(request) != required:
        raise RuntimeError("BrowseComp runner request fields are invalid")
    usage_sidecar = Path(str(request["usage_sidecar"])).absolute()
    os.environ[USAGE_SIDECAR_ENV] = str(usage_sidecar)
    runner = _verified_runner(
        str(request["runner_module"]),
        str(request["runner_callable"]),
        request["runner_identity"],
    )
    if not callable(runner):
        raise RuntimeError("BrowseComp configured runner is not callable")
    entrypoint = runner(
        candidate_source=Path(str(request["candidate_checkout"])),
        agent_ref=request["agent_ref"],
        model_runtime=request["model_runtime"],
        config=request["config"],
    )
    if not callable(entrypoint):
        raise RuntimeError("BrowseComp configured runner returned no callable")
    context = AgentExecutionContext(
        benchmark_id=str(request["benchmark_id"]),
        instruction=str(request["instruction"]),
        environment=BrowseCompTaskEnvironment(
            BrowseCompSearchClient(str(request["search_endpoint"]))
        ),
    )
    result = _resolve(
        entrypoint(
            context=context,
            model_runtime=request["model_runtime"],
            config=request["config"],
        )
    )
    if not isinstance(result, Mapping):
        raise RuntimeError("BrowseComp configured runner result is not an object")
    encoded = json.dumps(
        {"ok": True, "result": dict(result)},
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > 10 * 1024 * 1024:
        raise RuntimeError("BrowseComp configured runner result is too large")
    descriptor = os.open(
        result_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as output:
        output.write(encoded)
        output.flush()
        os.fsync(output.fileno())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
