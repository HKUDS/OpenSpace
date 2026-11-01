"""
Local Shell Connector — execute Python / Bash scripts directly via subprocess.

This connector has the **same public API** as ShellConnector (HTTP version)
but runs everything in-process, removing the need for a local_server.

Return format is kept identical so that ShellSession / ShellAgentTool
work without any changes.
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


def _subprocess_group_kwargs() -> Dict[str, Any]:
    """Return platform-specific kwargs that allow timeout cleanup."""
    if platform_name == "Windows":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": creationflags} if creationflags else {}
    return {"start_new_session": True}


async def _wait_for_exit(proc: asyncio.subprocess.Process, timeout: float) -> bool:
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


async def _kill_single_process(proc: asyncio.subprocess.Process) -> bool:
    """Fallback cleanup for platforms where process-tree cleanup is unavailable."""
    for action in (proc.terminate, proc.kill):
        if proc.returncode is not None:
            return True
        try:
            action()
        except ProcessLookupError:
            return True
        except Exception as e:
            logger.warning("Process cleanup action failed: %s", e)
        if await _wait_for_exit(proc, timeout=5):
            return True
    return proc.returncode is not None


async def _kill_windows_process_tree(proc: asyncio.subprocess.Process, pid: int) -> bool:
    """Best-effort Windows process-tree cleanup."""
    try:
        taskkill = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(taskkill.wait(), timeout=5)
    except (FileNotFoundError, PermissionError, OSError, asyncio.TimeoutError) as e:
        logger.warning("taskkill process-tree cleanup failed: %s", e)

    if await _wait_for_exit(proc, timeout=1):
        return True
    return await _kill_single_process(proc)


async def _kill_posix_process_tree(proc: asyncio.subprocess.Process, pid: int) -> bool:
    """Send SIGTERM, then SIGKILL, to the POSIX process group."""
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError as e:
        logger.warning("SIGTERM to process group failed: %s", e)
        return False
    except (AttributeError, OSError) as e:
        logger.warning("Process-group cleanup unavailable: %s", e)
        return await _kill_single_process(proc)

    if await _wait_for_exit(proc, timeout=5):
        return True

    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except ProcessLookupError:
        return True
    except PermissionError as e:
        logger.warning("SIGKILL to process group failed: %s", e)
        return False
    except (AttributeError, OSError) as e:
        logger.warning("Process-group SIGKILL unavailable: %s", e)
        return await _kill_single_process(proc)

    return await _wait_for_exit(proc, timeout=5)


async def _kill_process_tree(proc: asyncio.subprocess.Process, timeout: int) -> bool:
    """Terminate a timed-out subprocess and its descendants when possible."""
    if proc.returncode is not None:
        return True

    pid = proc.pid
    logger.warning(
        "Subprocess timeout after %ss; killing process tree (pid=%s)", timeout, pid
    )

    if platform_name == "Windows":
        return await _kill_windows_process_tree(proc, pid)
    return await _kill_posix_process_tree(proc, pid)


class LocalShellConnector(BaseConnector[Any]):
    """
    Shell connector that runs scripts **locally** using asyncio subprocesses,
    bypassing the Flask local_server entirely.
    
    Public API is compatible with ``ShellConnector`` so that ``ShellSession``
    works without modification.
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
                **_subprocess_group_kwargs(),
            )
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
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
            killed = await _kill_process_tree(proc, timeout)
            suffix = "(killed)" if killed else "(kill attempted)"
            return {
                "status": "error",
                "output": f"Execution timed out after {timeout} seconds {suffix}",
                "content": f"Execution timed out after {timeout} seconds {suffix}",
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
                **_subprocess_group_kwargs(),
            )
            stdout_b, _ = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
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
            killed = await _kill_process_tree(proc, timeout)
            suffix = "(killed)" if killed else "(kill attempted)"
            return {
                "status": "error",
                "output": f"Script execution timed out after {timeout} seconds {suffix}",
                "content": f"Script execution timed out after {timeout} seconds {suffix}",
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
    # Public API (same signatures as ShellConnector)
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

    async def run_bash_script(
        self,
        script: str,
        *,
        timeout: int = 90,
        working_dir: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        conda_env: Optional[str] = None,
    ) -> Any:
        """Execute a Bash script locally.

        Return format matches the server's ``/run_bash_script`` endpoint.
        """
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

    # ------------------------------------------------------------------
    # BaseConnector abstract methods
    # ------------------------------------------------------------------

    async def invoke(self, name: str, params: dict[str, Any]) -> Any:
        """Dispatch by name — same routing as ShellConnector via AioHttpConnector."""
        name_upper = name.strip().upper()
        if "/RUN_PYTHON" in name_upper:
            return await self.run_python_script(
                params.get("code", ""),
                timeout=params.get("timeout", 90),
                working_dir=params.get("working_dir"),
                env=params.get("env"),
                conda_env=params.get("conda_env"),
            )
        elif "/RUN_BASH_SCRIPT" in name_upper:
            return await self.run_bash_script(
                params.get("script", ""),
                timeout=params.get("timeout", 90),
                working_dir=params.get("working_dir"),
                env=params.get("env"),
                conda_env=params.get("conda_env"),
            )
        else:
            raise NotImplementedError(f"LocalShellConnector does not support: {name}")

    async def request(self, *args: Any, **kwargs: Any) -> Any:
        """Not used in local mode."""
        raise NotImplementedError(
            "LocalShellConnector does not support raw HTTP requests"
        )

