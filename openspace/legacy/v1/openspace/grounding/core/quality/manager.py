"""
Tool Quality Manager

Core API (called by main flow):
- record_execution(): Called by BaseTool after execution
- adjust_ranking(): Called by SearchCoordinator for quality-aware sorting
- evolve(): Called periodically by ToolLayer for self-evolution

Query API (for inspection/debugging):
- get_quality_report(), get_tool_insights()
"""

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from .types import ToolQualityRecord, ExecutionRecord, DescriptionQuality
from .store import QualityStore
from openspace.utils.logging import Logger

if TYPE_CHECKING:
    from openspace.grounding.core.tool import BaseTool
    from openspace.grounding.core.types import ToolResult
    from openspace.llm import LLMClient

logger = Logger.get_logger(__name__)


class ToolQualityManager:
    """
    Manages tool quality tracking and quality-aware ranking.
    
    Features:
    - Track execution success rate and latency
    - LLM-based description quality evaluation (optional, requires llm_client)
    - Persistent memory across sessions
    - Quality-integrated tool ranking
    - Incremental update detection
    """
    
    def __init__(
        self,
        *,
        db_path: Optional[Path] = None,
        cache_dir: Optional[Path] = None,  # deprecated, ignored
        llm_client: Optional["LLMClient"] = None,
        enable_persistence: bool = True,
        auto_save: bool = True,
        evolve_interval: int = 5,
    ):
        self._llm_client = llm_client
        self._enable_persistence = enable_persistence
        self._auto_save = auto_save
        self._evolve_interval = evolve_interval

        # In-memory cache
        self._records: Dict[str, ToolQualityRecord] = {}
        self._global_execution_count: int = 0
        self._last_evolve_count: int = 0

        # Persistent store (SQLite, shares DB file with SkillStore)
        self._store = QualityStore(db_path=db_path) if enable_persistence else None

        # Load from DB
        if self._store:
            self._records, self._global_execution_count = self._store.load_all()
            self._last_evolve_count = (
                (self._global_execution_count // self._evolve_interval)
                * self._evolve_interval
            )

        logger.info(
            f"ToolQualityManager initialized "
            f"(persistence={enable_persistence}, records={len(self._records)}, "
            f"global_count={self._global_execution_count}, "
            f"evolve_interval={self._evolve_interval})"
        )

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
    
    def _compute_description_hash(self, tool: "BaseTool") -> str:
        """Compute hash of tool description for change detection."""
        content = f"{tool.name}|{tool.description or ''}|{tool.schema.parameters}"
        return hashlib.md5(content.encode()).hexdigest()[:16]

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
                description_hash=self._compute_description_hash(tool),
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
        if len(parts) == 2:
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

    async def record_llm_tool_issues(
        self, tool_issues: List[str], task_id: str = "",
    ) -> int:
        """Record LLM-identified tool issues into the quality tracking system.

        Each issue is injected as a failed ``ExecutionRecord`` in the tool's
        ``recent_executions`` via ``add_llm_issue()``, so it feeds into the
        same ``recent_success_rate`` → ``penalty`` pipeline as rule-based
        tracking.  This means one unified set of quality metrics drives
        ranking adjustments and future batch skill updates.

        The LLM may catch semantic failures (HTTP 200 but wrong data,
        misleading output, etc.) that rule-based tracking misses.

        Args:
            tool_issues: List of ``"key — description"`` strings.
                Key formats: ``mcp:server:tool`` or ``backend:tool``.
            task_id: Task ID for traceability (optional).
        """
        updated = 0
        for issue in tool_issues:
            # Parse "key — description" or "key - description"
            if "—" in issue:
                key_part, _, description = issue.partition("—")
            elif " - " in issue:
                key_part, _, description = issue.partition(" - ")
            else:
                key_part, description = issue, ""
            key_part = key_part.strip()
            description = description.strip()
            if not key_part:
                continue

            record = self.find_record_by_key(key_part)
            if record is None:
                record = self.get_or_create_record_by_key(key_part)

            # Inject into the unified quality pipeline
            tag = f"(task={task_id}) " if task_id else ""
            record.add_llm_issue(f"{tag}{description}" if description else f"{tag}flagged by analysis LLM")
            updated += 1

        # Persist
        if updated and self._auto_save and self._store:
            await self._store.save_all(self._records, self._global_execution_count)

        if updated:
            logger.info(
                f"Recorded {updated} LLM tool issue(s) into quality pipeline"
                f"{f' (task={task_id})' if task_id else ''}"
            )
        return updated

    def get_llm_flagged_tools(
        self, min_flags: int = 2,
    ) -> List[ToolQualityRecord]:
        """Get tools repeatedly flagged by the analysis LLM.

        Useful for identifying tools whose descriptions, reliability, or
        behavior may need attention — and for batch-triggering skill
        updates on skills that depend on those tools.

        Each returned record carries LLM-identified failures in its
        ``recent_executions`` (prefixed ``[LLM]``), providing actionable context.

        Args:
            min_flags: Minimum number of LLM flags to include.
        """
        return [
            r for r in self._records.values()
            if r.llm_flagged_count >= min_flags
        ]

    # Execution Tracking
    async def record_execution(
        self,
        tool: "BaseTool",
        result: "ToolResult",
        execution_time_ms: float,
    ) -> None:
        """Record tool execution result and increment global counter."""
        record = self.get_record(tool)
        
        # Extract error message if failed
        error_message = None
        if result.is_error and result.error:
            error_message = str(result.error)[:500]
        
        # Add execution record
        record.add_execution(ExecutionRecord(
            timestamp=datetime.now(),
            success=result.is_success,
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
            f"success={result.is_success} time={execution_time_ms:.0f}ms "
            f"(global_count={self._global_execution_count})"
        )
    
    async def evaluate_description(
        self,
        tool: "BaseTool",
        force: bool = False,
    ) -> Optional[DescriptionQuality]:
        """
        Evaluate tool description quality using LLM.
        """
        try:
            from gdpval_bench.token_tracker import set_call_source, reset_call_source
            _src_tok = set_call_source("quality")
        except ImportError:
            _src_tok = None

        if not self._llm_client:
            logger.debug("LLM client not available for description evaluation")
            if _src_tok is not None:
                reset_call_source(_src_tok)
            return None
        
        record = self.get_record(tool)
        
        # Skip if already evaluated and not forced
        if record.description_quality and not force:
            # Check if description changed
            current_hash = self._compute_description_hash(tool)
            if current_hash == record.description_hash:
                return record.description_quality
        
        # Build evaluation prompt
        desc = tool.description or "No description provided"
        if len(desc) > 4000:
            desc = desc[:4000] + "\n... (truncated for length)"
        
        params = tool.schema.parameters or {}
        if params:
            param_lines = []
            # Extract parameter names and types from JSON schema
            if "properties" in params:
                for param_name, param_info in params.get("properties", {}).items():
                    param_type = param_info.get("type", "unknown")
                    param_desc = param_info.get("description", "")
                    param_lines.append(f"- {param_name} ({param_type}): {param_desc}" if param_desc else f"- {param_name} ({param_type})")
            param_text = "\n".join(param_lines) if param_lines else "No parameter descriptions available"
        else:
            param_text = "No parameters"
        
        prompt = f"""# Task: Evaluate this tool's documentation quality

## Tool Information

Name: {tool.name}

Description:
{desc}

Parameters:
{param_text}

## Evaluation Task

Rate the documentation on two dimensions (0.0 to 1.0 scale):

### 1. Clarity
How clear is the tool's purpose and usage?

- 0.0-0.3: No description or completely unclear
- 0.4-0.6: Basic purpose but vague
- 0.7-0.8: Clear purpose and functionality
- 0.9-1.0: Very clear with usage examples or context

### 2. Completeness
Are inputs/outputs properly documented?

- 0.0-0.3: Missing critical information
- 0.4-0.6: Basic info but lacks details
- 0.7-0.8: Well documented with types
- 0.9-1.0: Comprehensive with constraints and examples

## Scoring Guidelines

- Short descriptions can score high if clear and accurate
- If parameters exist but aren't explained in description, reduce completeness score
- Missing description means clarity = 0.0

## Output

Respond with JSON only:

```json
{{
  "reasoning": "Brief 1-2 sentence analysis",
  "clarity": 0.8,
  "completeness": 0.7
}}
```"""

        try:
            response = await self._llm_client.complete(prompt)
            content = response["message"]["content"]
            
            # Parse JSON response
            import json
            
            # Extract complete JSON object
            def extract_json_object(text: str) -> str | None:
                """Extract first complete JSON object from text by counting braces."""
                start = text.find('{')
                if start == -1:
                    return None
                
                count = 0
                in_string = False
                escape_next = False
                
                for i, char in enumerate(text[start:], start):
                    if escape_next:
                        escape_next = False
                        continue
                    
                    if char == '\\':
                        escape_next = True
                        continue
                    
                    if char == '"' and not escape_next:
                        in_string = not in_string
                        continue
                    
                    if not in_string:
                        if char == '{':
                            count += 1
                        elif char == '}':
                            count -= 1
                            if count == 0:
                                return text[start:i+1]
                return None
            
            json_str = extract_json_object(content)
            if not json_str:
                logger.warning(f"Could not find JSON in LLM response for {tool.name}")
                return None
            
            data = json.loads(json_str)
            
            # Extract and validate scores with robust error handling
            def safe_float(value, default=0.5, min_val=0.0, max_val=1.0):
                """Safely convert to float and clamp to valid range."""
                try:
                    if value is None:
                        return default
                    f = float(value)
                    return max(min_val, min(max_val, f))
                except (ValueError, TypeError):
                    logger.warning(f"Invalid score value: {value}, using default {default}")
                    return default
            
            clarity = safe_float(data.get("clarity"), default=0.5)
            completeness = safe_float(data.get("completeness"), default=0.5)
            reasoning = str(data.get("reasoning", ""))[:500]  # Limit reasoning length
            
            quality = DescriptionQuality(
                clarity=clarity,
                completeness=completeness,
                evaluated_at=datetime.now(),
                reasoning=reasoning,
            )
            
            # Update record
            record.description_quality = quality
            record.description_hash = self._compute_description_hash(tool)
            record.last_updated = datetime.now()
            
            # Save
            if self._auto_save and self._store:
                await self._store.save_record(record, self._records, self._global_execution_count)
            
            logger.info(f"Evaluated description: {tool.name} score={quality.overall_score:.2f}")
            return quality
            
        except Exception as e:
            logger.error(f"Description evaluation failed for {tool.name}: {e}")
            return None
        finally:
            if _src_tok is not None:
                reset_call_source(_src_tok)

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
    
    # Change Detection
    def check_changes(self, tools: List["BaseTool"]) -> Dict[str, str]:
        """
        Check for tool changes (new/updated/unchanged).
        
        Returns dict: {tool_key: "new"|"updated"|"unchanged"}
        """
        changes = {}
        
        for tool in tools:
            key = self.get_tool_key(tool)
            current_hash = self._compute_description_hash(tool)
            
            if key not in self._records:
                changes[key] = "new"
            elif self._records[key].description_hash != current_hash:
                changes[key] = "updated"
                # Clear old evaluation on description change
                self._records[key].description_quality = None
                self._records[key].description_hash = current_hash
            else:
                changes[key] = "unchanged"
        
        new_count = sum(1 for v in changes.values() if v == "new")
        updated_count = sum(1 for v in changes.values() if v == "updated")
        
        if new_count or updated_count:
            logger.info(f"Tool changes: {new_count} new, {updated_count} updated")
        
        return changes
    
    async def save(self) -> None:
        """
        Manually save all records to disk.
        
        Note: Usually not needed - auto_save handles persistence in
        record_execution(), evaluate_description(), and evolve().
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
            "tools_with_description_eval": sum(
                1 for r in records if r.description_quality
            ),
            "tools_llm_flagged": sum(
                1 for r in records if r.llm_flagged_count > 0
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
        
        # Check for tools needing description evaluation
        unevaluated = [r for r in records if not r.description_quality and r.total_calls >= 3]
        if unevaluated:
            recommendations.append(
                f"{len(unevaluated)} tools need description quality evaluation"
            )
        
        # Check for low description quality
        poor_docs = [
            r for r in records
            if r.description_quality and r.description_quality.overall_score < 0.5
        ]
        if poor_docs:
            recommendations.append(
                f"{len(poor_docs)} tools have poor documentation quality"
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
    
    def should_reevaluate_description(self, tool: "BaseTool") -> bool:
        """
        Check if a tool's description should be re-evaluated.
        
        Triggers re-evaluation when:
        - Description hash changed
        - Success rate dropped significantly
        - No evaluation yet but enough calls
        """
        record = self._records.get(self.get_tool_key(tool))
        if not record:
            return True
        
        # Check hash change
        current_hash = self._compute_description_hash(tool)
        if current_hash != record.description_hash:
            return True
        
        # No evaluation yet but enough data
        if not record.description_quality and record.total_calls >= 5:
            return True
        
        # Success rate dropped significantly (maybe description is misleading)
        if record.description_quality and record.total_calls >= 10:
            if record.recent_success_rate < 0.5 and record.description_quality.overall_score > 0.7:
                # High doc quality but low success -> mismatch
                return True
        
        return False
    
    async def evolve(self, tools: List["BaseTool"]) -> Dict:
        """
        Run self-evolution cycle on given tools.
        
        This method:
        1. Detects tool changes
        2. Re-evaluates descriptions where needed
        3. Updates quality weights
        4. Returns evolution report
        """
        report = {
            "changes_detected": {},
            "descriptions_evaluated": 0,
            "adaptive_weight": 0.0,
            "recommendations": [],
        }
        
        # 1. Detect changes
        report["changes_detected"] = self.check_changes(tools)
        
        # 2. Find tools needing re-evaluation
        needs_eval = [t for t in tools if self.should_reevaluate_description(t)]
        
        # 3. Evaluate descriptions (limit to avoid too many LLM calls)
        if needs_eval and self._llm_client:
            for tool in needs_eval[:5]:  # Max 5 per cycle
                result = await self.evaluate_description(tool, force=True)
                if result:
                    report["descriptions_evaluated"] += 1
        
        # 4. Compute adaptive weight
        report["adaptive_weight"] = self.compute_adaptive_quality_weight()
        
        # 5. Generate recommendations
        problematic = self.get_problematic_tools()
        report["recommendations"] = self._generate_recommendations(
            list(self._records.values()), problematic
        )
        
        # 6. Update last evolve count
        self._last_evolve_count = self._global_execution_count
        
        # Save
        if self._store:
            await self._store.save_all(self._records, self._global_execution_count)
        
        logger.info(
            f"Evolution cycle complete: "
            f"changes={len([v for v in report['changes_detected'].values() if v != 'unchanged'])}, "
            f"evaluated={report['descriptions_evaluated']}, "
            f"weight={report['adaptive_weight']}, "
            f"global_count={self._global_execution_count}"
        )
        
        return report
    
    def should_evolve(self) -> bool:
        """Check if evolution should be triggered based on global execution count."""
        return self._global_execution_count >= self._last_evolve_count + self._evolve_interval
    
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
            "description_quality": {
                "overall_score": record.description_quality.overall_score,
                "clarity": record.description_quality.clarity,
                "completeness": record.description_quality.completeness,
                "reasoning": record.description_quality.reasoning,
            } if record.description_quality else None,
            "llm_flagged_count": record.llm_flagged_count,
            "recent_failures_count": recent_failures_count,
            "first_seen": record.first_seen.isoformat(),
            "last_updated": record.last_updated.isoformat(),
        }
