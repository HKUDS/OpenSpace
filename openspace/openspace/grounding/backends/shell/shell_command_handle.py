from __future__ import annotations

import asyncio
import os
import signal
import shutil
from pathlib import Path
from typing import Any, Awaitable, Callable

from openspace.agents.shell_task import ShellTaskResult


class BackgroundShellStatus:
    RUNNING = "running"
    BACKGROUNDED = "backgrounded"
    COMPLETED = "completed"
    KILLED = "killed"


class BackgroundShellHandle:
    """Python counterpart of OpenSpace's ShellCommand for local bash tasks."""

    def __init__(
        self,
        *,
        task_id: str,
        command: str,
        process: asyncio.subprocess.Process,
        output_path: Path,
        drain_task: asyncio.Task[Any],
        cleanup_callbacks: list[Callable[[], Awaitable[None] | None]] | None = None,
        output_transform: Callable[[str], str] | None = None,
    ) -> None:
        self.task_id = task_id
        self.command = command
        self.process = process
        self.output_path = output_path
        self.pid = process.pid
        self._drain_task = drain_task
        self._cleanup_callbacks = cleanup_callbacks or []
        self._output_transform = output_transform
        self._result: asyncio.Future[ShellTaskResult] = asyncio.get_running_loop().create_future()
        self._backgrounded = False
        self._interrupted = False

    @classmethod
    async def spawn(
        cls,
        command: str,
        *,
        task_id: str,
        output_path: str | Path,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        conda_env: str | None = None,
        argv: list[str] | None = None,
        cleanup_callbacks: list[Callable[[], Awaitable[None] | None]] | None = None,
        output_transform: Callable[[str], str] | None = None,
    ) -> "BackgroundShellHandle":
        from openspace.grounding.backends.shell.transport.local_connector import (
            _wrap_script_with_conda,
        )

        merged_env = os.environ.copy()
        if env:
            merged_env.update({str(k): str(v) for k, v in env.items()})
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        if argv is not None:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd or None,
                env=merged_env,
                start_new_session=True,
            )
        else:
            final_command = _wrap_script_with_conda(command, conda_env)
            bash_path = shutil.which("bash", path=merged_env.get("PATH"))
            if bash_path:
                process = await asyncio.create_subprocess_exec(
                    bash_path,
                    "-lc",
                    final_command,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=cwd or None,
                    env=merged_env,
                    start_new_session=True,
                )
            else:
                process = await asyncio.create_subprocess_shell(
                    final_command,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=cwd or None,
                    env=merged_env,
                    start_new_session=True,
                )
        assert process.stdout is not None
        drain_task = asyncio.create_task(cls._drain_stream(process.stdout, path))
        handle = cls(
            task_id=task_id,
            command=command,
            process=process,
            output_path=path,
            drain_task=drain_task,
            cleanup_callbacks=cleanup_callbacks,
            output_transform=output_transform,
        )
        asyncio.create_task(handle._watch_process())
        return handle

    @property
    def result(self) -> asyncio.Future[ShellTaskResult]:
        return self._result

    @property
    def status(self) -> str:
        if self._backgrounded and self.process.returncode is None:
            return BackgroundShellStatus.BACKGROUNDED
        if self.process.returncode is None:
            return BackgroundShellStatus.RUNNING
        if self._interrupted:
            return BackgroundShellStatus.KILLED
        return BackgroundShellStatus.COMPLETED

    @property
    def is_backgrounded(self) -> bool:
        return self._backgrounded

    def background(self, task_id: str | None = None) -> bool:
        if self._backgrounded:
            return False
        self._backgrounded = True
        if task_id:
            self.task_id = task_id
        return True

    async def kill(self, signal_name: str = "TERM") -> None:
        if self.process.returncode is not None:
            return
        self._interrupted = True
        sig = signal.SIGKILL if str(signal_name).upper() == "KILL" else signal.SIGTERM
        try:
            if hasattr(os, "killpg"):
                os.killpg(self.process.pid, sig)
            else:
                self.process.send_signal(sig)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(self.process.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            if sig != signal.SIGKILL:
                try:
                    if hasattr(os, "killpg"):
                        os.killpg(self.process.pid, signal.SIGKILL)
                    else:
                        self.process.kill()
                except ProcessLookupError:
                    pass
                await self.process.wait()

    async def cleanup(self) -> None:
        if not self._drain_task.done():
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass

    async def _watch_process(self) -> None:
        code = 1
        try:
            code = await self.process.wait()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass
        finally:
            await self._transform_output_file()
            await self._run_cleanup_callbacks()
            if not self._result.done():
                self._result.set_result(
                    ShellTaskResult(code=code, interrupted=self._interrupted)
                )

    @staticmethod
    async def _drain_stream(stream: asyncio.StreamReader, output_path: Path) -> None:
        with output_path.open("ab") as output_file:
            while True:
                chunk = await stream.read(8192)
                if not chunk:
                    break
                output_file.write(chunk)
                output_file.flush()

    async def _transform_output_file(self) -> None:
        if self._output_transform is None:
            return
        try:
            original = self.output_path.read_text(encoding="utf-8", errors="replace")
            transformed = self._output_transform(original)
            if transformed != original:
                self.output_path.write_text(
                    transformed,
                    encoding="utf-8",
                    errors="replace",
                )
        except OSError:
            return

    async def _run_cleanup_callbacks(self) -> None:
        callbacks = list(self._cleanup_callbacks)
        self._cleanup_callbacks.clear()
        for callback in callbacks:
            result = callback()
            if asyncio.iscoroutine(result):
                await result


__all__ = ["BackgroundShellHandle", "BackgroundShellStatus"]
