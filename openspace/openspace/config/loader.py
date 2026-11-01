import threading
from pathlib import Path
from typing import Union, Iterable, Dict, Any, Optional

from .grounding import GroundingConfig
from .constants import (
    CONFIG_GROUNDING,
    CONFIG_SECURITY,
    CONFIG_DEV,
    CONFIG_MCP,
    CONFIG_AGENTS
)
from openspace.utils.logging import Logger
from .utils import load_json_file, save_json_file as save_json

logger = Logger.get_logger(__name__)


CONFIG_DIR = Path(__file__).parent

# Global configuration singleton
_config: GroundingConfig | None = None
_config_lock = threading.RLock()  # Use RLock to support recursive locking


def _deep_merge_dict(base: dict, update: dict) -> dict:
    """Deep merge two dictionaries, update's values will override base's values"""
    result = base.copy()
    for key, value in update.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge_dict(result[key], value)
        else:
            result[key] = value
    return result

def _load_json_file(path: Path) -> Dict[str, Any]:
    """Load single JSON configuration file.
    
    This function wraps the generic load_json_file and adds global configuration specific error handling and logging.
    """
    if not path.exists():
        logger.debug(f"Configuration file does not exist, skipping: {path}")
        return {}
    
    try:
        data = load_json_file(path)
        logger.info(f"Loaded configuration file: {path}")
        return data
    except Exception as e:
        logger.warning(f"Failed to load configuration file {path}: {e}")
        return {}

def _load_multiple_files(paths: Iterable[Path]) -> Dict[str, Any]:
    """Load configuration from multiple files"""
    merged = {}
    for path in paths:
        data = _load_json_file(path)
        if data:
            merged = _deep_merge_dict(merged, data)
    return merged

def load_config(*config_paths: Union[str, Path]) -> GroundingConfig:
    """
    Load configuration files
    """
    global _config
    
    with _config_lock:
        if config_paths:
            paths = [Path(p) for p in config_paths]
        else:
            paths = [
                CONFIG_DIR / CONFIG_GROUNDING,
                CONFIG_DIR / CONFIG_SECURITY,
                CONFIG_DIR / CONFIG_DEV,  # Optional: development environment configuration
            ]
        
        # Load and merge configuration
        raw_data = _load_multiple_files(paths)
        
        # Load MCP configuration (separate processing)
        # Check if mcpServers already provided in merged custom configs
        has_custom_mcp_servers = "mcpServers" in raw_data
        
        if has_custom_mcp_servers:
            # Use mcpServers from custom config
            if "mcp" not in raw_data:
                raw_data["mcp"] = {}
            raw_data["mcp"]["servers"] = raw_data.pop("mcpServers")
            logger.debug(f"Using custom MCP servers from provided config ({len(raw_data['mcp']['servers'])} servers)")
        else:
            # Load default MCP servers from config_mcp.json
            mcp_data = _load_json_file(CONFIG_DIR / CONFIG_MCP)
            if mcp_data and "mcpServers" in mcp_data:
                if "mcp" not in raw_data:
                    raw_data["mcp"] = {}
                raw_data["mcp"]["servers"] = mcp_data["mcpServers"]
                logger.debug(f"Loaded MCP servers from default config_mcp.json ({len(raw_data['mcp']['servers'])} servers)")
        
        # Validate and create configuration object
        try:
            _config = GroundingConfig.model_validate(raw_data)
        except Exception as e:
            logger.error(f"Validation failed, using default configuration: {e}")
            _config = GroundingConfig()
        
        # Adjust log level according to configuration
        if _config.debug:
            Logger.set_debug(2)
        elif _config.log_level:
            try:
                Logger.configure(level=_config.log_level)
            except Exception as e:
                logger.warning(f"Failed to set log level {_config.log_level}: {e}")
    
    return _config

def get_config() -> GroundingConfig:
    """
    Get global configuration instance.
    
    Usage:
        - Get configuration in Provider: get_config().get_backend_config('shell')
        - Get security policy in Tool: get_config().get_security_policy('shell')
    """
    global _config
    
    if _config is None:
        with _config_lock:
            if _config is None:
                load_config()
    
    return _config

def reset_config() -> None:
    """Reset configuration (for testing)"""
    global _config
    with _config_lock:
        _config = None

def save_config(config: GroundingConfig, path: Union[str, Path]) -> None:
    save_json(config.model_dump(), path)
    logger.info(f"Configuration saved to: {path}")


def load_agents_config() -> Dict[str, Any]:
    agents_config_path = CONFIG_DIR / CONFIG_AGENTS
    return _load_json_file(agents_config_path)


def get_agent_config(agent_name: str) -> Optional[Dict[str, Any]]:
    """
    Get the configuration of the specified agent
    """
    agents_config = load_agents_config()
    
    if "agents" not in agents_config:
        logger.warning(f"No 'agents' key found in {CONFIG_AGENTS}")
        return None
    
    for agent_cfg in agents_config.get("agents", []):
        if agent_cfg.get("name") == agent_name:
            return agent_cfg
    
    logger.warning(f"Agent '{agent_name}' not found in {CONFIG_AGENTS}")
    return None


__all__ = [
    "CONFIG_DIR",
    "load_config",
    "get_config",
    "reset_config",
    "save_config",
    "load_agents_config",
    "get_agent_config"
]