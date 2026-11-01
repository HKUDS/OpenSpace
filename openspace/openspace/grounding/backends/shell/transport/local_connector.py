"""
Local Shell Connector — execute Python / Bash scripts directly via subprocess.

This connector runs everything in-process, removing the need for a
local_server-backed shell transport.

Return values use the canonical shell result contract consumed by ShellSession.
"""

import asyncio
import os
import platform
import signal
import subprocess
import tempfile
import uuid
from typing import Any, Optional, Dict

from openspace.grounding.core.transport.connectors.base import BaseConnector
from openspace.grounding.core.transport.task_managers.noop import NoOpConnectionManager
from openspace.grounding.core.security import SecurityPolicyManager
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

platform_name = platform.system()


def _process_group_creation_kwargs() -> dict[str, Any]:
    """Start subprocesses in a group/session so timeouts can clean descendants."""
    if platform_name == "Windows":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


async def _terminate_process_group(proc: asyncio.subprocess.Process) -> None:
    """Terminate a subprocess and its descendants when supported by the platform."""
    if proc.returncode is not None:
        return

    try:
        if platform_name != "Windows" and hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    except ProcessLookupError:
        return
    except Exception:
        logger.debug("Failed to terminate subprocess pid=%s", proc.pid, exc_info=True)

    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
        return
    except asyncio.TimeoutError:
        pass

    try:
        if platform_name != "Windows" and hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        return
    except Exception:
        logger.debug("Failed to kill subprocess pid=%s", proc.pid, exc_info=True)
        return

    await proc.wait()


async def _communicate_or_terminate(
    proc: asyncio.subprocess.Process,
    *,
    timeout: int,
) -> tuple[bytes, bytes | None]:
    try:
        return await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await _terminate_process_group(proc)
        raise
    except asyncio.CancelledError:
        await _terminate_process_group(proc)
        raise


# ---------------------------------------------------------------------------
# Conda helpers (mirrored from local_server/main.py)
# ---------------------------------------------------------------------------

def _get_conda_activation_prefix(conda_env: str | None) -> str:
    """Generate platform-specific conda activation prefix."""
    if not conda_env:
        return ""
    if platform_name == "Windows":
        conda_paths = [
            os.path.expandvars(r"%USERPROFILE%\miniconda3\Scripts\activate.bat"),
            os.path.expandvars(r"%USERPROFILE%\anaconda3\Scripts\activate.bat"),
            r"C:\ProgramData\Miniconda3\Scripts\activate.bat",
            r"C:\ProgramData\Anaconda3\Scripts\activate.bat",
        ]
        for p in conda_paths:
            if os.path.exists(p):
                return f'call "{p}" {conda_env} && '
        return f"conda activate {conda_env} && "
    else:
        conda_paths = [
            os.path.expanduser("~/miniconda3/etc/profile.d/conda.sh"),
            os.path.expanduser("~/anaconda3/etc/profile.d/conda.sh"),
            "/opt/conda/etc/profile.d/conda.sh",
            "/usr/local/miniconda3/etc/profile.d/conda.sh",
            "/usr/local/anaconda3/etc/profile.d/conda.sh",
        ]
        for p in conda_paths:
            if os.path.exists(p):
                return f'source "{p}" && conda activate {conda_env} && '
        return f"conda activate {conda_env} && "


def _wrap_script_with_conda(script: str, conda_env: str | None) -> str:
    """Wrap bash script with conda activation if needed."""
    if not conda_env:
        return script
    if platform_name == "Windows":
        prefix = _get_conda_activation_prefix(conda_env)
        return f"{prefix}{script}"
    else:
        conda_paths = [
            os.path.expanduser("~/miniconda3/etc/profile.d/conda.sh"),
            os.path.expanduser("~/anaconda3/etc/profile.d/conda.sh"),
            os.path.expanduser("~/opt/anaconda3/etc/profile.d/conda.sh"),
            "/opt/conda/etc/profile.d/conda.sh",
        ]
        conda_sh = None
        for p in conda_paths:
            if os.path.exists(p):
                conda_sh = p
                break
        if conda_sh:
            return (
                f'#!/bin/bash\n'
                f'if [ -f "{conda_sh}" ]; then\n'
                f'    . "{conda_sh}"\n'
                f'    conda activate {conda_env} 2>/dev/null || true\n'
                f'fi\n\n'
                f'{script}\n'
            )
        else:
            logger.warning(
                "Conda environment '%s' requested but conda not found. "
                "Executing with system Python.", conda_env
            )
            return script


