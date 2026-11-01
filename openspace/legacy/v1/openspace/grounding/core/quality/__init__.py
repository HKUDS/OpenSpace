from .types import ToolQualityRecord, ExecutionRecord, DescriptionQuality
from .manager import ToolQualityManager
from .store import QualityStore

# Global manager instance
_global_manager: "ToolQualityManager | None" = None


def get_quality_manager() -> "ToolQualityManager | None":
    """Get the global quality manager instance."""
    return _global_manager


def set_quality_manager(manager: "ToolQualityManager") -> None:
    """Set the global quality manager instance."""
    global _global_manager
    _global_manager = manager


__all__ = [
    "ToolQualityRecord",
    "ExecutionRecord",
    "DescriptionQuality",
    "ToolQualityManager",
    "QualityStore",
    "get_quality_manager",
    "set_quality_manager",
]
