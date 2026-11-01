"""
Evolution trace logging for OpenEvolve

This module provides functionality to log detailed traces of program evolution,
capturing state-action-reward transitions for RL training and analysis.
"""

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from rsi_arena.baselines.openevolve.runtime.utils.trace_export_utils import (
    append_trace_jsonl,
    export_traces,
    export_traces_json,
)

logger = logging.getLogger(__name__)


@dataclass
class EvolutionTrace:
    """Represents a single evolution trace entry"""

    iteration: int
    timestamp: float
    parent_id: str
    child_id: str
    parent_metrics: Dict[str, Any]
    child_metrics: Dict[str, Any]
    parent_code: Optional[str] = None
    child_code: Optional[str] = None
    parent_changes_description: Optional[str] = None
    child_changes_description: Optional[str] = None
    code_diff: Optional[str] = None
    prompt: Optional[Dict[str, str]] = None
    llm_response: Optional[str] = None
    improvement_delta: Optional[Dict[str, float]] = None
    island_id: Optional[int] = None
    generation: Optional[int] = None
    artifacts: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert trace to dictionary format"""
        return {k: v for k, v in asdict(self).items() if v is not None}

    def calculate_improvement(self) -> Dict[str, float]:
        """Calculate improvement deltas between parent and child metrics"""
        improvement = {}
        for key in self.child_metrics:
            if key in self.parent_metrics:
                parent_val = self.parent_metrics[key]
                child_val = self.child_metrics[key]
                if isinstance(parent_val, (int, float)) and isinstance(child_val, (int, float)):
                    improvement[key] = child_val - parent_val
        return improvement


class EvolutionTracer:
    """Manages evolution trace logging with support for multiple formats"""

    def __init__(
        self,
        output_path: Optional[str] = None,
        format: str = "jsonl",
        include_code: bool = False,
        include_prompts: bool = True,
        enabled: bool = True,
        buffer_size: int = 10,
        compress: bool = False,
        include_changes_description: bool = True,
    ):
        """
        Initialize the evolution tracer

        Args:
            output_path: Path to save trace data
            format: Output format ('jsonl', 'json', 'hdf5')
            include_code: Whether to include full code in traces
            include_prompts: Whether to include prompts and LLM responses
            enabled: Whether tracing is enabled
            buffer_size: Number of traces to buffer before writing
            compress: Whether to compress output files
            include_changes_description: Whether to include per-program changes descriptions
        """
        self.enabled = enabled
        self.format = format
        self.include_code = include_code
        self.include_prompts = include_prompts
        self.include_changes_description = include_changes_description
        self.compress = compress
        self.buffer_size = buffer_size

        # Track statistics
        self.stats = {
            "total_traces": 0,
            "improvement_count": 0,
            "total_improvement": {},
            "best_improvement": {},
            "worst_decline": {},
        }

        if not self.enabled:
            logger.info("Evolution tracing is disabled")
            return

        # Set up output path
        if output_path:
            self.output_path = Path(output_path)
        else:
            self.output_path = Path(f"evolution_trace.{format}")

        # Add compression extension if needed
        if self.compress and format == "jsonl":
            self.output_path = self.output_path.with_suffix(".jsonl.gz")

        # Create parent directory if needed
        self.output_path.parent.mkdir(parents=True, exist_ok=True)

        # Initialize buffer for batched writing
        self.buffer: List[EvolutionTrace] = []

        # For JSON format, keep all traces in memory
        if format == "json":
            self.json_traces = []

        logger.info(f"Evolution tracer initialized: {self.output_path}")

    def log_trace(
        self,
        iteration: int,
        parent_program: Any,
        child_program: Any,
        prompt: Optional[Dict[str, str]] = None,
        llm_response: Optional[str] = None,
        artifacts: Optional[Dict[str, Any]] = None,
        island_id: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Log an evolution trace entry

        Args:
            iteration: Current iteration number
            parent_program: Parent program object
            child_program: Child program object
            prompt: Prompt used for evolution
            llm_response: LLM response
            artifacts: Any artifacts from evaluation
            island_id: Island ID if using island-based evolution
            metadata: Additional metadata
        """
        if not self.enabled:
            return

        try:
            # Create trace entry
            trace = EvolutionTrace(
                iteration=iteration,
                timestamp=time.time(),
                parent_id=parent_program.id,
                child_id=child_program.id,
                parent_metrics=parent_program.metrics,
                child_metrics=child_program.metrics,
                island_id=island_id,
                generation=child_program.generation,
                artifacts=artifacts,
                metadata=metadata,
            )

            # Optionally include code
            if self.include_code:
                trace.parent_code = parent_program.code
                trace.child_code = child_program.code

            # Changes descriptions (large-codebase mode)
            if self.include_changes_description:
                trace.parent_changes_description = getattr(parent_program, "changes_description", None)
                trace.child_changes_description = getattr(child_program, "changes_description", None)

            # Optionally include prompts
            if self.include_prompts:
                trace.prompt = prompt
                trace.llm_response = llm_response

            # Calculate improvement
            trace.improvement_delta = trace.calculate_improvement()

            # Update statistics
            self._update_stats(trace)

            # Add to buffer
            self.buffer.append(trace)

            # For JSON format, also keep in memory
            if self.format == "json":
                self.json_traces.append(trace)

            # Write if buffer is full
            if len(self.buffer) >= self.buffer_size:
                self.flush()

        except Exception as e:
            logger.error(f"Error logging evolution trace: {e}")

    def _update_stats(self, trace: EvolutionTrace):
        """Update running statistics"""
        self.stats["total_traces"] += 1

        if trace.improvement_delta:
            # Check if there's improvement in combined_score
            if "combined_score" in trace.improvement_delta:
                delta = trace.improvement_delta["combined_score"]
                if delta > 0:
                    self.stats["improvement_count"] += 1

            # Track cumulative improvements
            for metric, delta in trace.improvement_delta.items():
                if metric not in self.stats["total_improvement"]:
                    self.stats["total_improvement"][metric] = 0
                    self.stats["best_improvement"][metric] = delta
                    self.stats["worst_decline"][metric] = delta

                self.stats["total_improvement"][metric] += delta

                if delta > self.stats["best_improvement"][metric]:
                    self.stats["best_improvement"][metric] = delta
                if delta < self.stats["worst_decline"][metric]:
                    self.stats["worst_decline"][metric] = delta

    def flush(self):
        """Write buffered traces to file"""
        if not self.enabled or not self.buffer:
            return

        try:
            if self.format == "jsonl":
                # Append each trace to the JSONL file
                for trace in self.buffer:
                    append_trace_jsonl(trace, self.output_path, compress=self.compress)
            elif self.format == "json":
                # For JSON, we'll write everything at close
                pass  # Traces already added to json_traces
            elif self.format == "hdf5":
                # For HDF5, we need to write everything at once
                # So we'll keep accumulating until close
                pass

            # Clear buffer after writing (except for formats that need full data)
            if self.format == "jsonl":
                self.buffer.clear()

        except Exception as e:
            logger.error(f"Error flushing traces to file: {e}")

    def get_statistics(self) -> Dict[str, Any]:
        """Get current tracing statistics"""
        return {
            **self.stats,
            "improvement_rate": (
                self.stats["improvement_count"] / self.stats["total_traces"]
                if self.stats["total_traces"] > 0
                else 0
            ),
        }

    def close(self):
        """Close the tracer and flush remaining data"""
        if not self.enabled:
            return

        # Flush any remaining traces
        self.flush()

        # For JSON and HDF5, write everything at close
        if self.format == "json" and hasattr(self, "json_traces"):
            metadata = {
                "created_at": time.time(),
                "include_code": self.include_code,
                "include_prompts": self.include_prompts,
                "include_changes_description": getattr(self, "include_changes_description", True),
                "total_traces": len(self.json_traces),
            }
            export_traces_json(self.json_traces, self.output_path, metadata=metadata)
        elif self.format == "hdf5":
            # Export all buffered traces
            all_traces = getattr(self, "json_traces", self.buffer)
            if all_traces:
                metadata = {
                    "created_at": time.time(),
                    "include_code": self.include_code,
                    "include_prompts": self.include_prompts,
                    "include_changes_description": getattr(self, "include_changes_description", True),
                }
                export_traces(all_traces, self.output_path, format="hdf5", metadata=metadata)

        # Log final statistics
        stats = self.get_statistics()
        logger.info(f"Evolution tracing complete. Total traces: {stats['total_traces']}")
        logger.info(f"Improvement rate: {stats['improvement_rate']:.2%}")

        if stats["best_improvement"]:
            logger.info(f"Best improvements: {stats['best_improvement']}")
        if stats["worst_decline"]:
            logger.info(f"Worst declines: {stats['worst_decline']}")

    def __enter__(self):
        """Context manager entry"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.close()


def extract_evolution_trace_from_checkpoint(
    checkpoint_dir: Union[str, Path],
    output_path: Optional[str] = None,
    format: str = "jsonl",
    include_code: bool = True,
    include_prompts: bool = True,
    include_changes_description: bool = True,
) -> List[EvolutionTrace]:
    """
    Extract evolution traces from an existing checkpoint directory

    Args:
        checkpoint_dir: Path to checkpoint directory
        output_path: Optional path to save extracted traces
        format: Output format ('jsonl', 'json')
        include_code: Whether to include code in traces
        include_prompts: Whether to include prompts in traces

    Returns:
        List of EvolutionTrace objects
    """
    checkpoint_path = Path(checkpoint_dir)
    if not checkpoint_path.exists():
        raise FileNotFoundError(f"Checkpoint directory {checkpoint_dir} not found")

    programs_dir = checkpoint_path / "programs"
    if not programs_dir.exists():
        raise FileNotFoundError(f"Programs directory not found in {checkpoint_dir}")

    logger.info(f"Extracting evolution traces from {checkpoint_dir}")

    # Load all programs
    programs = {}
    program_files = list(programs_dir.glob("*.json"))

    for prog_file in program_files:
        try:
            with open(prog_file, "r") as f:
                prog_data = json.load(f)
                programs[prog_data["id"]] = prog_data
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Error loading program from {prog_file}: {e}")
            continue

    logger.info(f"Loaded {len(programs)} programs from checkpoint")

    # Build parent-child traces
    traces = []
    for prog_id, prog in programs.items():
        # Skip programs without parents
        parent_id = prog.get("parent_id")
        if not parent_id or parent_id not in programs:
            continue

        parent = programs[parent_id]

        # Create trace entry
        parent_desc = parent.get("changes_description")
        if not parent_desc:
            parent_desc = (parent.get("metadata", {}) or {}).get("changes_description") or (
                parent.get("metadata", {}) or {}
            ).get("changes")

        child_desc = prog.get("changes_description")
        if not child_desc:
            child_desc = (prog.get("metadata", {}) or {}).get("changes_description") or (
                prog.get("metadata", {}) or {}
            ).get("changes")

        trace = EvolutionTrace(
            iteration=prog.get("iteration_found", 0),
            timestamp=prog.get("timestamp", 0),
            parent_id=parent_id,
            child_id=prog_id,
            parent_metrics=parent.get("metrics", {}),
            child_metrics=prog.get("metrics", {}),
            generation=prog.get("generation", 0),
            island_id=prog.get("metadata", {}).get("island"),
            parent_changes_description=parent_desc if include_changes_description else None,
            child_changes_description=child_desc if include_changes_description else None,
            metadata=prog.get("metadata", {}),
        )

        # Add code if requested
        if include_code:
            trace.parent_code = parent.get("code", "")
            trace.child_code = prog.get("code", "")

        # Add prompts if available and requested
        if include_prompts:
            # Check for prompt data in the program
            if "prompts" in prog:
                # Prompts might be stored in the program data
                trace.prompt = prog["prompts"].get("prompt")
                trace.llm_response = prog["prompts"].get("response")

        # Calculate improvement
        trace.improvement_delta = trace.calculate_improvement()

        traces.append(trace)

    # Sort traces by iteration
    traces.sort(key=lambda x: (x.iteration, x.timestamp))

    logger.info(f"Extracted {len(traces)} evolution traces")

    # Save to file if output path provided
    if output_path:
        metadata = {
            "total_traces": len(traces),
            "extracted_at": time.time(),
            "source": "checkpoint",
        }
        export_traces(traces, output_path, format=format, metadata=metadata)
        logger.info(f"Saved {len(traces)} traces to {output_path}")

    return traces


def extract_full_lineage_traces(
    checkpoint_dir: Union[str, Path], output_path: Optional[str] = None, format: str = "json"
) -> List[Dict[str, Any]]:
    """
    Extract complete evolution traces with full lineage chains and prompts/actions

    This function builds the complete evolution history for each program,
    tracing back through all ancestors to create full lineage chains.

    Args:
        checkpoint_dir: Path to checkpoint directory
        output_path: Optional path to save extracted traces
        format: Output format ('json' or 'jsonl')

    Returns:
        List of lineage trace dictionaries
    """
    checkpoint_path = Path(checkpoint_dir)
    if not checkpoint_path.exists():
        raise FileNotFoundError(f"Checkpoint directory {checkpoint_dir} not found")

    programs_dir = checkpoint_path / "programs"
    if not programs_dir.exists():
        raise FileNotFoundError(f"Programs directory not found in {checkpoint_dir}")

    logger.info(f"Extracting full lineage traces from {checkpoint_dir}")

    # Load all programs
    programs = {}
    program_files = list(programs_dir.glob("*.json"))

    for prog_file in program_files:
        try:
            with open(prog_file, "r") as f:
                prog_data = json.load(f)
                programs[prog_data["id"]] = prog_data
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Error loading program from {prog_file}: {e}")
            continue

    logger.info(f"Loaded {len(programs)} programs from checkpoint")

    # Build lineage traces for each program
    traces = []

    for program_id, program in programs.items():
        # Build lineage chain by tracing back through parents
        lineage = []
        current = program

        while current:
            lineage.append(current)
            parent_id = current.get("parent_id")
            current = programs.get(parent_id) if parent_id else None

        # Reverse to get chronological order (oldest to newest)
        lineage.reverse()

        # Extract improvement steps with actions
        improvements = []
        for i in range(len(lineage) - 1):
            parent = lineage[i]
            child = lineage[i + 1]

            # Extract the action (prompt and response) from child's data
            prompts = child.get("prompts", {})
            action = None

            # Get the prompt used (could be diff_user, full_rewrite_user, or other templates)
            for template_key, prompt_data in prompts.items():
                action = {
                    "template": template_key,
                    "system_prompt": prompt_data.get("system", ""),
                    "user_prompt": prompt_data.get("user", ""),
                    "llm_response": (
                        prompt_data.get("responses", [""])[0]
                        if prompt_data.get("responses")
                        else ""
                    ),
                }
                break  # Take the first prompt found

            # Calculate improvements for each metric
            improvement_deltas = {}
            if child.get("metrics") and parent.get("metrics"):
                for metric in child["metrics"].keys():
                    if metric in parent["metrics"]:
                        parent_val = parent["metrics"][metric]
                        child_val = child["metrics"][metric]
                        if isinstance(parent_val, (int, float)) and isinstance(
                            child_val, (int, float)
                        ):
                            improvement_deltas[metric] = child_val - parent_val

            improvement = {
                "step": i,
                "parent_id": parent["id"],
                "child_id": child["id"],
                "parent_metrics": parent.get("metrics", {}),
                "child_metrics": child.get("metrics", {}),
                "improvement": improvement_deltas,
                "generation": child.get("generation", 0),
                "iteration_found": child.get("iteration_found", 0),
                "changes_description": (
                    child.get("changes_description")
                    or (child.get("metadata", {}) or {}).get("changes_description")
                    or (child.get("metadata", {}) or {}).get("changes")
                    or ""
                ),
                "island_id": child.get("metadata", {}).get("island"),
                "action": action,  # The prompt/response that led to this improvement
            }
            improvements.append(improvement)

        # Only add traces that have improvement steps
        if improvements:
            trace = {
                "final_program_id": program_id,
                "final_metrics": program.get("metrics", {}),
                "final_changes_description": (
                    program.get("changes_description")
                    or (program.get("metadata", {}) or {}).get("changes_description")
                    or (program.get("metadata", {}) or {}).get("changes")
                    or ""
                ),
                "generation_depth": len(lineage),
                "total_iterations": program.get("iteration_found", 0),
                "improvement_steps": improvements,
                "metadata": {
                    "language": program.get("language", ""),
                    "timestamp": program.get("timestamp", 0),
                },
            }
            traces.append(trace)

    # Sort traces by generation depth (most evolved first)
    traces.sort(key=lambda x: x["generation_depth"], reverse=True)

    logger.info(f"Extracted {len(traces)} lineage traces")

    # Save to file if output path provided
    if output_path:
        if format == "json":
            metadata = {
                "total_traces": len(traces),
                "extracted_at": time.time(),
                "source": "checkpoint",
                "type": "full_lineage",
            }
            export_traces_json(traces, output_path, metadata=metadata)
        elif format == "jsonl":
            # For JSONL, write each trace as a separate line
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w") as f:
                for trace in traces:
                    json.dump(trace, f)
                    f.write("\n")
        else:
            raise ValueError(f"Unsupported format: {format}. Use 'json' or 'jsonl'")

        logger.info(f"Saved {len(traces)} lineage traces to {output_path}")

    return traces