class LocalShellConnector(BaseConnector[Any]):
    """
    Shell connector that runs scripts **locally** using asyncio subprocesses,
    bypassing the Flask local_server entirely.
    
    Public API exposes typed local execution methods used by ``ShellSession``.
    """

    def __init__(
        self,
        *,
        retry_times: int = 3,
        retry_interval: float = 5,
        security_manager: "SecurityPolicyManager | None" = None,
    ) -> None:
        super().__init__(NoOpConnectionManager())
        self.retry_times = retry_times
        self.retry_interval = retry_interval
        self._security_manager = security_manager
        # Provide base_url = None so ShellSession._get_system_info falls back
        # to bash-based detection instead of HTTP.
        self.base_url: str | None = None

    # ------------------------------------------------------------------
    # connect / disconnect (mostly no-ops for local execution)
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """No real connection to establish for local mode."""
        if self._connected:
            return
        await super().connect()
        logger.info("LocalShellConnector: ready (local mode, no server required)")

    # ------------------------------------------------------------------
    # Core execution helpers
    # ------------------------------------------------------------------

    async def _run_subprocess(
        self,
        cmd: list[str],
        *,
        timeout: int = 90,
        working_dir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> Dict[str, Any]:
        """Run a command via asyncio subprocess and return a result dict
        matching the format returned by the local_server endpoints."""
        exec_env = os.environ.copy()
        if env:
            exec_env.update(env)

        cwd = working_dir or os.getcwd()

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=exec_env,
                **_process_group_creation_kwargs(),
            )
            stdout_b, stderr_b = await _communicate_or_terminate(proc, timeout=timeout)
            stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
            stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""
            returncode = proc.returncode or 0

            return {
                "status": "success" if returncode == 0 else "error",
                "output": stdout,
                "content": stdout or "Code executed successfully (no output)",
                "error": stderr,
                "returncode": returncode,
            }
        except asyncio.TimeoutError:
            return {
                "status": "error",
                "output": f"Execution timed out after {timeout} seconds",
                "content": f"Execution timed out after {timeout} seconds",
                "error": "",
                "returncode": -1,
            }
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "content": "",
                "error": str(e),
                "returncode": -1,
            }

    async def _run_shell_command(
        self,
        shell_cmd: str,
        *,
        timeout: int = 90,
        working_dir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> Dict[str, Any]:
        """Run a shell command string (used for conda-wrapped scripts)."""
        exec_env = os.environ.copy()
        if env:
            exec_env.update(env)

        cwd = working_dir or os.getcwd()

        try:
            proc = await asyncio.create_subprocess_shell(
                shell_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
                env=exec_env,
                **_process_group_creation_kwargs(),
            )
            stdout_b, _ = await _communicate_or_terminate(proc, timeout=timeout)
            stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
            returncode = proc.returncode or 0

            return {
                "status": "success" if returncode == 0 else "error",
                "output": stdout,
                "content": stdout or "Code executed successfully (no output)",
                "error": "",
                "returncode": returncode,
            }
        except asyncio.TimeoutError:
            return {
                "status": "error",
                "output": f"Script execution timed out after {timeout} seconds",
                "content": f"Script execution timed out after {timeout} seconds",
                "error": "",
                "returncode": -1,
            }
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "content": "",
                "error": str(e),
                "returncode": -1,
            }

    # ------------------------------------------------------------------
    # Public API used by ShellSession
    # ------------------------------------------------------------------

    async def run_python_script(
        self,
        code: str,
        *,
        timeout: int = 90,
        working_dir: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        conda_env: Optional[str] = None,
    ) -> Any:
        """Execute a Python script locally.

        Return format matches the server's ``/run_python`` endpoint.
        """
        # Security check
        if self._security_manager:
            from openspace.grounding.core.types import BackendType
            allowed = await self._security_manager.check_command_allowed(
                BackendType.SHELL, code
            )
            if not allowed:
                logger.error("SecurityPolicy blocked python code execution")
                raise PermissionError("SecurityPolicy: python code execution blocked")

        # Write code to temp file (same as local_server)
        suffix = uuid.uuid4().hex
        if platform_name == "Windows":
            temp_filename = os.path.join(tempfile.gettempdir(), f"python_exec_{suffix}.py")
        else:
            temp_filename = f"/tmp/python_exec_{suffix}.py"

        try:
            with open(temp_filename, "w") as f:
                f.write(code)

            logger.info(
                "Executing python script locally with timeout=%d seconds%s%s%s",
                timeout,
                f", working_dir={working_dir}" if working_dir else "",
                f", env={list(env.keys())}" if env else "",
                f", conda_env={conda_env}" if conda_env else "",
            )

            if conda_env:
                activation = _get_conda_activation_prefix(conda_env)
                if activation:
                    python_cmd = "python" if platform_name == "Windows" else "python3"
                    full_cmd = f'{activation}{python_cmd} "{temp_filename}"'
                    result = await self._run_shell_command(
                        full_cmd, timeout=timeout, working_dir=working_dir, env=env
                    )
                else:
                    python_cmd = "python" if platform_name == "Windows" else "python3"
                    result = await self._run_subprocess(
                        [python_cmd, temp_filename],
                        timeout=timeout,
                        working_dir=working_dir,
                        env=env,
                    )
            else:
                python_cmd = "python" if platform_name == "Windows" else "python3"
                result = await self._run_subprocess(
                    [python_cmd, temp_filename],
                    timeout=timeout,
                    working_dir=working_dir,
                    env=env,
                )

            return result

        finally:
            if os.path.exists(temp_filename):
                os.remove(temp_filename)

    async def run_bash_command(
        self,
        script: str,
        *,
        timeout: int = 90,
        working_dir: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        conda_env: Optional[str] = None,
    ) -> Any:
        """Execute a Bash script locally."""
        # Security check
        if self._security_manager:
            from openspace.grounding.core.types import BackendType
            allowed = await self._security_manager.check_command_allowed(
                BackendType.SHELL, script
            )
            if not allowed:
                logger.error("SecurityPolicy blocked bash script execution")
                raise PermissionError("SecurityPolicy: bash script execution blocked")

        # Wrap with conda if needed
        final_script = _wrap_script_with_conda(script, conda_env)

        # Write to temp file (same as local_server)
        suffix = uuid.uuid4().hex
        if platform_name == "Windows":
            temp_filename = os.path.join(tempfile.gettempdir(), f"bash_exec_{suffix}.sh")
        else:
            temp_filename = f"/tmp/bash_exec_{suffix}.sh"

        try:
            with open(temp_filename, "w") as f:
                f.write(final_script)
            os.chmod(temp_filename, 0o755)

            logger.info(
                "Executing bash script locally with timeout=%d seconds%s%s%s",
                timeout,
                f", working_dir={working_dir}" if working_dir else "",
                f", env={list(env.keys())}" if env else "",
                f", conda_env={conda_env}" if conda_env else "",
            )

            shell_cmd = ["bash", temp_filename] if platform_name == "Windows" else ["/bin/bash", temp_filename]
            result = await self._run_subprocess(
                shell_cmd,
                timeout=timeout,
                working_dir=working_dir,
                env=env,
            )
            return result

        finally:
            if os.path.exists(temp_filename):
                os.unlink(temp_filename)

    async def run_command_argv(
        self,
        argv: list[str],
        *,
        timeout: int = 90,
        working_dir: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        security_command: str | None = None,
    ) -> Any:
        """Execute an already-wrapped argv locally.

        Used by the process sandbox runtime. The caller has already decided
        shell quoting and sandbox policy, so this path uses subprocess argv
        execution rather than ``create_subprocess_shell``.
        """

        if self._security_manager:
            from openspace.grounding.core.types import BackendType

            allowed = await self._security_manager.check_command_allowed(
                BackendType.SHELL, security_command or " ".join(argv)
            )
            if not allowed:
                logger.error("SecurityPolicy blocked argv command execution")
                raise PermissionError("SecurityPolicy: argv command execution blocked")

        logger.info(
            "Executing argv command locally with timeout=%d seconds%s%s",
            timeout,
            f", working_dir={working_dir}" if working_dir else "",
            f", env={list(env.keys())}" if env else "",
        )
        return await self._run_subprocess(
            list(argv),
            timeout=timeout,
            working_dir=working_dir,
            env=env,
        )

    # ------------------------------------------------------------------
    # BaseConnector abstract methods
    # ------------------------------------------------------------------

    async def invoke(self, name: str, params: dict[str, Any]) -> Any:
        """Reject generic RPC dispatch for local shell execution."""
        del params
        raise NotImplementedError(
            f"LocalShellConnector does not support endpoint dispatch: {name}"
        )

    async def request(self, *args: Any, **kwargs: Any) -> Any:
        """Not used in local mode."""
        raise NotImplementedError(
            "LocalShellConnector does not support raw HTTP requests"
        )
