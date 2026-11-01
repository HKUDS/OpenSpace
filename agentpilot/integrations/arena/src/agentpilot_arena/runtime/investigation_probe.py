"""Durable, OS-isolated stdlib probes for visible investigation evidence."""

# Persistent format and evaluation identifiers retain their historical values.

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from agentpilot.realization import CommandDiagnostics

_MAX_CODE_BYTES = 65536
_MAX_OUTPUT_BYTES = 65536
_LIMITS = {
    "cpu_seconds": 300,
    "address_space_bytes": 1024 * 1024 * 1024,
    "outer_seconds": 600,
    "output_bytes": _MAX_OUTPUT_BYTES,
    "process_limit": 1,
    "file_size_bytes": 1024 * 1024,
}
_BOOTSTRAP = """import resource
resource.setrlimit(resource.RLIMIT_CPU, (300, 300))
resource.setrlimit(resource.RLIMIT_AS, (1024 * 1024 * 1024, 1024 * 1024 * 1024))
resource.setrlimit(resource.RLIMIT_NPROC, (1, 1))
resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
with open('/work/probe.py', encoding='utf-8') as source:
    code = compile(source.read(), '/work/probe.py', 'exec')
exec(code, {'__name__': '__main__', '__file__': '/work/probe.py'})
"""
_RESULT_KEYS = {
    "probe_id",
    "code_hash",
    "code",
    "stdout",
    "stderr",
    "passed",
    "exit_code",
    "timed_out",
    "output_limit_exceeded",
}


def _atomic_write(path: Path, content: str) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
            prefix=f".{path.name}.",
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    content = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    _atomic_write(path, content)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"invalid investigation probe journal: {path.name}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"invalid investigation probe journal: {path.name}")
    return value


