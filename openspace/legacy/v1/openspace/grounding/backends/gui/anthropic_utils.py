from typing import List, cast
from enum import Enum
from datetime import datetime
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

try:
    from anthropic.types.beta import (
        BetaCacheControlEphemeralParam,
        BetaContentBlockParam,
        BetaImageBlockParam,
        BetaMessage,
        BetaMessageParam,
        BetaTextBlock,
        BetaTextBlockParam,
        BetaToolResultBlockParam,
        BetaToolUseBlockParam,
    )
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False


# Beta flags
# For claude-sonnet-4-5 with computer-use-2025-01-24
COMPUTER_USE_BETA_FLAG = "computer-use-2025-01-24"
PROMPT_CACHING_BETA_FLAG = "prompt-caching-2024-07-31"


class APIProvider(Enum):
    """API Provider enumeration"""
    ANTHROPIC = "anthropic"
    # BEDROCK = "bedrock"
    # VERTEX = "vertex"


# Provider to model name mapping (simplified for claude-sonnet-4-5 only)
PROVIDER_TO_DEFAULT_MODEL_NAME: dict = {
    (APIProvider.ANTHROPIC, "claude-sonnet-4-5"): "claude-sonnet-4-5",
    # (APIProvider.BEDROCK, "claude-sonnet-4-5"): "us.anthropic.claude-sonnet-4-5-v1:0",
    # (APIProvider.VERTEX, "claude-sonnet-4-5"): "claude-sonnet-4-5-v1",
}


def get_system_prompt(platform: str = "Ubuntu") -> str:
    """
    Get system prompt based on platform.
    
    Args:
        platform: Platform type (Ubuntu, Windows, macOS, or Darwin)
    
    Returns:
        System prompt string
    """
    # Normalize platform name
    platform_lower = platform.lower()
    
    if platform_lower in ["windows", "win32"]:
        return f"""<SYSTEM_CAPABILITY>
* You are utilising a Windows virtual machine using x86_64 architecture with internet access.
* You can use the computer tool to interact with the desktop: take screenshots, click, type, and control applications.
* To accomplish tasks, you MUST use the computer tool to see the screen and take actions.
* To open browser, please just click on the Chrome icon. Note, Chrome is what is installed on your system.
* When viewing a page it can be helpful to zoom out so that you can see everything on the page. Either that, or make sure you scroll down to see everything before deciding something isn't available.
* DO NOT ask users for clarification during task execution. DO NOT stop to request more information from users. Always take action using available tools.
* When using your computer function calls, they take a while to run and send back to you. Where possible/feasible, try to chain multiple of these calls all into one function calls request.
* The current date is {datetime.today().strftime('%A, %B %d, %Y')}.
* Home directory of this Windows system is 'C:\\Users\\user'.
* When you want to open some applications on Windows, please use Double Click on it instead of clicking once.
* After each action, the system will provide you with a new screenshot showing the result.
* Continue taking actions until the task is complete.
</SYSTEM_CAPABILITY>"""
    elif platform_lower in ["macos", "darwin", "mac"]:
        return f"""<SYSTEM_CAPABILITY>
* You are utilising a macOS system with internet access.
* You can use the computer tool to interact with the desktop: take screenshots, click, type, and control applications.
* To accomplish tasks, you MUST use the computer tool to see the screen and take actions.
* To open browser, please just click on the Chrome icon. Note, Chrome is what is installed on your system.
* When viewing a page it can be helpful to zoom out so that you can see everything on the page. Either that, or make sure you scroll down to see everything before deciding something isn't available.
* DO NOT ask users for clarification during task execution. DO NOT stop to request more information from users. Always take action using available tools.
* When using your computer function calls, they take a while to run and send back to you. Where possible/feasible, try to chain multiple of these calls all into one function calls request.
* The current date is {datetime.today().strftime('%A, %B %d, %Y')}.
* Home directory of this macOS system is typically '/Users/[username]' or can be accessed via '~'.
* On macOS, use Command (⌘) key combinations instead of Ctrl (e.g., Command+C for copy).
* After each action, the system will provide you with a new screenshot showing the result.
* Continue taking actions until the task is complete.
* When the task is completed, simply describe what you've done in your response WITHOUT using the tool again.
</SYSTEM_CAPABILITY>"""
    else:  # Ubuntu/Linux
        return f"""<SYSTEM_CAPABILITY>
* You are utilising an Ubuntu virtual machine using x86_64 architecture with internet access.
* You can use the computer tool to interact with the desktop: take screenshots, click, type, and control applications.
* To accomplish tasks, you MUST use the computer tool to see the screen and take actions.
* To open browser, please just click on the Chrome icon. Note, Chrome is what is installed on your system.
* When viewing a page it can be helpful to zoom out so that you can see everything on the page. Either that, or make sure you scroll down to see everything before deciding something isn't available.
* DO NOT ask users for clarification during task execution. DO NOT stop to request more information from users. Always take action using available tools.
* When using your computer function calls, they take a while to run and send back to you. Where possible/feasible, try to chain multiple of these calls all into one function calls request.
* The current date is {datetime.today().strftime('%A, %B %d, %Y')}.
* Home directory of this Ubuntu system is '/home/user'.
* After each action, the system will provide you with a new screenshot showing the result.
* Continue taking actions until the task is complete.
</SYSTEM_CAPABILITY>"""


