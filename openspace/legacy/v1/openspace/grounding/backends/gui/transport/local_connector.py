"""
Local GUI Connector — execute GUI operations directly in-process.

This connector has the **same public API** as GUIConnector (HTTP version)
but uses local pyautogui / ScreenshotHelper / AccessibilityHelper,
removing the need for a local_server.

Return format is kept identical so that GUISession / GUIAgentTool
work without any changes.
"""

import asyncio
import os
import platform
import re
import tempfile
import uuid
from typing import Any, Dict, Optional

from openspace.grounding.core.transport.connectors.base import BaseConnector
from openspace.grounding.core.transport.task_managers.noop import NoOpConnectionManager
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

platform_name = platform.system()


class LocalGUIConnector(BaseConnector[Any]):
    """
    GUI connector that runs desktop automation **locally** using pyautogui /
    ScreenshotHelper / AccessibilityHelper, bypassing the Flask local_server.

    Public API is compatible with ``GUIConnector`` so that ``GUISession``
    works without modification.
    """

    def __init__(
        self,
        timeout: int = 90,
        retry_times: int = 3,
        retry_interval: float = 5.0,
        pkgs_prefix: str = "import pyautogui; import time; pyautogui.FAILSAFE = False; {command}",
    ):
        super().__init__(NoOpConnectionManager())
        self.timeout = timeout
        self.retry_times = retry_times
        self.retry_interval = retry_interval
        self.pkgs_prefix = pkgs_prefix

        # Compatibility attributes expected by GUISession
        self.vm_ip = "localhost"
        self.server_port = 0
        self.base_url = "local://localhost"

        # Lazy-initialized helpers (avoid import side effects at class load)
        self._screenshot_helper = None
        self._accessibility_helper = None

    def _get_screenshot_helper(self):
        if self._screenshot_helper is None:
            from openspace.local_server.utils import ScreenshotHelper
            self._screenshot_helper = ScreenshotHelper()
        return self._screenshot_helper

    def _get_accessibility_helper(self):
        if self._accessibility_helper is None:
            from openspace.local_server.utils import AccessibilityHelper
            self._accessibility_helper = AccessibilityHelper()
        return self._accessibility_helper

    # ------------------------------------------------------------------
    # connect / disconnect
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """No real connection for local mode."""
        if self._connected:
            return
        await super().connect()
        logger.info("LocalGUIConnector: ready (local mode, no server required)")

    # ------------------------------------------------------------------
    # Retry wrapper (same interface as GUIConnector._retry_invoke)
    # ------------------------------------------------------------------

    async def _retry_invoke(
        self,
        operation_name: str,
        operation_func,
        *args,
        **kwargs,
    ):
        last_exc: Exception | None = None
        for attempt in range(1, self.retry_times + 1):
            try:
                result = await operation_func(*args, **kwargs)
                logger.debug(
                    "%s executed successfully (attempt %d/%d)",
                    operation_name, attempt, self.retry_times,
                )
                return result
            except asyncio.TimeoutError as exc:
                logger.error("%s timed out", operation_name)
                raise RuntimeError(
                    f"{operation_name} timed out after {self.timeout} seconds"
                ) from exc
            except Exception as exc:
                last_exc = exc
                if attempt == self.retry_times:
                    break
                logger.warning(
                    "%s failed (attempt %d/%d): %s, retrying in %.1f seconds...",
                    operation_name, attempt, self.retry_times, exc, self.retry_interval,
                )
                await asyncio.sleep(self.retry_interval)

        error_msg = f"{operation_name} failed after {self.retry_times} retries"
        logger.error(error_msg)
        raise last_exc or RuntimeError(error_msg)

    # ------------------------------------------------------------------
    # PyAutoGUI '<' bug fix (same as GUIConnector)
    # ------------------------------------------------------------------

    @staticmethod
    def _fix_pyautogui_less_than_bug(command: str) -> str:
        """Fix PyAutoGUI '<' character bug."""
        press_pattern = r'pyautogui\.press\(["\'](?:<|\\u003c)["\']\)'

        def replace_press_less_than(match):
            return 'pyautogui.hotkey("shift", ",")'

        command = re.sub(press_pattern, replace_press_less_than, command)

        typewrite_pattern = r'pyautogui\.typewrite\((["\'])(.*?)\1\)'

        def process_typewrite_match(match):
            quote_char = match.group(1)
            content = match.group(2)
            try:
                decoded_content = content.encode("utf-8").decode("unicode_escape")
                content = decoded_content
            except UnicodeDecodeError:
                pass
            if "<" not in content:
                return match.group(0)
            parts = content.split("<")
            result_parts = []
            for i, part in enumerate(parts):
                if i == 0:
                    if part:
                        result_parts.append(
                            f"pyautogui.typewrite({quote_char}{part}{quote_char})"
                        )
                else:
                    result_parts.append('pyautogui.hotkey("shift", ",")')
                    if part:
                        result_parts.append(
                            f"pyautogui.typewrite({quote_char}{part}{quote_char})"
                        )
            return "; ".join(result_parts)

        command = re.sub(typewrite_pattern, process_typewrite_match, command)
        return command

    # ------------------------------------------------------------------
    # Image response validation (same as GUIConnector)
    # ------------------------------------------------------------------

    @staticmethod
    def _is_valid_image_response(content_type: str, data: Optional[bytes]) -> bool:
        if not isinstance(data, (bytes, bytearray)) or not data:
            return False
        if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
            return True
        if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
            return True
        if content_type and ("image/png" in content_type or "image/jpeg" in content_type):
            return True
        return False

    # ------------------------------------------------------------------
    # Public API (same signatures as GUIConnector)
    # ------------------------------------------------------------------

    async def get_screen_size(self) -> Optional[tuple[int, int]]:
        """Get screen size using pyautogui."""
        try:
            command = "print(pyautogui.size())"
            result = await self.execute_python_command(command)
            if result and result.get("status") == "success":
                output = result.get("output", "")
                match = re.search(r"width=(\d+).*height=(\d+)", output)
                if match:
                    width = int(match.group(1))
                    height = int(match.group(2))
                    logger.info("Detected screen size: %dx%d", width, height)
                    return (width, height)
            logger.warning("Failed to detect screen size, output: %s", result)
            return None
        except Exception as e:
            logger.error("Failed to get screen size: %s", e)
            return None

    async def get_screenshot(self) -> Optional[bytes]:
        """Capture screenshot locally using ScreenshotHelper."""
        try:
            async def _get():
                helper = self._get_screenshot_helper()
                tmp_path = os.path.join(
                    tempfile.gettempdir(), f"screenshot_{uuid.uuid4().hex}.png"
                )
                if helper.capture(tmp_path, with_cursor=True):
                    with open(tmp_path, "rb") as f:
                        data = f.read()
                    os.remove(tmp_path)
                    return data
                else:
                    raise RuntimeError("Screenshot capture failed")

            return await self._retry_invoke("get_screenshot", _get)
        except Exception as e:
            logger.error("Failed to get screenshot: %s", e)
            return None

    async def execute_python_command(self, command: str) -> Optional[Dict[str, Any]]:
        """Execute a pyautogui Python command locally via subprocess."""
        try:
            fixed_command = self._fix_pyautogui_less_than_bug(command)
            full_command = self.pkgs_prefix.format(command=fixed_command)

            async def _execute():
                python_cmd = "python" if platform_name == "Windows" else "python3"
                proc = await asyncio.create_subprocess_exec(
                    python_cmd, "-c", full_command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=self.timeout
                )
                stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
                stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""
                returncode = proc.returncode or 0
                return {
                    "status": "success" if returncode == 0 else "error",
                    "output": stdout + stderr,
                    "error": stderr if returncode != 0 else "",
                    "returncode": returncode,
                }

            return await self._retry_invoke("execute_python_command", _execute)
        except Exception as e:
            logger.error("Failed to execute command: %s", e)
            return None

    async def execute_action(
        self, action_type: str, parameters: Dict[str, Any] | None = None
    ) -> Dict[str, Any]:
        """Execute a desktop action (same logic as GUIConnector)."""
        parameters = parameters or {}

        if action_type in ["WAIT", "FAIL", "DONE"]:
            return {
                "status": "success",
                "action_type": action_type,
                "message": f"Control action {action_type} acknowledged",
            }

        # Import action builder (same module used by GUIConnector)
        from openspace.grounding.backends.gui.transport.actions import (
            build_pyautogui_command,
            KEYBOARD_KEYS,
        )

        if action_type in ["PRESS", "KEY_DOWN", "KEY_UP"]:
            key = parameters.get("key")
            if key and key not in KEYBOARD_KEYS:
                return {
                    "status": "error",
                    "action_type": action_type,
                    "error": f"Invalid key: {key}. Must be in supported keyboard keys.",
                }
        if action_type == "HOTKEY":
            keys = parameters.get("keys", [])
            invalid_keys = [k for k in keys if k not in KEYBOARD_KEYS]
            if invalid_keys:
                return {
                    "status": "error",
                    "action_type": action_type,
                    "error": f"Invalid keys: {invalid_keys}",
                }

        command = build_pyautogui_command(action_type, parameters)
        if command is None:
            return {
                "status": "error",
                "action_type": action_type,
                "error": f"Unsupported action type: {action_type}",
            }

        result = await self.execute_python_command(command)
        if result:
            return {
                "status": "success",
                "action_type": action_type,
                "parameters": parameters,
                "result": result,
            }
        else:
            return {
                "status": "error",
                "action_type": action_type,
                "parameters": parameters,
                "error": "Command execution failed",
            }

    async def get_accessibility_tree(
        self, max_depth: int = 5
    ) -> Optional[Dict[str, Any]]:
        """Get accessibility tree locally."""
        try:
            async def _get():
                helper = self._get_accessibility_helper()
                return helper.get_tree(max_depth=max_depth)

            return await self._retry_invoke("get_accessibility_tree", _get)
        except Exception as e:
            logger.error("Failed to get accessibility tree: %s", e)
            return None

    async def get_cursor_position(self) -> Optional[tuple[int, int]]:
        """Get cursor position locally."""
        try:
            async def _get():
                helper = self._get_screenshot_helper()
                return helper.get_cursor_position()

            return await self._retry_invoke("get_cursor_position", _get)
        except Exception as e:
            logger.error("Failed to get cursor position: %s", e)
            return None

    # ------------------------------------------------------------------
    # BaseConnector abstract methods
    # ------------------------------------------------------------------

    async def invoke(self, name: str, params: dict[str, Any]) -> Any:
        if name == "screenshot":
            return await self.get_screenshot()
        elif name == "accessibility_tree":
            max_depth = params.get("max_depth", 5) if params else 5
            return await self.get_accessibility_tree(max_depth)
        elif name == "cursor_position":
            return await self.get_cursor_position()
        else:
            return await self.execute_action(name.upper(), params or {})

    async def request(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(
            "LocalGUIConnector does not support raw HTTP requests"
        )

