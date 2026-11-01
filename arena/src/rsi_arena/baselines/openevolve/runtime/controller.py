"""
Main controller for OpenEvolve
"""

import asyncio
import logging
import os
import shutil
import signal
import time
import uuid
from pathlib import Path
from typing import Optional

from rsi_arena.baselines.openevolve.runtime.config import Config
from rsi_arena.baselines.openevolve.runtime.database import Program, ProgramDatabase
from rsi_arena.baselines.openevolve.runtime.evolution_trace import EvolutionTracer
from rsi_arena.baselines.openevolve.runtime.llm.ensemble import LLMEnsemble
from rsi_arena.baselines.openevolve.runtime.process_parallel import (
    ProcessParallelController,
    persist_native_database,
    prepare_native_admission,
    recover_evaluator_pending,
)
from rsi_arena.baselines.openevolve.runtime.utils.code_utils import extract_code_language
from rsi_arena.baselines.openevolve.runtime.utils.format_utils import format_improvement_safe, format_metrics_safe

logger = logging.getLogger(__name__)


class OpenEvolve:
    """
    Main controller for OpenEvolve

    Orchestrates the evolution process, coordinating between the prompt sampler,
    LLM ensemble, evaluator, and program database.

    Features:
    - Tracks the absolute best program across evolution steps
    - Ensures the best solution is not lost during the MAP-Elites process
    - Always includes the best program in the selection process for inspiration
    - Maintains detailed logs and metadata about improvements
    """

    def __init__(
        self,
        initial_program_path: str,
        evaluator: object,
        config: Config,
        output_dir: Optional[str] = None,
    ):
        # Load configuration (loaded in main_async)
        self.config = config
        self.evaluator = evaluator

        # Set up output directory
        self.output_dir = output_dir or os.path.join(
            os.path.dirname(initial_program_path), "openevolve_output"
        )
        os.makedirs(self.output_dir, exist_ok=True)

        # Set up logging
        self._setup_logging()

        # Manual mode queue lives in <openevolve_output>/manual_tasks_queue
        self._setup_manual_mode_queue()

        # Set random seed for reproducibility if specified
        if self.config.random_seed is not None:
            import hashlib
            import random

            import numpy as np

            # Set global random seeds
            random.seed(self.config.random_seed)
            np.random.seed(self.config.random_seed)

            # Create hash-based seeds for different components. md5 is used only to
            # derive a deterministic RNG seed from the configured seed, not for any
            # security purpose; usedforsecurity=False documents that intent.
            base_seed = str(self.config.random_seed).encode("utf-8")
            llm_seed = int(
                hashlib.md5(base_seed + b"llm", usedforsecurity=False).hexdigest()[:8], 16
            ) % (2**31)

            # Propagate seed to LLM configurations
            self.config.llm.random_seed = llm_seed
            for model_cfg in self.config.llm.models:
                if not hasattr(model_cfg, "random_seed") or model_cfg.random_seed is None:
                    model_cfg.random_seed = llm_seed
            for model_cfg in self.config.llm.evaluator_models:
                if not hasattr(model_cfg, "random_seed") or model_cfg.random_seed is None:
                    model_cfg.random_seed = llm_seed

            logger.info(f"Set random seed to {self.config.random_seed} for reproducibility")
            logger.debug(f"Generated LLM seed: {llm_seed}")

        # Load initial program
        self.initial_program_path = initial_program_path
        self.initial_program_code = self._load_initial_program()
        if not self.config.language:
            self.config.language = extract_code_language(self.initial_program_code)

        # Extract file extension from initial program
        self.file_extension = os.path.splitext(initial_program_path)[1]
        if not self.file_extension:
            # Default to .py if no extension found
            self.file_extension = ".py"
        else:
            # Make sure it starts with a dot
            if not self.file_extension.startswith("."):
                self.file_extension = f".{self.file_extension}"

        # Set the file_suffix in config (can be overridden in YAML)
        if not hasattr(self.config, "file_suffix") or self.config.file_suffix == ".py":
            self.config.file_suffix = self.file_extension

        # Initialize components
        self.llm_ensemble = LLMEnsemble(self.config.llm.models)

        # Pass random seed to database if specified
        if self.config.random_seed is not None:
            self.config.database.random_seed = self.config.random_seed

        self.config.database.novelty_llm = self.llm_ensemble
        self.database = ProgramDatabase(self.config.database)

        logger.info(f"Initialized OpenEvolve with {initial_program_path}")

        # Initialize evolution tracer
        if self.config.evolution_trace.enabled:
            trace_output_path = self.config.evolution_trace.output_path
            if not trace_output_path:
                # Default to output_dir/evolution_trace.{format}
                trace_output_path = os.path.join(
                    self.output_dir, f"evolution_trace.{self.config.evolution_trace.format}"
                )

            self.evolution_tracer = EvolutionTracer(
                output_path=trace_output_path,
                format=self.config.evolution_trace.format,
                include_code=self.config.evolution_trace.include_code,
                include_prompts=self.config.evolution_trace.include_prompts,
                enabled=True,
                buffer_size=self.config.evolution_trace.buffer_size,
                compress=self.config.evolution_trace.compress,
            )
            logger.info(f"Evolution tracing enabled: {trace_output_path}")
        else:
            self.evolution_tracer = None

        # Initialize improved parallel processing components
        self.parallel_controller = None

    def _setup_logging(self) -> None:
        """Set up logging"""
        log_dir = self.config.log_dir or os.path.join(self.output_dir, "logs")
        os.makedirs(log_dir, exist_ok=True)

        # Set up root logger
        root_logger = logging.getLogger()
        root_logger.setLevel(getattr(logging, self.config.log_level))

        # Add file handler
        log_file = os.path.join(log_dir, f"openevolve_{time.strftime('%Y%m%d_%H%M%S')}.log")
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )
        root_logger.addHandler(file_handler)

        # Add console handler
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        root_logger.addHandler(console_handler)

        logger.info(f"Logging to {log_file}")

    def _setup_manual_mode_queue(self) -> None:
        """
        Set up manual task queue directory if llm.manual_mode is enabled

        Queue directory is always:
          <openevolve_output>/manual_tasks_queue

        The directory is cleared on controller start so the UI shows only tasks
        from the current run (no stale tasks after restart)
        """
        if not bool(getattr(self.config.llm, "manual_mode", False)):
            return

        qdir = Path(self.output_dir).expanduser().resolve() / "manual_tasks_queue"

        # Clear stale tasks from previous runs
        if qdir.exists():
            shutil.rmtree(qdir)
        qdir.mkdir(parents=True, exist_ok=True)

        # Inject runtime-only queue dir into configs
        self.config.llm._manual_queue_dir = str(qdir)
        for model_cfg in self.config.llm.models:
            model_cfg._manual_queue_dir = str(qdir)
        for model_cfg in self.config.llm.evaluator_models:
            model_cfg._manual_queue_dir = str(qdir)

        logger.info(f"Manual mode enabled. Queue dir: {qdir}")

    def _load_initial_program(self) -> str:
        """Load the initial program from file"""
        with open(self.initial_program_path, "r") as f:
            return f.read()

    async def run(
        self,
        iterations: Optional[int] = None,
        target_score: Optional[float] = None,
        checkpoint_path: Optional[str] = None,
    ) -> Optional[Program]:
        """
        Run the evolution process with improved parallel processing

        Args:
            iterations: Maximum number of iterations (uses config if None)
            target_score: Target score to reach (continues until reached if specified)
            checkpoint_path: Path to resume from checkpoint

        Returns:
            Best program found
        """
        max_iterations = iterations or self.config.max_iterations
        # Determine starting iteration
        start_iteration = 0
        if checkpoint_path and os.path.exists(checkpoint_path):
            self._load_checkpoint(checkpoint_path)
            start_iteration = self.database.last_iteration + 1
            logger.info(f"Resuming from checkpoint at iteration {start_iteration}")
        else:
            start_iteration = self.database.last_iteration

        recovery = await recover_evaluator_pending(
            self.database, self.evaluator
        )
        if recovery.initial_outcome == "inconclusive" and not self.database.programs:
            logger.warning("Recovered initial evaluation was inconclusive")
            return None
        if recovery.max_iteration is not None:
            start_iteration = max(start_iteration, recovery.max_iteration + 1)

        # Only add initial program if starting fresh (not resuming from checkpoint)
        should_add_initial = (
            start_iteration == 0
            and len(self.database.programs) == 0
            and not any(
                p.code == self.initial_program_code for p in self.database.programs.values()
            )
        )

        if should_add_initial:
            logger.info("Adding initial program to database")
            initial_program_id = str(uuid.uuid4())

            initial_program = Program(
                id=initial_program_id,
                code=self.initial_program_code,
                changes_description=self.config.prompt.initial_changes_description,
                language=self.config.language,
                metrics={},
                iteration_found=start_iteration,
            )
            self.evaluator.bind_native_context(
                initial_program.to_dict(),
                iteration=start_iteration,
                target_island=None,
                initial=True,
            )
            initial_metrics = await self.evaluator.evaluate_program(
                initial_program.code, initial_program.id
            )
            if initial_metrics is None:
                self.evaluator.on_native_persisted(
                    initial_program.id,
                    admitted=False,
                    native_state_reference=None,
                )
                logger.warning("Initial program was inconclusive and was not admitted")
                return None
            initial_program.metrics = dict(initial_metrics)
            initial_island = prepare_native_admission(
                self.database,
                self.evaluator,
                initial_program,
                target_island=None,
            )
            self.database.add(initial_program, target_island=initial_island)
            initial_artifacts = self.evaluator.get_pending_artifacts(initial_program.id)
            if initial_artifacts:
                try:
                    self.database.store_artifacts(
                        initial_program.id, initial_artifacts
                    )
                except Exception as exc:
                    logger.warning(
                        "OpenEvolve initial evaluator artifacts were unavailable: %s",
                        exc,
                    )
            native_state_reference = persist_native_database(
                self.database,
                iteration=start_iteration,
                program_id=initial_program.id,
            )
            self.evaluator.on_native_persisted(
                initial_program.id,
                admitted=True,
                native_state_reference=native_state_reference,
            )
        else:
            logger.info(
                f"Skipping initial program addition (resuming from iteration {start_iteration} "
                f"with {len(self.database.programs)} existing programs)"
            )

        # Initialize improved parallel processing
        try:
            self.parallel_controller = ProcessParallelController(
                self.config,
                self.database,
                self.evaluator,
                self.evolution_tracer,
                file_suffix=self.config.file_suffix,
            )

            # Set up signal handlers for graceful shutdown
            def signal_handler(signum, frame):
                logger.info(f"Received signal {signum}, initiating graceful shutdown...")
                self.parallel_controller.request_shutdown()

                # Set up a secondary handler for immediate exit if user presses Ctrl+C again
                def force_exit_handler(signum, frame):
                    logger.info("Force exit requested - terminating immediately")
                    import sys

                    sys.exit(0)

                signal.signal(signal.SIGINT, force_exit_handler)

            signal.signal(signal.SIGINT, signal_handler)
            signal.signal(signal.SIGTERM, signal_handler)

            self.parallel_controller.start()

            # When starting from iteration 0, we've already done the initial program evaluation
            # So we need to adjust the start_iteration for the actual evolution
            evolution_start = start_iteration
            evolution_iterations = max_iterations

            # If we just added the initial program at iteration 0, start evolution from iteration 1
            if should_add_initial and start_iteration == 0:
                evolution_start = 1
                # User expects max_iterations evolutionary iterations AFTER the initial program
                # So we don't need to reduce evolution_iterations

            # Run evolution with improved parallel processing and checkpoint callback
            await self._run_evolution_with_checkpoints(
                evolution_start, evolution_iterations, target_score
            )

        finally:
            # Clean up parallel processing resources
            if self.parallel_controller:
                self.parallel_controller.stop()
                self.parallel_controller = None

            # Close evolution tracer
            if self.evolution_tracer:
                self.evolution_tracer.close()
                logger.info("Evolution tracer closed")

        # Get the best program
        best_program = None
        if self.database.best_program_id:
            best_program = self.database.get(self.database.best_program_id)
            logger.info(f"Using tracked best program: {self.database.best_program_id}")

        if best_program is None:
            best_program = self.database.get_best_program()
            logger.info("Using calculated best program (tracked program not found)")

        if best_program:
            if (
                hasattr(self, "parallel_controller")
                and self.parallel_controller
                and self.parallel_controller.early_stopping_triggered
            ):
                logger.info(
                    f"🛑 Evolution complete via early stopping. Best program has metrics: "
                    f"{format_metrics_safe(best_program.metrics)}"
                )
            else:
                logger.info(
                    f"Evolution complete. Best program has metrics: "
                    f"{format_metrics_safe(best_program.metrics)}"
                )
            self._save_best_program(best_program)
            return best_program
        else:
            logger.warning("No valid programs found during evolution")
            return None

    def _log_iteration(
        self,
        iteration: int,
        parent: Program,
        child: Program,
        elapsed_time: float,
    ) -> None:
        """
        Log iteration progress

        Args:
            iteration: Iteration number
            parent: Parent program
            child: Child program
            elapsed_time: Elapsed time in seconds
        """
        # Calculate improvement using safe formatting
        improvement_str = format_improvement_safe(parent.metrics, child.metrics)

        logger.info(
            f"Iteration {iteration+1}: Child {child.id} from parent {parent.id} "
            f"in {elapsed_time:.2f}s. Metrics: "
            f"{format_metrics_safe(child.metrics)} "
            f"(Δ: {improvement_str})"
        )

    def _save_checkpoint(self, iteration: int) -> None:
        """
        Save a checkpoint

        Args:
            iteration: Current iteration number
        """
        checkpoint_dir = os.path.join(self.output_dir, "checkpoints")
        os.makedirs(checkpoint_dir, exist_ok=True)

        # Create specific checkpoint directory
        checkpoint_path = os.path.join(checkpoint_dir, f"checkpoint_{iteration}")
        os.makedirs(checkpoint_path, exist_ok=True)

        # Save the database
        self.database.save(checkpoint_path, iteration)

        # Save the best program found so far
        best_program = None
        if self.database.best_program_id:
            best_program = self.database.get(self.database.best_program_id)
        else:
            best_program = self.database.get_best_program()

        if best_program:
            # Save the best program at this checkpoint
            best_program_path = os.path.join(checkpoint_path, f"best_program{self.file_extension}")
            with open(best_program_path, "w") as f:
                f.write(best_program.code)

            # Save metrics
            best_program_info_path = os.path.join(checkpoint_path, "best_program_info.json")
            with open(best_program_info_path, "w") as f:
                import json

                json.dump(
                    {
                        "id": best_program.id,
                        "generation": best_program.generation,
                        "iteration": best_program.iteration_found,
                        "current_iteration": iteration,
                        "metrics": best_program.metrics,
                        "language": best_program.language,
                        "timestamp": best_program.timestamp,
                        "saved_at": time.time(),
                    },
                    f,
                    indent=2,
                )

            logger.info(
                f"Saved best program at checkpoint {iteration} with metrics: "
                f"{format_metrics_safe(best_program.metrics)}"
            )

        logger.info(f"Saved checkpoint at iteration {iteration} to {checkpoint_path}")

    def _load_checkpoint(self, checkpoint_path: str) -> None:
        """Load state from a checkpoint directory"""
        if not os.path.exists(checkpoint_path):
            raise FileNotFoundError(f"Checkpoint directory {checkpoint_path} not found")

        logger.info(f"Loading checkpoint from {checkpoint_path}")
        self.database.load(checkpoint_path)
        logger.info(f"Checkpoint loaded successfully (iteration {self.database.last_iteration})")

    async def _run_evolution_with_checkpoints(
        self, start_iteration: int, max_iterations: int, target_score: Optional[float]
    ) -> None:
        """Run evolution with checkpoint saving support"""
        logger.info(f"Using island-based evolution with {self.config.database.num_islands} islands")
        self.database.log_island_status()

        # Run the evolution process with checkpoint callback
        await self.parallel_controller.run_evolution(
            start_iteration, max_iterations, target_score, checkpoint_callback=self._save_checkpoint
        )

        # Check if shutdown or early stopping was triggered
        if self.parallel_controller.shutdown_event.is_set():
            logger.info("Evolution stopped due to shutdown request")
            return
        elif self.parallel_controller.early_stopping_triggered:
            logger.info("Evolution stopped due to early stopping - saving final checkpoint")
            # Continue to save final checkpoint for early stopping

        # Save final checkpoint if needed
        # Note: start_iteration here is the evolution start (1 for fresh start, not 0)
        # max_iterations is the number of evolution iterations to run
        final_iteration = start_iteration + max_iterations - 1
        if final_iteration > 0 and final_iteration % self.config.checkpoint_interval == 0:
            self._save_checkpoint(final_iteration)

    def _save_best_program(self, program: Optional[Program] = None) -> None:
        """
        Save the best program

        Args:
            program: Best program (if None, uses the tracked best program)
        """
        # If no program is provided, use the tracked best program from the database
        if program is None:
            if self.database.best_program_id:
                program = self.database.get(self.database.best_program_id)
            else:
                # Fallback to calculating best program if no tracked best program
                program = self.database.get_best_program()

        if not program:
            logger.warning("No best program found to save")
            return

        best_dir = os.path.join(self.output_dir, "best")
        os.makedirs(best_dir, exist_ok=True)

        # Use the extension from the initial program file
        filename = f"best_program{self.file_extension}"
        code_path = os.path.join(best_dir, filename)

        with open(code_path, "w") as f:
            f.write(program.code)

        # Save complete program info including metrics
        info_path = os.path.join(best_dir, "best_program_info.json")
        with open(info_path, "w") as f:
            import json

            json.dump(
                {
                    "id": program.id,
                    "generation": program.generation,
                    "iteration": program.iteration_found,
                    "timestamp": program.timestamp,
                    "parent_id": program.parent_id,
                    "metrics": program.metrics,
                    "language": program.language,
                    "saved_at": time.time(),
                },
                f,
                indent=2,
            )

        logger.info(f"Saved best program to {code_path} with program info to {info_path}")