class BwrapInvestigationProbe:
    """Run caller-identified probes once; replay their exact persisted payload.

    The trusted caller chooses the root and probe ID and supplies only visible
    A/source-derived code. The sandbox cannot enforce that provenance itself.
    A claimed but incomplete probe requires operator recovery, never an implicit
    rerun that could replace nondeterministic evidence in a later paid request.
    """

    def __init__(self, root: Path, bwrap_path: Path = Path("/usr/bin/bwrap")) -> None:
        self.root = Path(root).resolve()
        self.bwrap = Path(bwrap_path).resolve()
        self.python = Path(sys.executable).resolve()
        self.runtime = self.python.parent.parent

    def run(self, *, probe_id: str, code: str) -> Mapping[str, Any]:
        if not isinstance(probe_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", probe_id
        ):
            raise ValueError("probe_id must be a caller-generated safe path component")
        if not isinstance(code, str) or not code.strip():
            raise ValueError("probe code must be nonempty UTF-8 text")
        try:
            encoded = code.encode("utf-8")
        except UnicodeError as exc:
            raise ValueError("probe code must be valid UTF-8 text") from exc
        if len(encoded) > _MAX_CODE_BYTES:
            raise ValueError("probe code must be at most 8192 UTF-8 bytes")

        request = {
            "probe_id": probe_id,
            "code_hash": hashlib.sha256(encoded).hexdigest(),
            "code": code,
        }
        self.root.mkdir(parents=True, exist_ok=True)
        directory = self.root / probe_id
        if directory.is_symlink():
            raise ValueError("probe directory must not be a symlink")
        directory.mkdir(exist_ok=True)
        with (directory / ".lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            request_path = directory / "request.json"
            result_path = directory / "result.json"
            if request_path.exists():
                if _read_json(request_path) != request:
                    raise ValueError("probe_id already belongs to different code")
                if not result_path.exists():
                    raise RuntimeError(
                        "probe is incomplete; explicit recovery is required"
                    )
                result = _read_json(result_path)
                self._validate_result(result, request)
                return result
            if result_path.exists():
                raise RuntimeError("probe result has no matching request journal")
            if not self.bwrap.is_file():
                raise RuntimeError(
                    "bubblewrap is unavailable; an unrestricted fallback is forbidden"
                )

            _write_json(request_path, request)
            _atomic_write(directory / "probe.py", code)
            result, audit = self._execute(request)
            self._validate_result(result, request)
            _write_json(directory / "audit.json", audit)
            # This is the completion marker. Nothing nondeterministic is added
            # to the returned payload, either now or when this file is replayed.
            _write_json(result_path, result)
            return result

    @staticmethod
    def _validate_result(result: Mapping[str, Any], request: Mapping[str, Any]) -> None:
        valid = (
            set(result) == _RESULT_KEYS
            and all(result.get(key) == value for key, value in request.items())
            and all(isinstance(result.get(key), str) for key in ("stdout", "stderr"))
            and all(
                type(result.get(key)) is bool
                for key in ("passed", "timed_out", "output_limit_exceeded")
            )
            and (
                result.get("exit_code") is None or type(result.get("exit_code")) is int
            )
        )
        if not valid:
            raise RuntimeError("invalid or mismatched investigation probe result")
        output_bytes = sum(
            len(result[key].encode("utf-8")) for key in ("stdout", "stderr")
        )
        if output_bytes > _MAX_OUTPUT_BYTES:
            raise RuntimeError("persisted probe output exceeds its byte limit")

    def _execute(
        self, request: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        started = time.time()
        with tempfile.TemporaryDirectory(
            prefix="agentpilot-investigation-probe-"
        ) as folder:
            sandbox = Path(folder)
            work = sandbox / "work"
            work.mkdir()
            empty = sandbox / "empty"
            empty.mkdir()
            (work / "probe.py").write_text(request["code"], encoding="utf-8")
            (work / "bootstrap.py").write_text(_BOOTSTRAP, encoding="utf-8")
            stdlib = (
                f"/python/lib/python{sys.version_info.major}.{sys.version_info.minor}"
            )
            command = (
                str(self.bwrap),
                "--unshare-all",
                "--die-with-parent",
                "--new-session",
                "--cap-drop",
                "ALL",
                "--ro-bind",
                str(self.runtime),
                "/python",
                "--ro-bind",
                str(empty),
                stdlib + "/site-packages",
                "--ro-bind",
                str(empty),
                stdlib + "/ensurepip/_bundled",
                "--ro-bind",
                "/lib/x86_64-linux-gnu",
                "/lib/x86_64-linux-gnu",
                "--ro-bind",
                "/lib64",
                "/lib64",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--tmpfs",
                "/tmp",
                "--bind",
                str(work),
                "/work",
                "--chdir",
                "/work",
                "--setenv",
                "PATH",
                "/python/bin",
                "--setenv",
                "LANG",
                "C.UTF-8",
                "/python/bin/" + self.python.name,
                "-I",
                "-S",
                "-B",
                "/work/bootstrap.py",
            )
            diagnostic = CommandDiagnostics(
                command,
                timeout_seconds=_LIMITS["outer_seconds"],
                max_output_bytes=_MAX_OUTPUT_BYTES,
                environment={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            ).run(worktree=sandbox)
            audit = {
                "schema": "openthink.investigation-probe-audit.v1",
                "probe_id": request["probe_id"],
                "code_hash": request["code_hash"],
                "started_at_unix": started,
                "finished_at_unix": time.time(),
                "sandbox_path": folder,
                "limits": dict(_LIMITS),
                "diagnostic": asdict(diagnostic),
            }

            # Setup errors can contain mount-source paths. Keep the original
            # diagnostic in audit while keeping these paths out of model input.
            def model_text(value: str) -> str:
                for source, replacement in (
                    (folder, "/sandbox"),
                    (str(self.runtime), "/python"),
                    (str(self.root), "/probe-audit"),
                    (str(self.bwrap), "/usr/bin/bwrap"),
                ):
                    value = value.replace(source, replacement)
                return value

            stdout = (
                model_text(diagnostic.stdout)
                .encode("utf-8")[:_MAX_OUTPUT_BYTES]
                .decode("utf-8", errors="ignore")
            )
            remaining = _MAX_OUTPUT_BYTES - len(stdout.encode("utf-8"))
            stderr = (
                model_text(diagnostic.stderr)
                .encode("utf-8")[:remaining]
                .decode("utf-8", errors="ignore")
            )
            result = dict(request) | {
                "stdout": stdout,
                "stderr": stderr,
                "passed": diagnostic.passed,
                "exit_code": diagnostic.exit_code,
                "timed_out": diagnostic.timed_out,
                "output_limit_exceeded": diagnostic.output_limit_exceeded,
            }
        return result, audit


__all__ = ["BwrapInvestigationProbe"]
