"""Bounded post-edit diagnostics; never part of proposer generation."""

from __future__ import annotations

import math
import os
import signal
import subprocess
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO

from agentpilot.realization.base import DiagnosticResult


class CommandDiagnostics:
    def __init__(
        self,
        command: tuple[str, ...],
        *,
        timeout_seconds: float,
        max_output_bytes: int = 1_048_576,
        environment: Mapping[str, str] | None = None,
    ):
        if (
            not isinstance(command, tuple)
            or not command
            or any(
                not isinstance(item, str) or not item or "\x00" in item
                for item in command
            )
        ):
            raise ValueError("diagnostic command must be a non-empty text tuple")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0.0
            or timeout_seconds > 86_400.0
        ):
            raise ValueError(
                "diagnostic timeout must be finite, positive, and at most 86400"
            )
        if (
            isinstance(max_output_bytes, bool)
            or not isinstance(max_output_bytes, int)
            or max_output_bytes < 1_024
        ):
            raise ValueError("diagnostic max_output_bytes must be at least 1024")
        if environment is not None and (
            not isinstance(environment, Mapping)
            or any(
                not isinstance(name, str)
                or not name
                or "=" in name
                or "\x00" in name
                or not isinstance(value, str)
                or "\x00" in value
                for name, value in environment.items()
            )
        ):
            raise ValueError("diagnostic environment must be a valid text mapping")
        self.command = command
        self.timeout_seconds = float(timeout_seconds)
        self.max_output_bytes = max_output_bytes
        self.environment = dict(environment) if environment is not None else None

    @staticmethod
    def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    @staticmethod
    def _close_stream(stream: BinaryIO) -> None:
        try:
            stream.close()
        except OSError:
            pass

    def run(self, *, worktree: Path) -> DiagnosticResult:
        buffers = (bytearray(), bytearray())
        overflow = threading.Event()
        lock = threading.Lock()
        remaining = [self.max_output_bytes]
        process: subprocess.Popen[bytes] | None = None
        streams: tuple[BinaryIO, ...] = ()
        threads: list[threading.Thread] = []

        def read_bounded(stream: BinaryIO, destination: bytearray) -> None:
            read = getattr(stream, "read1", stream.read)
            while True:
                chunk = read(64 * 1024)
                if not chunk:
                    return
                with lock:
                    accepted = min(len(chunk), remaining[0])
                    destination.extend(chunk[:accepted])
                    remaining[0] -= accepted
                    if accepted != len(chunk):
                        overflow.set()

        try:
            process = subprocess.Popen(
                self.command,
                cwd=worktree,
                env=self.environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            assert process.stdout is not None and process.stderr is not None
            streams = (process.stdout, process.stderr)
            threads = [
                threading.Thread(
                    target=read_bounded,
                    args=(process.stdout, buffers[0]),
                    daemon=True,
                ),
                threading.Thread(
                    target=read_bounded,
                    args=(process.stderr, buffers[1]),
                    daemon=True,
                ),
            ]
            for thread in threads:
                thread.start()
            deadline = time.monotonic() + self.timeout_seconds
            timed_out = False
            while process.poll() is None:
                remaining_time = deadline - time.monotonic()
                if remaining_time <= 0 or overflow.is_set():
                    timed_out = remaining_time <= 0
                    self._kill_process_group(process)
                    break
                time.sleep(min(0.01, remaining_time))

            process.wait()
            # A successful leader may have left helpers holding the output pipes.
            self._kill_process_group(process)
            for thread in threads:
                thread.join(timeout=1.0)
            stdout = buffers[0].decode("utf-8", errors="replace")
            stderr = buffers[1].decode("utf-8", errors="replace")
            if overflow.is_set():
                stderr = (
                    stderr + "\ndiagnostic output exceeded its byte bound"
                ).strip()
            return DiagnosticResult(
                passed=(
                    process.returncode == 0 and not timed_out and not overflow.is_set()
                ),
                command=self.command,
                exit_code=(
                    None if timed_out or overflow.is_set() else process.returncode
                ),
                stdout=stdout,
                stderr=stderr,
                timed_out=timed_out,
                output_limit_exceeded=overflow.is_set(),
            )
        except OSError as exc:
            raise RuntimeError("cannot run diagnostic command") from exc
        finally:
            if process is not None:
                self._kill_process_group(process)
            for thread in threads:
                thread.join(timeout=1.0)
            for stream in streams:
                self._close_stream(stream)
            for thread in threads:
                thread.join(timeout=1.0)


__all__ = ["CommandDiagnostics"]
