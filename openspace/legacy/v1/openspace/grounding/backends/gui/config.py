from typing import Dict, Any, Optional
import os
import platform as platform_module
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


def build_llm_config(user_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Build complete LLM configuration with auto-detection and environment variables.
    
    Auto-detects:
    - API key from environment variables (ANTHROPIC_API_KEY)
    - Platform from system (macOS/Windows/Ubuntu)
    - Provider defaults to 'anthropic'
    
    User-provided config values will override auto-detected values.
    
    Args:
        user_config: User-provided configuration (optional)
        
    Returns:
        Complete LLM configuration dict
        
    Example:
        >>> # Auto-detect everything
        >>> config = build_llm_config()
        
        >>> # Override specific values
        >>> config = build_llm_config({
        ...     "model": "claude-3-5-sonnet-20241022",
        ...     "max_tokens": 8192
        ... })
    """
    if user_config is None:
        user_config = {}
    
    # Auto-detect platform
    system = platform_module.system()
    if system == "Darwin":
        detected_platform = "macOS"
    elif system == "Windows":
        detected_platform = "Windows"
    else:  # Linux
        detected_platform = "Ubuntu"
    
    # Auto-detect API key from environment
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning(
            "ANTHROPIC_API_KEY not found in environment. "
            "Please set it: export ANTHROPIC_API_KEY='your-key'"
        )
    
    # Build configuration with precedence: user_config > auto-detected > defaults
    config = {
        "type": user_config.get("type", "anthropic"),
        "model": user_config.get("model", "claude-sonnet-4-5"),
        "platform": user_config.get("platform", detected_platform),
        "api_key": user_config.get("api_key", api_key),
        "provider": user_config.get("provider", "anthropic"),
        "max_tokens": user_config.get("max_tokens", 4096),
        "only_n_most_recent_images": user_config.get("only_n_most_recent_images", 3),
        "enable_prompt_caching": user_config.get("enable_prompt_caching", True),
    }
    
    # Optional: screen_size (will be auto-detected from screenshot later)
    if "screen_size" in user_config:
        config["screen_size"] = user_config["screen_size"]
    
    logger.info(f"Built LLM config - Platform: {config['platform']}, Model: {config['model']}")
    if config["api_key"]:
        logger.info(f"API key loaded: {config['api_key'][:10]}...")
    
    return config