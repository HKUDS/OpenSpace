"""
Tool Quality Manager

Core API (called by main flow):
- record_execution(): Called by the PostToolUse quality_tracking hook
- adjust_ranking(): Called by ToolPreselector for quality-aware sorting

Query API (for inspection/debugging):
- get_quality_report(), get_tool_insights()
"""

from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from .types import ToolQualityRecord, ExecutionRecord
from .store import QualityStore
from openspace.utils.logging import Logger

if TYPE_CHECKING:
    from openspace.grounding.core.tool import BaseTool
    from openspace.grounding.core.types import ToolResult

logger = Logger.get_logger(__name__)


class ToolQualityManager:
    """
    Manages tool quality tracking and quality-aware ranking.
    
    Features:
    - Track execution success rate and latency
    - Persistent memory across sessions
    - Quality-integrated tool ranking
    """
    
    def __init__(
        self,
        *,
        db_path: Optional[Path] = None,
        enable_persistence: bool = True,
        auto_save: bool = True,
    ):
        self._enable_persistence = enable_persistence
        self._auto_save = auto_save

        # In-memory cache
        self._records: Dict[str, ToolQualityRecord] = {}
        self._global_execution_count: int = 0

        # Persistent store (SQLite, shares DB file with SkillStore)
        self._store = QualityStore(db_path=db_path) if enable_persistence else None

        # Load from DB
        if self._store:
            self._records, self._global_execution_count = self._store.load_all()

        logger.info(
            f"ToolQualityManager initialized "
            f"(persistence={enable_persistence}, records={len(self._records)}, "
            f"global_count={self._global_execution_count})"
        )

    @property
    def global_execution_count(self) -> int:
        return self._global_execution_count

    @property
    def store(self) -> QualityStore | None:
        return self._store

    def get_tool_key(self, tool: "BaseTool") -> str:
        """Generate unique key for a tool."""
        from openspace.grounding.core.types import BackendType
        
        if tool.is_bound:
            backend = tool.runtime_info.backend.value
            server = tool.runtime_info.server_name or "default"
        else:
            backend = tool.backend_type.value if tool.backend_type != BackendType.NOT_SET else "unknown"
            server = "default"
        
        return f"{backend}:{server}:{tool.name}"
    
    def get_record(self, tool: "BaseTool") -> ToolQualityRecord:
        """Get or create quality record for a tool."""
        key = self.get_tool_key(tool)
        
        if key not in self._records:
            backend, server, name = key.split(":", 2)
            self._records[key] = ToolQualityRecord(
                tool_key=key,
                backend=backend,
                server=server,
                tool_name=name,
            )
        
        return self._records[key]
    
    def get_quality_score(self, tool: "BaseTool") -> float:
        """Get quality score for a tool (0-1)."""
        return self.get_record(tool).quality_score
    
    # Key-based record access (for cross-system integration)
    def get_or_create_record_by_key(self, tool_key: str) -> ToolQualityRecord:
        """Get or create a ToolQualityRecord by its canonical key.

        Used by ExecutionAnalyzer integration where no BaseTool instance
        is available. Parses ``tool_key`` into backend/server/tool_name.

        Key formats:
          - ``backend:server:tool_name``   → three-part key (canonical for MCP)
          - ``backend:tool_name``          → two-part; tries ``backend:default:tool_name``
                                             first for matching existing records.
        """
        # 1. Direct match
        if tool_key in self._records:
            return self._records[tool_key]

        parts = tool_key.split(":", 2)
        if len(parts) == 3:
            backend, server, name = parts
        elif len(parts) == 2:
            backend, name = parts
            server = "default"
            # Try normalized 3-part key before creating a new record
            canonical = f"{backend}:default:{name}"
            if canonical in self._records:
                return self._records[canonical]
        else:
            backend, server, name = "unknown", "default", tool_key

        canonical_key = f"{backend}:{server}:{name}"
        if canonical_key in self._records:
            return self._records[canonical_key]

        record = ToolQualityRecord(
            tool_key=canonical_key,
            backend=backend,
            server=server,
            tool_name=name,
        )
        self._records[canonical_key] = record
        return record

    def find_record_by_key(self, key: str) -> Optional[ToolQualityRecord]:
        """Find a record by exact or partial tool key.

        Tries in order:
          1. Exact match (3-part ``backend:server:tool`` or 2-part)
          2. Normalized 2-part → ``backend:default:tool``
          3. Linear scan matching backend + tool_name (ignoring server)
        """
        # 1. Exact
        if key in self._records:
            return self._records[key]

        parts = key.split(":", 2)
        if len(parts) == 3:
            backend, server, tool_name = parts
            # Some LLM analyses emit placeholder keys such as
            # ``backend:default:ls`` instead of the runtime key
            # ``shell:default:ls``. Prefer an existing concrete record for the
            # same tool over creating a parallel aggregate bucket, but only
            # when that match is unambiguous.
            candidates = [
                record for record in self._records.values()
                if (
                    record.tool_name == tool_name
                    and (
                        record.backend == backend
                        or backend in {"backend", "tool", "unknown", "default"}
                    )
                    and (record.server == server or server == "default")
                )
            ]
            if len(candidates) == 1:
                return candidates[0]
        elif len(parts) == 2:
            backend, tool_name = parts
            # 2. Normalize
            canonical = f"{backend}:default:{tool_name}"
            if canonical in self._records:
                return self._records[canonical]
            # 3. Scan
            for record in self._records.values():
                if record.backend == backend and record.tool_name == tool_name:
                    return record
        return None

    # Execution Tracking
    async def record_execution(
        self,
        tool: "BaseTool",
        result: "ToolResult",
        execution_time_ms: float,
    ) -> ToolQualityRecord:
        """Record tool execution result and increment global counter."""
        error_message = None
        if result.is_error and result.error:
            error_message = str(result.error)[:500]

        return await self.record_outcome(
            tool,
            success=result.is_success,
            execution_time_ms=execution_time_ms,
            error_message=error_message,
        )

    async def record_outcome(
        self,
        tool: "BaseTool",
        *,
        success: bool,
        execution_time_ms: float = 0.0,
        error_message: Optional[str] = None,
    ) -> ToolQualityRecord:
        """Record a normalized pipeline outcome for a tool attempt."""
        record = self.get_record(tool)

        # Add execution record
        record.add_execution(ExecutionRecord(
            timestamp=datetime.now(),
            success=bool(success),
            execution_time_ms=execution_time_ms,
            error_message=error_message,
        ))
        
        # Increment global execution count
        self._global_execution_count += 1
        
        # Auto-save
        if self._auto_save and self._store:
            await self._store.save_record(record, self._records, self._global_execution_count)
        
        logger.debug(
            f"Recorded execution: {record.tool_key} "
            f"success={bool(success)} time={execution_time_ms:.0f}ms "
            f"(global_count={self._global_execution_count})"
        )
        return record
    
    # Quality-Aware Ranking
    def adjust_ranking(
        self,
        tools_with_scores: List[Tuple["BaseTool", float]],
    ) -> List[Tuple["BaseTool", float]]:
        """
        Adjust tool ranking using penalty-based approach.
           
        Args:
            tools_with_scores: List of (tool, semantic_score) tuples
        """
        adjusted = []
        for tool, semantic_score in tools_with_scores:
            penalty = self.get_penalty(tool)
            
            adjusted_score = semantic_score * penalty
            
            adjusted.append((tool, adjusted_score))
        
        # Sort by adjusted score (descending)
        adjusted.sort(key=lambda x: x[1], reverse=True)
        
        return adjusted
    
    def get_penalty(self, tool: "BaseTool") -> float:
        """Get penalty factor for a tool (0.2-1.0)."""
        return self.get_record(tool).penalty
    
    async def save(self) -> None:
        """
        Manually save all records to disk.
        
        Note: Usually not needed - auto_save handles persistence in
        record_execution().
        Provided as public API for explicit save when needed.
        """
        if self._store:
            await self._store.save_all(self._records)
    
    def clear_cache(self) -> None:
        """Clear all cached data."""
        self._records.clear()
        if self._store:
            self._store.clear()
    
    def get_stats(self) -> Dict:
        """
        Get quality tracking statistics.
        
        Note: Query API for inspection, may not be called in main flow.
        """
        if not self._records:
            return {"total_tools": 0}
        
        records = list(self._records.values())
        
        return {
            "total_tools": len(records),
            "total_executions": sum(r.total_calls for r in records),
            "avg_success_rate": (
                sum(r.success_rate for r in records) / len(records)
                if records else 0
            ),
            "avg_quality_score": (
                sum(r.quality_score for r in records) / len(records)
                if records else 0
            ),
        }

    def get_top_tools(
        self,
        n: int = 10,
        backend: Optional[str] = None,
        min_calls: int = 3,
    ) -> List[ToolQualityRecord]:
        """
        Get top N tools by quality score.
        
        Args:
            n: Number of tools to return
            backend: Filter by backend type (optional)
            min_calls: Minimum calls required (to filter untested tools)
        """
        records = [
            r for r in self._records.values()
            if r.total_calls >= min_calls
            and (backend is None or r.backend == backend)
        ]
        
        records.sort(key=lambda r: r.quality_score, reverse=True)
        return records[:n]
    
    def get_problematic_tools(
        self,
        success_rate_threshold: float = 0.5,
        min_calls: int = 5,
    ) -> List[ToolQualityRecord]:
        """
        Get tools with low success rate (candidates for review/removal).
        
        Args:
            success_rate_threshold: Tools below this rate are flagged
            min_calls: Minimum calls required (avoid flagging new tools)
        """
        return [
            r for r in self._records.values()
            if r.total_calls >= min_calls
            and r.recent_success_rate < success_rate_threshold
        ]
    
    def get_quality_report(self) -> Dict:
        """
        Generate comprehensive quality report for upper layer.
        
        Returns structured report with:
        - Overall stats
        - Per-backend breakdown
        - Top/problematic tools
        - Improvement suggestions
        """
        if not self._records:
            return {"status": "no_data", "message": "No quality data collected yet"}
        
        records = list(self._records.values())
        tested_records = [r for r in records if r.total_calls >= 3]
        
        # Per-backend stats
        backends = {}
        for r in records:
            if r.backend not in backends:
                backends[r.backend] = {
                    "tools": 0,
                    "total_calls": 0,
                    "success_count": 0,
                    "servers": set()
                }
            backends[r.backend]["tools"] += 1
            backends[r.backend]["total_calls"] += r.total_calls
            backends[r.backend]["success_count"] += r.success_count
            backends[r.backend]["servers"].add(r.server)
        
        # Convert sets to counts
        for b in backends:
            backends[b]["servers"] = len(backends[b]["servers"])
            backends[b]["success_rate"] = (
                backends[b]["success_count"] / backends[b]["total_calls"]
                if backends[b]["total_calls"] > 0 else 0
            )
        
        # Top and problematic tools
        top_tools = self.get_top_tools(5)
        problematic = self.get_problematic_tools()
        
        return {
            "summary": {
                "total_tools": len(records),
                "tested_tools": len(tested_records),
                "total_executions": sum(r.total_calls for r in records),
                "overall_success_rate": (
                    sum(r.success_count for r in records) /
                    max(1, sum(r.total_calls for r in records))
                ),
                "avg_quality_score": (
                    sum(r.quality_score for r in tested_records) / len(tested_records)
                    if tested_records else 0
                ),
            },
            "by_backend": backends,
            "top_tools": [
                {"key": r.tool_key, "score": r.quality_score, "success_rate": r.success_rate}
                for r in top_tools
            ],
            "problematic_tools": [
                {"key": r.tool_key, "success_rate": r.success_rate, "calls": r.total_calls}
                for r in problematic
            ],
            "recommendations": self._generate_recommendations(records, problematic),
        }
    
    def _generate_recommendations(
        self,
        records: List[ToolQualityRecord],
        problematic: List[ToolQualityRecord],
    ) -> List[str]:
        """Generate actionable recommendations based on quality data."""
        recommendations = []
        
        # Check for problematic tools
        if problematic:
            tool_names = [r.tool_name for r in problematic[:3]]
            recommendations.append(
                f"Review low-success tools: {', '.join(tool_names)}"
            )
        
        return recommendations

    def compute_adaptive_quality_weight(self) -> float:
        """
        Compute adaptive quality weight based on data confidence.
        
        Returns higher weight when we have more reliable quality data,
        lower weight when data is sparse.
        """
        if not self._records:
            return 0.1  # Low weight when no data
        
        records = list(self._records.values())
        tested_count = sum(1 for r in records if r.total_calls >= 3)
        
        if tested_count == 0:
            return 0.1
        
        # More tested tools -> higher confidence -> higher weight
        coverage = tested_count / len(records)
        
        # Average calls per tested tool -> data richness
        avg_calls = sum(r.total_calls for r in records) / len(records)
        richness = min(1.0, avg_calls / 20)  # Cap at 20 calls average
        
        # Combine coverage and richness
        confidence = (coverage * 0.5 + richness * 0.5)
        
        # Map to weight range [0.1, 0.5]
        weight = 0.1 + confidence * 0.4
        
        return round(weight, 2)
    
    def get_tool_insights(self, tool: "BaseTool") -> Dict:
        """
        Get detailed insights for a specific tool (for debugging/analysis).
        
        Returns comprehensive info about tool's quality history.
        """
        record = self._records.get(self.get_tool_key(tool))
        if not record:
            return {"status": "not_tracked", "tool": tool.name}
        
        # Count recent failures
        recent_failures_count = sum(
            1 for e in record.recent_executions[-20:]
            if not e.success
        )
        
        return {
            "tool_key": record.tool_key,
            "total_calls": record.total_calls,
            "success_rate": record.success_rate,
            "recent_success_rate": record.recent_success_rate,
            "avg_execution_time_ms": record.avg_execution_time_ms,
            "quality_score": record.quality_score,
            "recent_failures_count": recent_failures_count,
            "first_seen": record.first_seen.isoformat(),
            "last_updated": record.last_updated.isoformat(),
        }
