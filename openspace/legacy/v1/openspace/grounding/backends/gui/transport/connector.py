import asyncio
import re
from typing import Any, Dict, Optional
from openspace.grounding.core.transport.connectors import AioHttpConnector
from .actions import build_pyautogui_command, KEYBOARD_KEYS
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


class GUIConnector(AioHttpConnector):
    """
    Connector for desktop_env HTTP API.
    Provides action execution and observation methods.
    """
    
    def __init__(
        self,
        vm_ip: str,
        server_port: int = 5000,
        timeout: int = 90,
        retry_times: int = 3,
        retry_interval: float = 5.0,
        pkgs_prefix: str = "import pyautogui; import time; pyautogui.FAILSAFE = False; {command}",
    ):
        """
        Initialize GUI connector.
        
        Args:
            vm_ip: IP address of the VM running desktop_env
            server_port: Port of the desktop_env HTTP server
            timeout: Request timeout in seconds
            retry_times: Number of retries for failed requests
            retry_interval: Interval between retries in seconds
            pkgs_prefix: Python command prefix for pyautogui setup
        """
        base_url = f"http://{vm_ip}:{server_port}"
        super().__init__(base_url, timeout=timeout)
        
        self.vm_ip = vm_ip
        self.server_port = server_port
        self.retry_times = retry_times
        self.retry_interval = retry_interval
        self.pkgs_prefix = pkgs_prefix
        self.timeout = timeout
    
    async def _retry_invoke(
        self, 
        operation_name: str,
        operation_func,
        *args,
        **kwargs
    ):
        """
        Execute operation with retry logic.
        
        Args:
            operation_name: Name of operation for logging
            operation_func: Async function to execute
            *args: Positional arguments for operation_func
            **kwargs: Keyword arguments for operation_func
        
        Returns:
            Operation result
        
        Raises:
            Exception: Last exception after all retries fail
        """
        last_exc: Exception | None = None
        
        for attempt in range(1, self.retry_times + 1):
            try:
                result = await operation_func(*args, **kwargs)
                logger.debug("%s executed successfully (attempt %d/%d)", operation_name, attempt, self.retry_times)
                return result
            except asyncio.TimeoutError as exc:
                logger.error("%s timed out", operation_name)
                raise RuntimeError(f"{operation_name} timed out after {self.timeout} seconds") from exc
            except Exception as exc:
                last_exc = exc
                if attempt == self.retry_times:
                    break
                logger.warning(
                    "%s failed (attempt %d/%d): %s, retrying in %.1f seconds...", 
                    operation_name, attempt, self.retry_times, exc, self.retry_interval
                )
                await asyncio.sleep(self.retry_interval)
        
        error_msg = f"{operation_name} failed after {self.retry_times} retries"
        logger.error(error_msg)
        raise last_exc or RuntimeError(error_msg)
    
    @staticmethod
    def _is_valid_image_response(content_type: str, data: Optional[bytes]) -> bool:
        """Validate image response using magic bytes."""
        if not isinstance(data, (bytes, bytearray)) or not data:
            return False
        # PNG magic
        if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
            return True
        # JPEG magic
        if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
            return True
        # Fallback to content-type
        if content_type and ("image/png" in content_type or "image/jpeg" in content_type):
            return True
        return False
    
    @staticmethod
    def _fix_pyautogui_less_than_bug(command: str) -> str:
        """
        Fix PyAutoGUI '<' character bug by converting it to hotkey("shift", ',') calls.
        
        This fixes the known PyAutoGUI issue where typing '<' produces '>' instead.
        References:
        - https://github.com/asweigart/pyautogui/issues/198
        - https://github.com/xlang-ai/OSWorld/issues/257
        
        Args:
            command (str): The original pyautogui command
            
        Returns:
            str: The fixed command with '<' characters handled properly
        """
        # Pattern to match press('<') or press('\u003c') calls  
        press_pattern = r'pyautogui\.press\(["\'](?:<|\\u003c)["\']\)'

        # Handle press('<') calls
        def replace_press_less_than(match):
            return 'pyautogui.hotkey("shift", ",")'
        
        # First handle press('<') calls
        command = re.sub(press_pattern, replace_press_less_than, command)

        # Pattern to match typewrite calls with quoted strings
        typewrite_pattern = r'pyautogui\.typewrite\((["\'])(.*?)\1\)'
        
        # Then handle typewrite calls
        def process_typewrite_match(match):
            quote_char = match.group(1)
            content = match.group(2)
            
            # Preprocess: Try to decode Unicode escapes like \u003c to actual '<'
            # This handles cases where '<' is represented as escaped Unicode
            try:
                # Attempt to decode unicode escapes
                decoded_content = content.encode('utf-8').decode('unicode_escape')
                content = decoded_content
            except UnicodeDecodeError:
                # If decoding fails, proceed with original content to avoid breaking existing logic
                pass  # Graceful degradation - fall back to original content if decoding fails
            
            # Check if content contains '<'
            if '<' not in content:
                return match.group(0)
            
            # Split by '<' and rebuild
            parts = content.split('<')
            result_parts = []
            
            for i, part in enumerate(parts):
                if i == 0:
                    # First part
                    if part:
                        result_parts.append(f"pyautogui.typewrite({quote_char}{part}{quote_char})")
                else:
                    # Add hotkey for '<' and then typewrite for the rest
                    result_parts.append('pyautogui.hotkey("shift", ",")')
                    if part:
                        result_parts.append(f"pyautogui.typewrite({quote_char}{part}{quote_char})")
            
            return '; '.join(result_parts)
        
        command = re.sub(typewrite_pattern, process_typewrite_match, command)
        
        return command
    
    async def get_screen_size(self) -> Optional[tuple[int, int]]:
        """
        Get actual screen size from desktop environment using pyautogui.
        
        Returns:
            (width, height) tuple, or None on failure
        """
        try:
            command = "print(pyautogui.size())"
            result = await self.execute_python_command(command)
            if result and result.get("status") == "success":
                output = result.get("output", "")
                # Parse output like "Size(width=2880, height=1800)"
                import re
                match = re.search(r'width=(\d+).*height=(\d+)', output)
                if match:
                    width = int(match.group(1))
                    height = int(match.group(2))
                    logger.info(f"Detected screen size: {width}x{height}")
                    return (width, height)
            logger.warning(f"Failed to detect screen size, output: {result}")
            return None
        except Exception as e:
            logger.error(f"Failed to get screen size: {e}")
            return None
    
    async def get_screenshot(self) -> Optional[bytes]:
        """
        Get screenshot from desktop environment.
        
        Returns:
            Screenshot image bytes (PNG/JPEG), or None on failure
        """
        try:
            async def _get():
                response = await self._request("GET", "/screenshot", timeout=10)
                if response.status == 200:
                    content_type = response.headers.get("Content-Type", "")
                    content = await response.read()
                    if self._is_valid_image_response(content_type, content):
                        return content
                    else:
                        raise ValueError("Invalid screenshot format")
                else:
                    raise RuntimeError(f"HTTP {response.status}")
            
            return await self._retry_invoke("get_screenshot", _get)
        except Exception as e:
            logger.error(f"Failed to get screenshot: {e}")
            return None
    
    async def execute_python_command(self, command: str) -> Optional[Dict[str, Any]]:
        """
        Execute a Python command on desktop environment.
        Used for pyautogui commands.
        
        Args:
            command: Python command to execute
        
        Returns:
            Response dict with execution result, or None on failure
        """
        try:
            # Apply '<' character fix for PyAutoGUI bug
            fixed_command = self._fix_pyautogui_less_than_bug(command)
            
            command_list = ["python", "-c", self.pkgs_prefix.format(command=fixed_command)]
            payload = {"command": command_list, "shell": False}
            
            async def _execute():
                return await self.post_json("/execute", payload)
            
            return await self._retry_invoke("execute_python_command", _execute)
        except Exception as e:
            logger.error(f"Failed to execute command: {e}")
            return None
    
    async def execute_action(self, action_type: str, parameters: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Execute a desktop action.
        This is the main method for action space execution.
        
        Args:
            action_type: Action type (e.g., 'CLICK', 'TYPING')
            parameters: Action parameters
        
        Returns:
            Result dict with execution status
        """
        parameters = parameters or {}
        
        # Handle control actions
        if action_type in ['WAIT', 'FAIL', 'DONE']:
            return {
                "status": "success",
                "action_type": action_type,
                "message": f"Control action {action_type} acknowledged"
            }
        
        # Validate keyboard keys
        if action_type in ['PRESS', 'KEY_DOWN', 'KEY_UP']:
            key = parameters.get('key')
            if key and key not in KEYBOARD_KEYS:
                return {
                    "status": "error",
                    "action_type": action_type,
                    "error": f"Invalid key: {key}. Must be in supported keyboard keys."
                }
        
        if action_type == 'HOTKEY':
            keys = parameters.get('keys', [])
            invalid_keys = [k for k in keys if k not in KEYBOARD_KEYS]
            if invalid_keys:
                return {
                    "status": "error",
                    "action_type": action_type,
                    "error": f"Invalid keys: {invalid_keys}"
                }
        
        # Build pyautogui command
        command = build_pyautogui_command(action_type, parameters)
        
        if command is None:
            return {
                "status": "error",
                "action_type": action_type,
                "error": f"Unsupported action type: {action_type}"
            }
        
        # Execute command
        result = await self.execute_python_command(command)
        
        if result:
            return {
                "status": "success",
                "action_type": action_type,
                "parameters": parameters,
                "result": result
            }
        else:
            return {
                "status": "error",
                "action_type": action_type,
                "parameters": parameters,
                "error": "Command execution failed"
            }
    
    async def get_accessibility_tree(self, max_depth: int = 5) -> Optional[Dict[str, Any]]:
        """
        Get accessibility tree from desktop environment.
        
        Args:
            max_depth: Maximum depth of accessibility tree traversal
        
        Returns:
            Accessibility tree as dict, or None on failure
        """
        try:
            async def _get():
                response = await self._request("GET", "/accessibility", timeout=10)
                if response.status == 200:
                    data = await response.json()
                    return data.get("AT")
                else:
                    raise RuntimeError(f"HTTP {response.status}")
            
            return await self._retry_invoke("get_accessibility_tree", _get)
        except Exception as e:
            logger.error(f"Failed to get accessibility tree: {e}")
            return None

    async def get_cursor_position(self) -> Optional[tuple[int, int]]:
        """
        Get current mouse cursor position.
        Useful for GUI debugging and relative positioning.
        
        Returns:
            (x, y) tuple, or None on failure
        """
        try:
            async def _get():
                result = await self.get_json("/cursor_position")
                return (result.get("x"), result.get("y"))
            
            return await self._retry_invoke("get_cursor_position", _get)
        except Exception as e:
            logger.error(f"Failed to get cursor position: {e}")
            return None
    
    async def invoke(self, name: str, params: dict[str, Any]) -> Any:
        """
        Unified RPC entry for operations.
        Required by BaseConnector.
        
        Args:
            name: Operation name (action_type or observation method)
            params: Operation parameters
        
        Returns:
            Operation result
        """
        # Handle observation methods
        if name == "screenshot":
            return await self.get_screenshot()
        elif name == "accessibility_tree":
            max_depth = params.get("max_depth", 5) if params else 5
            return await self.get_accessibility_tree(max_depth)
        elif name == "cursor_position":
            return await self.get_cursor_position()
        else:
            # Treat as action
            return await self.execute_action(name.upper(), params or {})