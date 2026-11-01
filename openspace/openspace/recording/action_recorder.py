"""
Agent Action Recorder

Records agent decision-making processes, reasoning, and outputs.
Focuses on high-level agent behaviors rather than low-level tool executions.
"""

import datetime
import json
from typing import Any, Dict, Optional
from pathlib import Path

from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


class ActionRecorder:
    """
    Records agent actions and decision-making processes.
    
    This recorder captures the 'thinking' layer of the agent:
    - Task planning and decomposition
    - Tool selection reasoning
    - Evaluation decisions
    """
    
    def __init__(self, trajectory_dir: Path):
        """
        Initialize action recorder.
        
        Args:
            trajectory_dir: Directory to save action records
        """
        self.trajectory_dir = trajectory_dir
        self.actions_file = trajectory_dir / "agent_actions.jsonl"
        self.step_counter = 0
        
        # Ensure directory exists
        self.trajectory_dir.mkdir(parents=True, exist_ok=True)
    
    async def record_action(
        self,
        agent_name: str,
        action_type: str,
        input_data: Optional[Dict[str, Any]] = None,
        reasoning: Optional[Dict[str, Any]] = None,
        output_data: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        related_tool_steps: Optional[list] = None,
        correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Record an agent action.
        
        Args:
            agent_name: Name of the agent performing the action
            action_type: Type of action (plan | execute | evaluate | monitor)
            input_data: Input data the agent received (simplified)
            reasoning: Agent's reasoning process (structured)
            output_data: Agent's output/decision (structured)
            metadata: Additional metadata (LLM model, tokens, duration, etc.)
            related_tool_steps: List of tool execution step numbers related to this action
            correlation_id: Optional correlation ID to link related events
        """
        self.step_counter += 1
        timestamp = datetime.datetime.now().isoformat()
        
        # Infer agent type from agent name
        agent_type = self._infer_agent_type(agent_name)
        
        action_info = {
            "step": self.step_counter,
            "timestamp": timestamp,
            "agent_name": agent_name,
            "agent_type": agent_type, 
            "action_type": action_type,
            "correlation_id": correlation_id or f"action_{self.step_counter}_{timestamp}", 
        }
        
        # Add input (with smart truncation)
        if input_data:
            action_info["input"] = self._truncate_data(input_data, max_length=1000)
        
        # Add reasoning (keep structured)
        if reasoning:
            action_info["reasoning"] = self._truncate_data(reasoning, max_length=2000)
        
        # Add output (keep structured)
        if output_data:
            action_info["output"] = self._truncate_data(output_data, max_length=1000)
        
        # Add metadata
        if metadata:
            action_info["metadata"] = metadata
        
        # Add related tool steps for correlation
        if related_tool_steps:
            action_info["related_tool_steps"] = related_tool_steps
        
        # Append to JSONL file
        await self._append_to_file(action_info)
        
        logger.debug(
            f"Recorded {action_type} action from {agent_name} (step {self.step_counter})"
        )
        
        return action_info
    
    def _infer_agent_type(self, agent_name: str) -> str:
        name_lower = agent_name.lower()
        
        if "host" in name_lower:
            return "host"
        elif "grounding" in name_lower:
            return "grounding"
        elif "eval" in name_lower:
            return "eval"
        elif "coordinator" in name_lower:
            return "coordinator"
        else:
            return "unknown"
    
    def _truncate_data(self, data: Any, max_length: int) -> Any:
        if isinstance(data, str):
            if len(data) > max_length:
                return data[:max_length] + "... [truncated]"
            return data
        
        elif isinstance(data, dict):
            result = {}
            for key, value in data.items():
                if isinstance(value, str) and len(value) > max_length:
                    result[key] = value[:max_length] + "... [truncated]"
                elif isinstance(value, (dict, list)):
                    # Recursively truncate nested structures
                    result[key] = self._truncate_data(value, max_length)
                else:
                    result[key] = value
            return result
        
        elif isinstance(data, list):
            # Truncate list items
            result = []
            for item in data:
                if isinstance(item, str) and len(item) > max_length:
                    result.append(item[:max_length] + "... [truncated]")
                elif isinstance(item, (dict, list)):
                    result.append(self._truncate_data(item, max_length))
                else:
                    result.append(item)
            return result
        
        else:
            return data
    
    async def _append_to_file(self, action_info: Dict[str, Any]):
        """Append action to JSONL file."""
        with open(self.actions_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(action_info, ensure_ascii=False))
            f.write("\n")
    
    def get_step_count(self) -> int:
        """Get current step count."""
        return self.step_counter


def load_agent_actions(trajectory_dir: str) -> list:
    """
    Load agent actions from a trajectory directory.
    """
    actions_file = Path(trajectory_dir) / "agent_actions.jsonl"
    
    if not actions_file.exists():
        logger.warning(f"Agent actions file not found: {actions_file}")
        return []
    
    actions = []
    try:
        with open(actions_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    actions.append(json.loads(line))
        
        logger.info(f"Loaded {len(actions)} agent actions from {actions_file}")
        return actions
    
    except Exception as e:
        logger.error(f"Failed to load agent actions from {actions_file}: {e}")
        return []


def analyze_agent_actions(actions: list) -> Dict[str, Any]:
    """
    Analyze agent actions and generate statistics.
    """
    if not actions:
        return {
            "total_actions": 0,
            "by_agent": {},
            "by_type": {},
        }
    
    # Count by agent
    by_agent = {}
    by_type = {}
    
    for action in actions:
        agent_name = action.get("agent_name", "unknown")
        action_type = action.get("action_type", "unknown")
        
        by_agent[agent_name] = by_agent.get(agent_name, 0) + 1
        by_type[action_type] = by_type.get(action_type, 0) + 1
    
    return {
        "total_actions": len(actions),
        "by_agent": by_agent,
        "by_type": by_type,
    }


def format_agent_actions(actions: list, format_type: str = "compact") -> str:
    """
    Format agent actions for display.
    """
    if not actions:
        return "No agent actions recorded"
    
    if format_type == "compact":
        lines = []
        for action in actions:
            step = action.get("step", "?")
            agent = action.get("agent_name", "?")
            action_type = action.get("action_type", "?")
            
            # Try to extract key info from reasoning or output
            key_info = ""
            if action.get("reasoning"):
                thought = action["reasoning"].get("thought", "")
                if thought:
                    key_info = f": {thought[:60]}..."
            
            lines.append(f"Step {step}: [{agent}] {action_type}{key_info}")
        
        return "\n".join(lines)
    
    elif format_type == "detailed":
        lines = []
        for action in actions:
            lines.append(f"\n{'='*60}")
            lines.append(f"Step {action.get('step', '?')}: {action.get('agent_name', '?')}")
            lines.append(f"Type: {action.get('action_type', '?')}")
            lines.append(f"Time: {action.get('timestamp', '?')}")
            
            if action.get("reasoning"):
                lines.append("\nReasoning:")
                lines.append(json.dumps(action["reasoning"], indent=2, ensure_ascii=False))
            
            if action.get("output"):
                lines.append("\nOutput:")
                lines.append(json.dumps(action["output"], indent=2, ensure_ascii=False))
            
            if action.get("metadata"):
                lines.append("\nMetadata:")
                lines.append(json.dumps(action["metadata"], indent=2, ensure_ascii=False))
        
        return "\n".join(lines)
    
    else:
        raise ValueError(f"Unknown format type: {format_type}")