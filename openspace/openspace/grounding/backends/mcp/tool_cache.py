import json
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional

from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

# Cache path in project root directory (OpenSpace/)
# __file__ = .../OpenSpace/openspace/grounding/backends/mcp/tool_cache.py
# parent x5 = .../OpenSpace/
DEFAULT_CACHE_PATH = Path(__file__).parent.parent.parent.parent.parent / "mcp_tool_cache.json"
# Sanitized cache path (Claude API compatible JSON Schema)
DEFAULT_SANITIZED_CACHE_PATH = Path(__file__).parent.parent.parent.parent.parent / "mcp_tool_cache_sanitized.json"


class MCPToolCache:
    """Simple file-based cache for MCP tool metadata."""
    
    CACHE_VERSION = 1
    
    def __init__(self, cache_path: Optional[Path] = None, sanitized_cache_path: Optional[Path] = None):
        self.cache_path = cache_path or DEFAULT_CACHE_PATH
        self.sanitized_cache_path = sanitized_cache_path or DEFAULT_SANITIZED_CACHE_PATH
        self._cache: Optional[Dict] = None
        self._sanitized_cache: Optional[Dict] = None
        self._server_order: Optional[List[str]] = None
    
    def set_server_order(self, order: List[str]):
        """Set expected server order (from config). Used when saving to disk."""
        self._server_order = order
    
    def _reorder_servers(self, servers: Dict[str, List[Dict]]) -> Dict[str, List[Dict]]:
        """Reorder servers dict according to _server_order."""
        if not self._server_order:
            return servers
        
        ordered = {}
        # First add servers in config order
        for name in self._server_order:
            if name in servers:
                ordered[name] = servers[name]
        # Then add any remaining servers (not in config)
        for name in servers:
            if name not in ordered:
                ordered[name] = servers[name]
        return ordered
    
    def _ensure_dir(self):
        """Ensure cache directory exists."""
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
    
    def load(self) -> Dict[str, Any]:
        """Load cache from disk. Returns empty dict if not exists."""
        if self._cache is not None:
            return self._cache
        
        if not self.cache_path.exists():
            self._cache = {"version": self.CACHE_VERSION, "servers": {}}
            return self._cache
        
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                self._cache = json.load(f)
            logger.info(f"Loaded MCP tool cache: {len(self._cache.get('servers', {}))} servers")
            return self._cache
        except Exception as e:
            logger.warning(f"Failed to load cache: {e}")
            self._cache = {"version": self.CACHE_VERSION, "servers": {}}
            return self._cache
    
    def save(self, servers: Dict[str, List[Dict]]):
        """
        Save tool metadata to disk (overwrites existing cache).
        
        Args:
            servers: Dict mapping server_name -> list of tool metadata dicts
                     Each tool dict should have: name, description, parameters
        """
        self._ensure_dir()
        
        cache_data = {
            "version": self.CACHE_VERSION,
            "updated_at": datetime.now().isoformat(),
            "servers": servers,
        }
        
        try:
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, indent=2, ensure_ascii=False)
            self._cache = cache_data
            logger.info(f"Saved MCP tool cache: {len(servers)} servers")
        except Exception as e:
            logger.error(f"Failed to save cache: {e}")
    
    def save_server(self, server_name: str, tools: List[Dict]):
        """
        Save/update a single server's tools to cache (incremental append).
        
        Args:
            server_name: Name of the MCP server
            tools: List of tool metadata dicts for this server
        """
        self._ensure_dir()
        
        # Load existing cache
        cache = self.load()
        
        # Update server entry
        if "servers" not in cache:
            cache["servers"] = {}
        cache["servers"][server_name] = tools
        cache["servers"] = self._reorder_servers(cache["servers"])
        cache["updated_at"] = datetime.now().isoformat()
        
        # Save back
        try:
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(cache, f, indent=2, ensure_ascii=False)
            self._cache = cache
            logger.debug(f"Saved {len(tools)} tools for server '{server_name}'")
        except Exception as e:
            logger.error(f"Failed to save cache for server '{server_name}': {e}")
    
    def get_server_tools(self, server_name: str) -> Optional[List[Dict]]:
        """Get cached tools for a specific server."""
        cache = self.load()
        return cache.get("servers", {}).get(server_name)
    
    def get_all_tools(self) -> Dict[str, List[Dict]]:
        """Get all cached tools, grouped by server."""
        cache = self.load()
        return cache.get("servers", {})
    
    def has_cache(self) -> bool:
        """Check if cache exists and has data."""
        cache = self.load()
        return bool(cache.get("servers"))
    
    def clear(self):
        """Clear the cache."""
        if self.cache_path.exists():
            self.cache_path.unlink()
        self._cache = None
        logger.info("MCP tool cache cleared")
    
    def save_failed_server(self, server_name: str, error: str):
        """
        Record a failed server to cache.
        
        Args:
            server_name: Name of the failed MCP server
            error: Error message
        """
        self._ensure_dir()
        
        # Load existing cache
        cache = self.load()
        
        # Add to failed_servers list
        if "failed_servers" not in cache:
            cache["failed_servers"] = {}
        cache["failed_servers"][server_name] = {
            "error": error,
            "failed_at": datetime.now().isoformat(),
        }
        cache["updated_at"] = datetime.now().isoformat()
        
        # Save back
        try:
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(cache, f, indent=2, ensure_ascii=False)
            self._cache = cache
        except Exception as e:
            logger.error(f"Failed to save failed server '{server_name}': {e}")
    
    def get_failed_servers(self) -> Dict[str, Dict]:
        """Get list of failed servers from cache."""
        cache = self.load()
        return cache.get("failed_servers", {})
    
    def load_sanitized(self) -> Dict[str, Any]:
        """Load sanitized cache from disk. Returns empty dict if not exists."""
        if self._sanitized_cache is not None:
            return self._sanitized_cache
        
        if not self.sanitized_cache_path.exists():
            self._sanitized_cache = {"version": self.CACHE_VERSION, "servers": {}}
            return self._sanitized_cache
        
        try:
            with open(self.sanitized_cache_path, "r", encoding="utf-8") as f:
                self._sanitized_cache = json.load(f)
            logger.info(f"Loaded sanitized MCP tool cache: {len(self._sanitized_cache.get('servers', {}))} servers")
            return self._sanitized_cache
        except Exception as e:
            logger.warning(f"Failed to load sanitized cache: {e}")
            self._sanitized_cache = {"version": self.CACHE_VERSION, "servers": {}}
            return self._sanitized_cache
    
    def save_sanitized(self, servers: Dict[str, List[Dict]]):
        """
        Save sanitized tool metadata to disk.
        
        Args:
            servers: Dict mapping server_name -> list of sanitized tool metadata dicts
        """
        self._ensure_dir()
        
        cache_data = {
            "version": self.CACHE_VERSION,
            "updated_at": datetime.now().isoformat(),
            "sanitized": True,
            "servers": servers,
        }
        
        try:
            with open(self.sanitized_cache_path, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, indent=2, ensure_ascii=False)
            self._sanitized_cache = cache_data
            logger.info(f"Saved sanitized MCP tool cache: {len(servers)} servers")
        except Exception as e:
            logger.error(f"Failed to save sanitized cache: {e}")
    
    def get_all_sanitized_tools(self) -> Dict[str, List[Dict]]:
        """Get all sanitized cached tools, grouped by server."""
        cache = self.load_sanitized()
        return cache.get("servers", {})
    
    def has_sanitized_cache(self) -> bool:
        """Check if sanitized cache exists and has data."""
        cache = self.load_sanitized()
        return bool(cache.get("servers"))
    
    def clear_sanitized(self):
        """Clear the sanitized cache."""
        if self.sanitized_cache_path.exists():
            self.sanitized_cache_path.unlink()
        self._sanitized_cache = None
        logger.info("Sanitized MCP tool cache cleared")


# Global instance
_tool_cache: Optional[MCPToolCache] = None


def get_tool_cache() -> MCPToolCache:
    """Get global tool cache instance."""
    global _tool_cache
    if _tool_cache is None:
        _tool_cache = MCPToolCache()
    return _tool_cache