def inject_prompt_caching(messages: List[BetaMessageParam]) -> None:
    """
    Set cache breakpoints for the 3 most recent turns.
    One cache breakpoint is left for tools/system prompt, to be shared across sessions.
    
    Args:
        messages: Message history (modified in place)
    """
    if not ANTHROPIC_AVAILABLE:
        return
    
    breakpoints_remaining = 3
    for message in reversed(messages):
        if message["role"] == "user" and isinstance(
            content := message["content"], list
        ):
            if breakpoints_remaining:
                breakpoints_remaining -= 1
                # Use type ignore to bypass TypedDict check until SDK types are updated
                content[-1]["cache_control"] = BetaCacheControlEphemeralParam(  # type: ignore
                    {"type": "ephemeral"}
                )
            else:
                content[-1].pop("cache_control", None)
                # we'll only ever have one extra turn per loop
                break


def maybe_filter_to_n_most_recent_images(
    messages: List[BetaMessageParam],
    images_to_keep: int,
    min_removal_threshold: int,
) -> None:
    """
    With the assumption that images are screenshots that are of diminishing value as
    the conversation progresses, remove all but the final `images_to_keep` tool_result
    images in place, with a chunk of min_removal_threshold to reduce the amount we
    break the implicit prompt cache.
    
    Args:
        messages: Message history (modified in place)
        images_to_keep: Number of recent images to keep
        min_removal_threshold: Minimum number of images to remove at once (for cache efficiency)
    """
    if not ANTHROPIC_AVAILABLE or images_to_keep is None:
        return
    
    tool_result_blocks = cast(
        list[BetaToolResultBlockParam],
        [
            item
            for message in messages
            for item in (
                message["content"] if isinstance(message["content"], list) else []
            )
            if isinstance(item, dict) and item.get("type") == "tool_result"
        ],
    )
    
    total_images = sum(
        1
        for tool_result in tool_result_blocks
        for content in tool_result.get("content", [])
        if isinstance(content, dict) and content.get("type") == "image"
    )
    
    images_to_remove = total_images - images_to_keep
    # for better cache behavior, we want to remove in chunks
    images_to_remove -= images_to_remove % min_removal_threshold
    
    for tool_result in tool_result_blocks:
        if isinstance(tool_result.get("content"), list):
            new_content = []
            for content in tool_result.get("content", []):
                if isinstance(content, dict) and content.get("type") == "image":
                    if images_to_remove > 0:
                        images_to_remove -= 1
                        continue
                new_content.append(content)
            tool_result["content"] = new_content


def response_to_params(response: BetaMessage) -> List[BetaContentBlockParam]:
    """
    Convert Anthropic response to parameter list.
    Handles both text blocks, tool use blocks, and thinking blocks.
    
    Args:
        response: Anthropic API response
    
    Returns:
        List of content blocks
    """
    if not ANTHROPIC_AVAILABLE:
        return []
    
    res: List[BetaContentBlockParam] = []
    if response.content:
        for block in response.content:
            # Check block type using type attribute
            # Note: type may be a string or enum, so convert to string for comparison
            block_type = str(getattr(block, "type", ""))
            
            if block_type == "text":
                # Regular text block
                if isinstance(block, BetaTextBlock) and block.text:
                    res.append(BetaTextBlockParam(type="text", text=block.text))
            elif block_type == "thinking":
                # Thinking block (for Claude 4 and Sonnet 3.7)
                thinking_block = {
                    "type": "thinking",
                    "thinking": getattr(block, "thinking", ""),
                }
                if hasattr(block, "signature"):
                    thinking_block["signature"] = getattr(block, "signature", None)
                res.append(cast(BetaContentBlockParam, thinking_block))
            elif block_type == "tool_use":
                # Tool use block - only include required fields to avoid API errors
                # (e.g., 'caller' field is not permitted by Anthropic API)
                tool_use_dict = {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                }
                res.append(cast(BetaToolUseBlockParam, tool_use_dict))
            else:
                # Unknown block type - try to handle generically
                try:
                    res.append(cast(BetaContentBlockParam, block.model_dump()))
                except Exception as e:
                    logger.warning(f"Failed to parse block type {block_type}: {e}")
        return res
    else:
        return []

