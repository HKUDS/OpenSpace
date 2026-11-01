import base64
import os
import time
from typing import Any, Dict, Optional, Tuple, List
from openspace.utils.logging import Logger
from PIL import Image
import io

logger = Logger.get_logger(__name__)

try:
    from anthropic import (
        Anthropic,
        AnthropicBedrock,
        AnthropicVertex,
        APIError,
        APIResponseValidationError,
        APIStatusError,
    )
    from anthropic.types.beta import (
        BetaMessageParam,
        BetaTextBlockParam,
    )
    ANTHROPIC_AVAILABLE = True
except ImportError:
    logger.warning("Anthropic SDK not available. Install with: pip install anthropic")
    ANTHROPIC_AVAILABLE = False

# Import utility functions
from .anthropic_utils import (
    APIProvider,
    PROVIDER_TO_DEFAULT_MODEL_NAME,
    COMPUTER_USE_BETA_FLAG,
    PROMPT_CACHING_BETA_FLAG,
    get_system_prompt,
    inject_prompt_caching,
    maybe_filter_to_n_most_recent_images,
    response_to_params,
)

# API retry configuration
API_RETRY_TIMES = 10
API_RETRY_INTERVAL = 5  # seconds


class AnthropicGUIClient:
    """
    Anthropic LLM Client for GUI operations.
    Uses Claude Sonnet 4.5 with computer-use-2025-01-24 API.
    
    Features:
    - Vision-based screen understanding
    - Automatic screenshot resizing (configurable display size)
    - Coordinate scaling between display and actual screen
    """
    
    def __init__(
        self,
        model: str = "claude-sonnet-4-5",
        platform: str = "Ubuntu",
        api_key: Optional[str] = None,
        provider: str = "anthropic",
        max_tokens: int = 4096,
        screen_size: Tuple[int, int] = (1920, 1080),
        display_size: Tuple[int, int] = (1024, 768),  # Computer use display size
        pyautogui_size: Optional[Tuple[int, int]] = None,  # PyAutoGUI working size
        only_n_most_recent_images: int = 3,
        enable_prompt_caching: bool = True,
        backup_api_key: Optional[str] = None,
    ):
        """
        Initialize Anthropic GUI Client for Claude Sonnet 4.5.
        
        Args:
            model: Model name (only "claude-sonnet-4-5" supported)
            platform: Platform type (Ubuntu, Windows, or macOS)
            api_key: Anthropic API key (defaults to ANTHROPIC_API_KEY env var)
            provider: API provider (only "anthropic" supported)
            max_tokens: Maximum tokens for response
            screen_size: Actual screenshot resolution (width, height) - physical pixels
            display_size: Display size for computer use tool (width, height)
                         Screenshots will be resized to this size before sending to API
            pyautogui_size: PyAutoGUI working size (logical pixels). If None, assumed same as screen_size.
                           On Retina/HiDPI displays, this may be screen_size / 2
            only_n_most_recent_images: Number of recent screenshots to keep in history
            enable_prompt_caching: Whether to enable prompt caching for cost optimization
            backup_api_key: Backup API key (defaults to ANTHROPIC_API_KEY_BACKUP env var)
        """
        if not ANTHROPIC_AVAILABLE:
            raise RuntimeError("Anthropic SDK not installed. Install with: pip install anthropic")
        
        # Only support claude-sonnet-4-5
        if model != "claude-sonnet-4-5":
            logger.warning(f"Model '{model}' not supported. Using 'claude-sonnet-4-5'")
            model = "claude-sonnet-4-5"
        
        self.model = model
        self.platform = platform
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ValueError("Anthropic API key not provided. Set ANTHROPIC_API_KEY env var or pass api_key parameter")
        
        # Backup API key for failover
        self.backup_api_key = backup_api_key or os.environ.get("ANTHROPIC_API_KEY_BACKUP")
        
        # Only support anthropic provider
        if provider != "anthropic":
            logger.warning(f"Provider '{provider}' not supported. Using 'anthropic'")
            provider = "anthropic"
        
        self.provider = APIProvider(provider)
        self.max_tokens = max_tokens
        self.screen_size = screen_size
        self.display_size = display_size
        self.pyautogui_size = pyautogui_size or screen_size  # Default to screen_size if not specified
        self.only_n_most_recent_images = only_n_most_recent_images
        self.enable_prompt_caching = enable_prompt_caching
        
        # Message history
        self.messages: List[BetaMessageParam] = []
        
        # Calculate resize factor for coordinate scaling
        # Step 1: LLM coordinates (display_size) -> Physical pixels (screen_size)
        # Step 2: Physical pixels -> PyAutoGUI logical pixels (pyautogui_size)
        self.resize_factor = (
            self.pyautogui_size[0] / display_size[0],  # x scale factor
            self.pyautogui_size[1] / display_size[1]   # y scale factor
        )
        
        logger.info(
            f"Initialized AnthropicGUIClient:\n"
            f"  Model: {model}\n"
            f"  Platform: {platform}\n"
            f"  Screen Size (physical): {screen_size}\n"
            f"  PyAutoGUI Size (logical): {self.pyautogui_size}\n"
            f"  Display Size (LLM): {display_size}\n"
            f"  Resize Factor (LLM->PyAutoGUI): {self.resize_factor}\n"
            f"  Prompt Caching: {enable_prompt_caching}"
        )
    
    def _create_client(self, api_key: Optional[str] = None):
        """Create Anthropic client (only supports anthropic provider)."""
        key = api_key or self.api_key
        return Anthropic(api_key=key, max_retries=4)
    
    def _resize_screenshot(self, screenshot_bytes: bytes) -> bytes:
        """
        Resize screenshot to display size for Computer Use API.
        
        For computer-use-2025-01-24, the screenshot must be resized to the
        display_width_px x display_height_px specified in the tool definition.
        """
        screenshot_image = Image.open(io.BytesIO(screenshot_bytes))
        resized_image = screenshot_image.resize(self.display_size, Image.Resampling.LANCZOS)
        
        output_buffer = io.BytesIO()
        resized_image.save(output_buffer, format='PNG')
        return output_buffer.getvalue()
    
    def _scale_coordinates(self, x: int, y: int) -> Tuple[int, int]:
        """
        Scale coordinates from display size to actual screen size.
        
        The API returns coordinates in display_size (e.g., 1024x768).
        We need to scale them to actual screen_size (e.g., 1920x1080) for execution.
        
        Args:
            x, y: Coordinates in display size space
            
        Returns:
            Scaled coordinates in actual screen size space
        """
        scaled_x = int(x * self.resize_factor[0])
        scaled_y = int(y * self.resize_factor[1])
        return scaled_x, scaled_y
    
    async def plan_action(
        self,
        task_description: str,
        screenshot: bytes,
        action_history: List[Dict[str, Any]] = None,
    ) -> Tuple[Optional[str], List[str]]:
        """
        Plan next action based on task and current screenshot.
        Includes prompt caching, error handling, and backup API key support.
        
        Args:
            task_description: Task to accomplish
            screenshot: Current screenshot (PNG bytes)
            action_history: Previous actions (for context)
        
        Returns:
            Tuple of (reasoning, list of pyautogui commands)
        """
        # Resize screenshot
        resized_screenshot = self._resize_screenshot(screenshot)
        screenshot_b64 = base64.b64encode(resized_screenshot).decode('utf-8')
        
        # Initialize messages with first task + screenshot
        if not self.messages:
            # IMPORTANT: Image should come BEFORE text for better model understanding
            # This matches OSWorld's implementation which has proven effectiveness
            self.messages.append({
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": screenshot_b64,
                        },
                    },
                    {"type": "text", "text": task_description},
                ]
            })
        
        # Filter images BEFORE adding new screenshot to control message size
        # This is critical to avoid exceeding the 25MB API limit
        image_truncation_threshold = 10
        if self.only_n_most_recent_images and len(self.messages) > 1:
            # Reserve 1 slot for the screenshot we're about to add
            maybe_filter_to_n_most_recent_images(
                self.messages,
                max(1, self.only_n_most_recent_images - 1),
                min_removal_threshold=1,  # More aggressive filtering
            )
        
        # Add tool result from previous action if exists
        if self.messages and self.messages[-1]["role"] == "assistant":
            last_content = self.messages[-1]["content"]
            if isinstance(last_content, list) and any(
                block.get("type") == "tool_use" for block in last_content
            ):
                tool_use_id = next(
                    block["id"] for block in last_content 
                    if block.get("type") == "tool_use"
                )
                self._add_tool_result(tool_use_id, "Success", resized_screenshot)
        
        # Define tools and betas for claude-sonnet-4-5 with computer-use-2025-01-24
        tools = [{
            'name': 'computer',
            'type': 'computer_20250124',
            'display_width_px': self.display_size[0],
            'display_height_px': self.display_size[1],
            'display_number': 1
        }]
        betas = [COMPUTER_USE_BETA_FLAG]
        
        # Prepare system prompt with optional caching
        system = BetaTextBlockParam(
            type="text",
            text=get_system_prompt(self.platform)
        )
        
        # Enable prompt caching if supported and enabled
        if self.enable_prompt_caching:
            betas.append(PROMPT_CACHING_BETA_FLAG)
            inject_prompt_caching(self.messages)
            system["cache_control"] = {"type": "ephemeral"}  # type: ignore
        
        # Model name - use claude-sonnet-4-5 directly
        model_name = "claude-sonnet-4-5"
        
        # Enable thinking for complex computer use tasks
        extra_body = {"thinking": {"type": "enabled", "budget_tokens": 2048}}
        
        # Log request details for debugging
        # Count current images in messages
        total_images = sum(
            1
            for message in self.messages
            for item in (message.get("content", []) if isinstance(message.get("content"), list) else [])
            if isinstance(item, dict) and item.get("type") == "image"
        )
        tool_result_images = sum(
            1
            for message in self.messages
            for item in (message.get("content", []) if isinstance(message.get("content"), list) else [])
            if isinstance(item, dict) and item.get("type") == "tool_result"
            for content in item.get("content", [])
            if isinstance(content, dict) and content.get("type") == "image"
        )
        logger.info(
            f"Anthropic API request:\n"
            f"  Model: {model_name}\n"
            f"  Display Size: {self.display_size}\n"
            f"  Betas: {betas}\n"
            f"  Images: {total_images} ({tool_result_images} in tool_results)\n"
            f"  Messages: {len(self.messages)}"
        )
        
        # Try API call with retry and backup
        client = self._create_client()
        response = None
        
        try:
            # Retry loop with automatic image count reduction on 25MB error
            for attempt in range(API_RETRY_TIMES):
                try:
                    response = client.beta.messages.create(
                        max_tokens=self.max_tokens,
                        messages=self.messages,
                        model=model_name,
                        system=[system],
                        tools=tools,
                        betas=betas,
                        extra_body=extra_body
                    )
                    logger.info(f"API call succeeded on attempt {attempt + 1}")
                    break
                    
                except (APIError, APIStatusError, APIResponseValidationError) as e:
                    error_msg = str(e)
                    logger.warning(f"Anthropic API error (attempt {attempt+1}/{API_RETRY_TIMES}): {error_msg}")
                    
                    # Handle 25MB payload limit error (including HTTP 413)
                    if ("25000000" in error_msg or 
                        "Member must have length less than or equal to" in error_msg or 
                        "request_too_large" in error_msg or 
                        "413" in str(e)):
                        logger.warning("Detected 25MB limit error, reducing image count")
                        current_count = self.only_n_most_recent_images
                        new_count = max(1, current_count // 2)
                        self.only_n_most_recent_images = new_count
                        
                        maybe_filter_to_n_most_recent_images(
                            self.messages,
                            new_count,
                            min_removal_threshold=1,  # Aggressive filtering when hitting limit
                        )
                        logger.info(f"Image count reduced from {current_count} to {new_count}")
                    
                    if attempt < API_RETRY_TIMES - 1:
                        time.sleep(API_RETRY_INTERVAL)
                    else:
                        raise
        
        except (APIError, APIStatusError, APIResponseValidationError) as e:
            logger.error(f"Primary API key failed: {e}")
            
            # Try backup API key if available
            if self.backup_api_key:
                logger.warning("Retrying with backup API key...")
                try:
                    backup_client = self._create_client(self.backup_api_key)
                    response = backup_client.beta.messages.create(
                        max_tokens=self.max_tokens,
                        messages=self.messages,
                        model=model_name,
                        system=[system],
                        tools=tools,
                        betas=betas,
                        extra_body=extra_body
                    )
                    logger.info("Successfully used backup API key")
                except Exception as backup_e:
                    logger.error(f"Backup API key also failed: {backup_e}")
                    return None, ["FAIL"]
            else:
                return None, ["FAIL"]
        
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            return None, ["FAIL"]
        
        if not response:
            return None, ["FAIL"]
        
        # Parse response using utility function
        response_params = response_to_params(response)
        
        # Extract reasoning and commands
        reasoning = ""
        commands = []
        
        for block in response_params:
            block_type = block.get("type")
            
            if block_type == "text":
                reasoning = block.get("text", "")
            elif block_type == "thinking":
                reasoning = block.get("thinking", "")
            elif block_type == "tool_use":
                tool_input = block.get("input", {})
                command = self._parse_computer_tool_use(tool_input)
                if command:
                    commands.append(command)
                else:
                    logger.warning(f"Failed to parse tool_use: {tool_input}")
        
        # Store assistant response
        self.messages.append({
            "role": "assistant",
            "content": response_params
        })
        
        logger.info(f"Parsed {len(commands)} commands from response")
        
        return reasoning, commands
    
    def _add_tool_result(
        self,
        tool_use_id: str,
        result: str,
        screenshot_bytes: Optional[bytes] = None
    ):
        """
        Add tool result to message history.
        IMPORTANT: Put screenshot BEFORE text for consistency with initial message.
        """
        # Build content list with image first (if provided), then text
        content_list = []
        
        # Add screenshot first if provided (consistent with initial message ordering)
        if screenshot_bytes is not None:
            screenshot_b64 = base64.b64encode(screenshot_bytes).decode('utf-8')
            content_list.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": screenshot_b64
                }
            })
        
        # Then add text result
        content_list.append({"type": "text", "text": result})
        
        tool_result_content = [{
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": content_list
        }]
        
        self.messages.append({
            "role": "user",
            "content": tool_result_content
        })
    
    def _parse_computer_tool_use(self, tool_input: Dict[str, Any]) -> Optional[str]:
        """
        Parse Anthropic computer tool use to pyautogui command.
        
        Args:
            tool_input: Tool input from Anthropic (action, coordinate, text, etc.)
        
        Returns:
            PyAutoGUI command string or control command (DONE, FAIL)
        """
        action = tool_input.get("action")
        if not action:
            return None
        
        # Action conversion
        action_conversion = {
            "left click": "click",
            "right click": "right_click"
        }
        action = action_conversion.get(action, action)
        
        text = tool_input.get("text")
        coordinate = tool_input.get("coordinate")
        scroll_direction = tool_input.get("scroll_direction")
        scroll_amount = tool_input.get("scroll_amount", 5)
        
        # Scale coordinates to actual screen size
        if coordinate:
            coordinate = self._scale_coordinates(coordinate[0], coordinate[1])
        
        # Build commands
        command = ""
        
        if action == "mouse_move":
            if coordinate:
                x, y = coordinate
                command = f"pyautogui.moveTo({x}, {y}, duration=0.5)"
        
        elif action in ("left_click", "click"):
            if coordinate:
                x, y = coordinate
                command = f"pyautogui.click({x}, {y})"
            else:
                command = "pyautogui.click()"
        
        elif action == "right_click":
            if coordinate:
                x, y = coordinate
                command = f"pyautogui.rightClick({x}, {y})"
            else:
                command = "pyautogui.rightClick()"
        
        elif action == "double_click":
            if coordinate:
                x, y = coordinate
                command = f"pyautogui.doubleClick({x}, {y})"
            else:
                command = "pyautogui.doubleClick()"
        
        elif action == "middle_click":
            if coordinate:
                x, y = coordinate
                command = f"pyautogui.middleClick({x}, {y})"
            else:
                command = "pyautogui.middleClick()"
        
        elif action == "left_click_drag":
            if coordinate:
                x, y = coordinate
                command = f"pyautogui.dragTo({x}, {y}, duration=0.5)"
        
        elif action == "key":
            if text:
                keys = text.split('+')
                # Key conversion
                key_conversion = {
                    "page_down": "pagedown",
                    "page_up": "pageup",
                    "super_l": "win",
                    "super": "command",
                    "escape": "esc"
                }
                converted_keys = [key_conversion.get(k.strip().lower(), k.strip().lower()) for k in keys]
                
                # Press and release keys
                for key in converted_keys:
                    command += f"pyautogui.keyDown('{key}'); "
                for key in reversed(converted_keys):
                    command += f"pyautogui.keyUp('{key}'); "
                # Remove trailing semicolon and space
                command = command.rstrip('; ')
        
        elif action == "type":
            if text:
                command = f"pyautogui.typewrite({repr(text)}, interval=0.01)"
        
        elif action == "scroll":
            if scroll_direction in ("up", "down"):
                scroll_value = scroll_amount if scroll_direction == "up" else -scroll_amount
                if coordinate:
                    x, y = coordinate
                    command = f"pyautogui.scroll({scroll_value}, {x}, {y})"
                else:
                    command = f"pyautogui.scroll({scroll_value})"
            elif scroll_direction in ("left", "right"):
                scroll_value = scroll_amount if scroll_direction == "right" else -scroll_amount
                if coordinate:
                    x, y = coordinate
                    command = f"pyautogui.hscroll({scroll_value}, {x}, {y})"
                else:
                    command = f"pyautogui.hscroll({scroll_value})"
        
        elif action == "screenshot":
            # Screenshot is automatically handled by the system
            # Return special marker to indicate no action needed
            return "SCREENSHOT"
        
        elif action == "wait":
            # Wait for specified duration
            duration = tool_input.get("duration", 1)
            command = f"pyautogui.sleep({duration})"
        
        elif action == "done":
            return "DONE"
        
        elif action == "fail":
            return "FAIL"
        
        return command if command else None
    
    def reset(self):
        """Reset message history."""
        self.messages = []
        logger.info("Reset AnthropicGUIClient message history")