import asyncio
from typing import Any, Optional, Dict

from openspace.grounding.core.transport.connectors import AioHttpConnector
from openspace.grounding.core.security import SecurityPolicyManager
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


class ShellConnector(AioHttpConnector):
    """
    Shell backend HTTP connector
    Basic routes:
      POST /run_python      {"code": str}
      POST /run_bash_script {"script": str, "timeout": int, "working_dir": str | None}
    """

    def __init__(
        self,
        vm_ip: str,
        port: int = 5000,
        *,
        retry_times: int = 3,
        retry_interval: float = 5,
        security_manager: "SecurityPolicyManager | None" = None,
    ) -> None:
        base_url = f"http://{vm_ip}:{port}"
        super().__init__(base_url)
        self.retry_times = retry_times
        self.retry_interval = retry_interval
        self._security_manager = security_manager

    async def _retry_invoke(
        self, 
        name: str, 
        payload: Dict[str, Any], 
        script_timeout: int,
        *,
        break_on_timeout: bool = False
    ):
        """
        Execute HTTP request and retry
        
        Args:
            name: RPC method name
            payload: Request payload
            script_timeout: Script execution timeout
            break_on_timeout: Whether to exit immediately on timeout (default False)
        
        Returns:
            Server response result
        
        Raises:
            Exception: Last exception thrown after all retries fail
        """
        last_exc: Exception | None = None
        # HTTP request timeout should be longer than script execution timeout, leaving buffer time
        http_timeout = script_timeout + 60
        
        for attempt in range(1, self.retry_times + 1):
            try:
                # Pass timeout parameter to server
                result = await self.invoke(name, payload | {"timeout": script_timeout})
                logger.info("%s executed successfully (attempt %d/%d)", name, attempt, self.retry_times)
                return result
            except asyncio.TimeoutError as exc:
                # Timeout exception usually does not need to be retried (script execution time too long)
                if break_on_timeout:
                    logger.error("%s timed out after %d seconds, aborting retry", name, script_timeout)
                    raise RuntimeError(
                        f"Script execution timed out after {script_timeout} seconds"
                    ) from exc
                last_exc = exc
                if attempt == self.retry_times:
                    break
                logger.warning(
                    "%s timed out (attempt %d/%d), retrying in %.1f seconds...", 
                    name, attempt, self.retry_times, self.retry_interval
                )
                await asyncio.sleep(self.retry_interval)
            except Exception as exc:
                last_exc = exc
                if attempt == self.retry_times:
                    break
                logger.warning(
                    "%s failed (attempt %d/%d): %s, retrying in %.1f seconds...", 
                    name, attempt, self.retry_times, exc, self.retry_interval
                )
                await asyncio.sleep(self.retry_interval)
        
        error_msg = f"{name} failed after {self.retry_times} retries"
        logger.error(error_msg)
        raise last_exc or RuntimeError(error_msg)

    async def run_python_script(
        self, 
        code: str, 
        *, 
        timeout: int = 90,
        working_dir: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        conda_env: Optional[str] = None
    ) -> Any:
        """
        Execute Python script on remote server
        
        Args:
            code: Python code string
            timeout: Execution timeout in seconds (default 90 seconds)
            working_dir: Working directory for script execution (optional)
            env: Environment variables for script execution (optional)
            conda_env: Conda environment name to activate (optional)
        
        Returns:
            Server response result
        
        Raises:
            PermissionError: Security policy blocked execution
            RuntimeError: Execution failed or timed out
        """
        if self._security_manager:
            from openspace.grounding.core.types import BackendType
            allowed = await self._security_manager.check_command_allowed(BackendType.SHELL, code)
            if not allowed:
                logger.error("SecurityPolicy blocked python code execution")
                raise PermissionError("SecurityPolicy: python code execution blocked")
        
        payload = {"code": code, "working_dir": working_dir, "env": env, "conda_env": conda_env}
        logger.info(
            "Executing python script with timeout=%d seconds%s%s%s",
            timeout,
            f", working_dir={working_dir}" if working_dir else "",
            f", env={list(env.keys())}" if env else "",
            f", conda_env={conda_env}" if conda_env else ""
        )
        # Python script timed out, exit immediately without retry (timeout usually means script logic problem)
        return await self._retry_invoke(
            "POST /run_python", 
            payload, 
            timeout,
            break_on_timeout=True
        )

    async def run_bash_script(
        self,
        script: str,
        *,
        timeout: int = 90,
        working_dir: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        conda_env: Optional[str] = None
    ) -> Any:
        """
        Execute Bash script on remote server
        
        Args:
            script: Bash script content (can be multi-line)
            timeout: Execution timeout in seconds (default 90 seconds)
            working_dir: Working directory for script execution (optional)
            env: Environment variables for script execution (optional)
            conda_env: Conda environment name to activate (optional)
        
        Returns:
            Server response result, containing status, output, error, returncode, etc.
        
        Raises:
            PermissionError: Security policy blocked execution
            RuntimeError: Execution failed or timed out
        """
        if self._security_manager:
            from openspace.grounding.core.types import BackendType
            allowed = await self._security_manager.check_command_allowed(BackendType.SHELL, script)
            if not allowed:
                logger.error("SecurityPolicy blocked bash script execution")
                raise PermissionError("SecurityPolicy: bash script execution blocked")
        
        payload = {"script": script, "working_dir": working_dir, "env": env, "conda_env": conda_env}
        logger.info(
            "Executing bash script with timeout=%d seconds%s%s%s", 
            timeout,
            f", working_dir={working_dir}" if working_dir else "",
            f", env={list(env.keys())}" if env else "",
            f", conda_env={conda_env}" if conda_env else ""
        )
        
        # Bash script timed out, exit immediately without retry (timeout usually means script logic problem)
        result = await self._retry_invoke(
            "POST /run_bash_script", 
            payload, 
            timeout,
            break_on_timeout=True
        )
        
        # Record execution result
        if isinstance(result, dict) and "returncode" in result:
            logger.info("Bash script executed with return code: %d", result.get("returncode", -1))
        
        return result