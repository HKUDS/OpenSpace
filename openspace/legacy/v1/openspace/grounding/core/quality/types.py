"""
Data types for tool quality tracking.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import ClassVar, List, Optional


@dataclass
class ExecutionRecord:
    """Single execution record."""
    timestamp: datetime
    success: bool
    execution_time_ms: float
    error_message: Optional[str] = None


@dataclass
class DescriptionQuality:
    """LLM-evaluated description quality."""
    clarity: float  # 0-1: Is the purpose and usage clear?
    completeness: float  # 0-1: Are inputs/outputs documented?
    evaluated_at: datetime
    reasoning: str = ""  # LLM's reasoning for the scores
    
    @property
    def overall_score(self) -> float:
        """Computed overall score (average of all dimensions)."""
        return (self.clarity + self.completeness) / 2


@dataclass
class ToolQualityRecord:
    """
    Complete quality record for a tool.
    
    Key: "{backend}:{server}:{tool_name}"
    """
    tool_key: str
    backend: str
    server: str
    tool_name: str
    
    # Execution stats
    total_calls: int = 0
    success_count: int = 0
    total_execution_time_ms: float = 0.0
    
    # Recent execution history (rolling window)
    recent_executions: List[ExecutionRecord] = field(default_factory=list)
    
    # Description quality (LLM-evaluated)
    description_quality: Optional[DescriptionQuality] = None
    
    # LLM analysis feedback — how many times the analysis LLM flagged this tool.
    # LLM-identified issues are also injected into recent_executions as
    # ExecutionRecord(success=False, error_message="[LLM] ...") so they feed
    # into the same recent_success_rate → penalty pipeline as rule-based tracking.
    llm_flagged_count: int = 0
    
    # Metadata
    description_hash: Optional[str] = None
    first_seen: datetime = field(default_factory=datetime.now)
    last_updated: datetime = field(default_factory=datetime.now)
    
    # Keep only recent N executions
    MAX_RECENT_EXECUTIONS: ClassVar[int] = 100
    
    # Penalty threshold: only penalize tools with success rate below this value
    # Tools with success rate >= this threshold get penalty = 1.0 (no penalty)
    PENALTY_THRESHOLD: ClassVar[float] = 0.4
    
    @property
    def success_rate(self) -> float:
        """Overall success rate."""
        if self.total_calls == 0:
            return 0.0
        return self.success_count / self.total_calls
    
    @property
    def avg_execution_time_ms(self) -> float:
        """Average execution time."""
        if self.total_calls == 0:
            return 0.0
        return self.total_execution_time_ms / self.total_calls
    
    @property
    def recent_success_rate(self) -> float:
        """Success rate from recent executions."""
        if not self.recent_executions:
            return self.success_rate
        successes = sum(1 for e in self.recent_executions if e.success)
        return successes / len(self.recent_executions)
    
    @property
    def consecutive_failures(self) -> int:
        """Count consecutive failures from the most recent execution."""
        count = 0
        for exec_record in reversed(self.recent_executions):
            if not exec_record.success:
                count += 1
            else:
                break
        return count
    
    @property
    def penalty(self) -> float:
        """
        Compute penalty factor based on failure rate.
        
        Design principles:
        - Only penalize tools with success rate < PENALTY_THRESHOLD (default 40%)
        - New tools (< 3 calls) get no penalty to allow fair evaluation
        
        Returns value between 0.2-1.0:
        - 1.0: No penalty (success rate >= threshold or insufficient data)
        - 0.2: Maximum penalty (consistently failing tool)
        """
        if self.total_calls < 3:
            return 1.0
        
        success_rate = self.recent_success_rate
        threshold = self.PENALTY_THRESHOLD
        
        if success_rate >= threshold:
            return 1.0
        
        # Linear mapping: penalty = 0.3 + (success_rate / threshold) * 0.7
        base_penalty = 0.3 + (success_rate / threshold) * 0.7
        
        # Extra penalty for consecutive failures (indicates systematic issues)
        consec = self.consecutive_failures
        if consec >= 3:
            # 3 consecutive → extra 0.1, 5 consecutive → extra 0.3
            extra_penalty = min(0.3, (consec - 2) * 0.1)
            base_penalty -= extra_penalty
        
        # Clamp to [0.2, 1.0]
        return max(0.2, min(1.0, base_penalty))
    
    @property
    def quality_score(self) -> float:
        """
        Legacy quality score for backward compatibility.
        Now delegates to penalty property.
        """
        return self.penalty
    
    def add_llm_issue(self, description: str) -> None:
        """Record an LLM-identified issue as a failure in recent_executions.

        Unlike ``add_execution()``, this does NOT increment ``total_calls``
        or ``total_execution_time_ms`` — the real execution was already
        counted by the rule-based system.  The LLM's qualitative judgment
        supplements it by catching semantic failures (e.g. HTTP 200 but
        wrong data) that rule-based tracking missed.

        The injected record feeds into ``recent_success_rate`` → ``penalty``,
        so one unified quality metric drives ranking and future batch updates.
        """
        self.llm_flagged_count += 1
        self.recent_executions.append(ExecutionRecord(
            timestamp=datetime.now(),
            success=False,
            execution_time_ms=0.0,
            error_message=f"[LLM] {description}",
        ))
        if len(self.recent_executions) > self.MAX_RECENT_EXECUTIONS:
            self.recent_executions = self.recent_executions[-self.MAX_RECENT_EXECUTIONS:]
        self.last_updated = datetime.now()

    def add_execution(self, record: ExecutionRecord) -> None:
        """Add execution record and update stats."""
        self.total_calls += 1
        self.total_execution_time_ms += record.execution_time_ms
        
        if record.success:
            self.success_count += 1
        
        self.recent_executions.append(record)
        
        # Trim to max size
        if len(self.recent_executions) > self.MAX_RECENT_EXECUTIONS:
            self.recent_executions = self.recent_executions[-self.MAX_RECENT_EXECUTIONS:]
        
        self.last_updated = datetime.now()
