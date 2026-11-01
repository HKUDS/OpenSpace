from pathlib import Path

CONFIG_GROUNDING = "config_grounding.json"
CONFIG_SECURITY = "config_security.json"
CONFIG_MCP = "config_mcp.json"
CONFIG_DEV = "config_dev.json"
CONFIG_AGENTS = "config_agents.json"

LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# Project root directory (OpenSpace/)
PROJECT_ROOT = Path(__file__).parent.parent.parent


__all__ = [
    "CONFIG_GROUNDING",
    "CONFIG_SECURITY",
    "CONFIG_MCP",
    "CONFIG_DEV",
    "CONFIG_AGENTS",
    "LOG_LEVELS",
    "PROJECT_ROOT",
]