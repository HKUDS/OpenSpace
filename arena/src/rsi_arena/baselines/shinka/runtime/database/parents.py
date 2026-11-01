import json
import logging
import sqlite3
from abc import ABC, abstractmethod
from typing import Optional, Callable, Any, Tuple
import numpy as np  # type: ignore

logger = logging.getLogger(__name__)


def sample_with_powerlaw(items: list, alpha: float = 1.0) -> int:
    """
    Sample an index from a list of items using a power law distribution
    based on their rank (order in the list).

    Parameters
    ----------
    items : list
        List of items to sample from (order implies rank, e.g., best first).
    alpha : float, default=1.0
        Power law exponent.
        - alpha = 0: uniform sampling
        - alpha > 0: items earlier in the list (higher rank) are sampled more.
        - alpha < 0: items later in the list (lower rank) are sampled more.

    Returns
    -------
    int
        Index of the sampled item from the input list.
    """
    if not items:
        raise ValueError("Empty items list for power-law sampling")

    # Probabilities based on rank (index + 1)
    probs = np.array([(i + 1) ** (-alpha) for i in range(len(items))])
    if np.sum(probs) == 0:  # Avoid div by zero if all probs are zero
        # Fallback to uniform if power law results in all zero probabilities
        probs = np.ones(len(items))

    probs = probs / probs.sum()  # Normalize
    logger.info(f"Power law probs: {probs.tolist()}")
    return np.random.choice(len(items), p=probs)


def stable_sigmoid(x: float) -> float:
    """
    Numerically stable sigmoid function that avoids overflow.

    Standard sigmoid: sigmoid(x) = 1 / (1 + exp(-x))
    This can overflow when x is very large (positive or negative).

    For x >= 0: sigmoid(x) = 1 / (1 + exp(-x))
    For x < 0: sigmoid(x) = exp(x) / (1 + exp(x))

    Args:
        x: Input value

    Returns:
        Sigmoid value between 0 and 1
    """
    if x >= 0:
        exp_neg_x = np.exp(-x)
        return 1.0 / (1.0 + exp_neg_x)
    else:
        exp_x = np.exp(x)
        return exp_x / (1.0 + exp_x)


class ParentSamplingStrategy(ABC):
    """Abstract base class for parent sampling strategies."""

    def __init__(
        self,
        cursor: sqlite3.Cursor,
        conn: sqlite3.Connection,
        config: Any,
        get_program_func: Callable[[str], Any],
        best_program_id: Optional[str] = None,
        island_idx: Optional[int] = None,
    ):
        self.cursor = cursor
        self.conn = conn
        self.config = config
        self.get_program = get_program_func
        self.best_program_id = best_program_id
        self.island_idx = island_idx

    @abstractmethod
    def sample_parent(self) -> Any:
        """Sample and return a parent program."""
        pass

    def _get_island_idx(self, program_id: str) -> Optional[int]:
        """Get the island index for a given program ID."""
        self.cursor.execute(
            "SELECT island_idx FROM programs WHERE id = ?", (program_id,)
        )
        row = self.cursor.fetchone()
        return row["island_idx"] if row else None


class PowerLawSamplingStrategy(ParentSamplingStrategy):
    """Power law sampling strategy for parent selection."""

    def sample_parent(self) -> Any:
        if not hasattr(self.config, "exploitation_ratio"):
            raise ConnectionError("DB/config issue for parent sampling.")

        # Try to sample from archive first, with fallbacks if empty
        if self.island_idx is not None:
            self.cursor.execute(
                """SELECT a.program_id FROM archive a
                   JOIN programs p ON a.program_id = p.id
                   WHERE p.island_idx = ?""",
                (self.island_idx,),
            )
        else:
            self.cursor.execute("SELECT program_id FROM archive")

        archived_rows = self.cursor.fetchall()

        # Try to sample from archive first
        if archived_rows:
            archived_program_ids = [row["program_id"] for row in archived_rows]

            # Fetch Program objects
            archived_programs = []
            for prog_id in archived_program_ids:
                prog = self.get_program(prog_id)
                if prog:
                    archived_programs.append(prog)

            if archived_programs:
                # Sort by combined_score descending (best first)
                archived_programs.sort(
                    key=lambda p: p.combined_score or 0.0, reverse=True
                )
                logger.info(
                    f"Island {self.island_idx} => Archived program "
                    f"scores: {[p.combined_score for p in archived_programs]}"
                )

                alpha = getattr(self.config, "exploitation_alpha", 1.0)
                sampled_idx = sample_with_powerlaw(archived_programs, alpha)
                selected_prog = archived_programs[sampled_idx]

                logger.info(
                    f"Power law: Sampled from archive: {selected_prog.id} "
                    f"(Gen: {selected_prog.generation}, "
                    f"Score: {selected_prog.combined_score or 0.0:.4f}, "
                    f"Island: {selected_prog.island_idx})"
                )
                return selected_prog

        # Fallback: Sample from all correct programs with power law
        logger.warning(
            f"No archived programs available (island {self.island_idx}). "
            f"Falling back to all correct programs."
        )

        if self.island_idx is not None:
            self.cursor.execute(
                """SELECT p.id FROM programs p
                   WHERE p.correct = 1 AND p.island_idx = ?
                   ORDER BY p.combined_score DESC""",
                (self.island_idx,),
            )
        else:
            self.cursor.execute(
                """SELECT p.id FROM programs p
                   WHERE p.correct = 1
                   ORDER BY p.combined_score DESC"""
            )

        correct_rows = self.cursor.fetchall()
        if correct_rows:
            correct_program_ids = [row["id"] for row in correct_rows]
            correct_programs = []
            for prog_id in correct_program_ids:
                prog = self.get_program(prog_id)
                if prog:
                    correct_programs.append(prog)

            if correct_programs:
                alpha = getattr(self.config, "exploitation_alpha", 1.0)
                sampled_idx = sample_with_powerlaw(correct_programs, alpha)
                selected_prog = correct_programs[sampled_idx]

                logger.info(
                    f"Power law fallback: Sampled from all correct: "
                    f"{selected_prog.id} (Gen: {selected_prog.generation}, "
                    f"Score: {selected_prog.combined_score or 0.0:.4f}, "
                    f"Island: {selected_prog.island_idx})"
                )
                return selected_prog

        # Final fallback: best program or random correct
        if self.best_program_id:
            best_prog = self.get_program(self.best_program_id)
            if (
                best_prog
                and best_prog.correct
                and (self.island_idx is None or best_prog.island_idx == self.island_idx)
            ):
                logger.info(f"Power law: Using best program {self.best_program_id}")
                return best_prog

        # Last resort: any correct program
        if self.island_idx is not None:
            self.cursor.execute(
                """SELECT id FROM programs
                   WHERE correct = 1 AND island_idx = ?
                   ORDER BY RANDOM() LIMIT 1""",
                (self.island_idx,),
            )
        else:
            self.cursor.execute(
                """SELECT id FROM programs WHERE correct = 1
                   ORDER BY RANDOM() LIMIT 1"""
            )
        row = self.cursor.fetchone()
        if row:
            prog = self.get_program(row["id"])
            if prog:
                logger.info(
                    f"Power law: Final fallback to random correct program: {prog.id}"
                )
                return prog

        logger.warning(
            "No parent found, database may be empty or no correct "
            "programs in specified island."
        )
        return None


class WeightedSamplingStrategy(ParentSamplingStrategy):
    """Weighted sampling strategy for parent selection."""

    def sample_parent(self) -> Any:
        # Fetch all programs from the archive.
        if self.island_idx is not None:
            self.cursor.execute(
                """
                SELECT p.*
                FROM programs p
                JOIN archive a ON p.id = a.program_id
                WHERE p.correct = 1 AND p.island_idx = ?
                """,
                (self.island_idx,),
            )
        else:
            self.cursor.execute(
                """
                SELECT p.*
                FROM programs p
                JOIN archive a ON p.id = a.program_id
                WHERE p.correct = 1
                """
            )
        archive_rows = self.cursor.fetchall()

        if not archive_rows:
            logger.warning(
                f"No archived programs found for weighted sampling "
                f"(island {self.island_idx}). "
                f"Falling back to best/random correct program."
            )
            # Fallback to best program
            if self.best_program_id:
                best_prog = self.get_program(self.best_program_id)
                if best_prog and (
                    self.island_idx is None or best_prog.island_idx == self.island_idx
                ):
                    logger.info(f"Weighted: Using best program {self.best_program_id}")
                    return best_prog

            # Fallback to random correct program
            if self.island_idx is not None:
                self.cursor.execute(
                    """SELECT id FROM programs
                       WHERE correct = 1 AND island_idx = ?
                       ORDER BY RANDOM() LIMIT 1""",
                    (self.island_idx,),
                )
            else:
                self.cursor.execute(
                    """SELECT id FROM programs WHERE correct = 1
                       ORDER BY RANDOM() LIMIT 1"""
                )
            row = self.cursor.fetchone()
            if row:
                prog = self.get_program(row["id"])
                if prog:
                    logger.info(
                        f"Weighted: Fallback to random correct program: {prog.id}"
                    )
                    return prog

            logger.warning("No suitable parent found for weighted sampling")
            return None

        eligible_programs = []
        for row in archive_rows:
            p_dict = dict(row)

            # Parse JSON fields
            p_dict["public_metrics"] = (
                json.loads(p_dict["public_metrics"])
                if p_dict.get("public_metrics")
                else {}
            )
            p_dict["private_metrics"] = (
                json.loads(p_dict["private_metrics"])
                if p_dict.get("private_metrics")
                else {}
            )
            p_dict["metadata"] = (
                json.loads(p_dict["metadata"]) if p_dict.get("metadata") else {}
            )
            p_dict["archive_inspiration_ids"] = (
                json.loads(p_dict["archive_inspiration_ids"])
                if p_dict.get("archive_inspiration_ids")
                else []
            )
            p_dict["top_k_inspiration_ids"] = (
                json.loads(p_dict["top_k_inspiration_ids"])
                if p_dict.get("top_k_inspiration_ids")
                else []
            )
            p_dict["embedding"] = (
                json.loads(p_dict["embedding"]) if p_dict.get("embedding") else []
            )
            p_dict["embedding_pca_2d"] = (
                json.loads(p_dict["embedding_pca_2d"])
                if p_dict.get("embedding_pca_2d")
                else []
            )
            p_dict["embedding_pca_3d"] = (
                json.loads(p_dict["embedding_pca_3d"])
                if p_dict.get("embedding_pca_3d")
                else []
            )
            p_dict["migration_history"] = (
                json.loads(p_dict["migration_history"])
                if p_dict.get("migration_history")
                else []
            )

            # Create a simple dataclass-like object from the dict to avoid circular imports
            class SimpleProgram:
                def __init__(self, data):
                    for key, value in data.items():
                        setattr(self, key, value)
                    # Ensure required attributes exist
                    if not hasattr(self, "combined_score"):
                        self.combined_score = 0.0
                    if not hasattr(self, "children_count"):
                        self.children_count = 0
                    if not hasattr(self, "correct"):
                        self.correct = False
                    if not hasattr(self, "id"):
                        self.id = None

            eligible_programs.append(SimpleProgram(p_dict))

        # Calculate baseline performance (alpha_0) as the median
        scores = [p.combined_score or 0.0 for p in eligible_programs]
        alpha_0 = np.median(scores) if scores else 0.0

        # Calculate scale-robust normalization factor
        # Use median absolute deviation (MAD) for robust scaling
        score_deviations = [abs(score - alpha_0) for score in scores]
        mad = np.median(score_deviations) if score_deviations else 1.0
        # Avoid division by zero - use a small epsilon if MAD is zero
        scale_factor = max(mad, 1e-6)

        # Calculate weights for each program
        weights = []
        lambda_ = self.config.parent_selection_lambda

        for i, p in enumerate(eligible_programs):
            # performance (alpha_i) from combined_score
            alpha_i = p.combined_score or 0.0
            # children count (n_i)
            n_i = p.children_count

            # sigmoid-scaled performance (s_i) - scale-robust and numerically stable
            # Normalize the difference by the scale factor to make it robust to problem scale
            normalized_diff = (alpha_i - alpha_0) / scale_factor
            s_i = stable_sigmoid(lambda_ * normalized_diff)

            # novelty bonus (h_i)
            h_i = 1 / (1 + n_i)

            # unnormalized weight (w_i)
            w_i = s_i * h_i
            weights.append(w_i)
            logger.debug(
                f"I-{self.island_idx} => P-{i}: w_i: {w_i:.2f}, s_i: {s_i:.2f}, h_i: {h_i:.2f}, alpha_i: {alpha_i:.2f}, alpha_0: {alpha_0:.2f}, "
                f"normalized_diff: {normalized_diff:.2f}, scale_factor: {scale_factor:.2f}"
            )

        # Normalize weights to get probabilities
        weights_sum = sum(weights)
        if weights_sum > 0:
            probabilities = [w / weights_sum for w in weights]
        else:
            # Fallback to uniform distribution if all weights are zero
            logger.warning(
                "All parent selection weights are zero, falling back to "
                "uniform sampling."
            )
            num_eligible = len(eligible_programs)
            probabilities = [1.0 / num_eligible] * num_eligible
        logger.info(
            f"Island {self.island_idx} => Probabilities: {np.array(probabilities).tolist()}"
        )
        logger.info(
            f"Island {self.island_idx} => Scores: {[p.combined_score for p in eligible_programs]}"
        )
        # Sample one parent based on probabilities
        selected_parent = np.random.choice(eligible_programs, p=probabilities)

        logger.info(
            f"Sampled parent {selected_parent.id} "
            f"(Gen: {selected_parent.generation}, "
            f"Score: {selected_parent.combined_score or 0.0:.4f}, "
            f"Children: {selected_parent.children_count}, "
            f"Island: {selected_parent.island_idx})"
        )

        return self.get_program(selected_parent.id)


class BeamSearchSamplingStrategy(ParentSamplingStrategy):
    """Beam search sampling strategy that locks onto a parent for multiple generations."""

    def __init__(
        self,
        cursor: sqlite3.Cursor,
        conn: sqlite3.Connection,
        config: Any,
        get_program_func: Callable[[str], Any],
        best_program_id: Optional[str] = None,
        island_idx: Optional[int] = None,
        beam_search_parent_id: Optional[str] = None,
        last_iteration: int = 0,
        update_metadata_func: Optional[Callable[[str, Optional[str]], None]] = None,
        get_best_program_func: Optional[Callable[[], Any]] = None,
    ):
        super().__init__(
            cursor, conn, config, get_program_func, best_program_id, island_idx
        )
        self.beam_search_parent_id = beam_search_parent_id
        self.last_iteration = last_iteration
        self.update_metadata = update_metadata_func
        self.get_best_program_func = get_best_program_func

    def _try_update_metadata(self, key: str, value: str) -> None:
        """Try to update metadata, gracefully handling read-only databases."""
        if self.update_metadata:
            try:
                self.update_metadata(key, value)
            except Exception as e:
                # In async mode, sampling happens in read-only thread-local connections
                # Just log debug and continue - the state is tracked in memory
                logger.debug(f"Could not persist {key} (read-only mode): {e}")

    def sample_parent(self) -> Any:
        num_beams = getattr(self.config, "num_beams", 5)

        # If no current beam search parent, select one
        if not self.beam_search_parent_id:
            # Get top programs and select one
            if self.get_best_program_func:
                best_program = self.get_best_program_func()
                if best_program:
                    self.beam_search_parent_id = best_program.id
                    self._try_update_metadata(
                        "beam_search_parent_id", self.beam_search_parent_id
                    )
                    logger.info(
                        f"Beam search: Selected new parent {self.beam_search_parent_id} "
                        f"(Gen: {best_program.generation}, "
                        f"Score: {best_program.combined_score or 0.0:.4f})"
                    )

        # Use the current beam search parent
        if self.beam_search_parent_id:
            parent = self.get_program(self.beam_search_parent_id)
            if parent:
                # Check if we should continue with this parent based on num_beams
                self.cursor.execute(
                    "SELECT COUNT(*) FROM programs WHERE parent_id = ?",
                    (self.beam_search_parent_id,),
                )
                children_count = (self.cursor.fetchone() or [0])[0]

                if children_count < num_beams:
                    logger.info(
                        f"Beam search: Continue with parent {self.beam_search_parent_id} "
                        f"({children_count}/{num_beams} children)"
                    )
                    return parent
                else:
                    # This parent has enough children, select a new one
                    if self.get_best_program_func:
                        best_program = self.get_best_program_func()
                        if best_program:
                            self.beam_search_parent_id = best_program.id
                            self._try_update_metadata(
                                "beam_search_parent_id", self.beam_search_parent_id
                            )
                            logger.info(
                                f"Beam search: Switch to new parent {self.beam_search_parent_id} "
                                f"(Gen: {best_program.generation}, "
                                f"Score: {best_program.combined_score or 0.0:.4f})"
                            )
                            return best_program

        # Fallback to best program
        if self.best_program_id:
            return self.get_program(self.best_program_id)

        # Final fallback
        self.cursor.execute(
            "SELECT id FROM programs WHERE correct = 1 ORDER BY RANDOM() LIMIT 1"
        )
        row = self.cursor.fetchone()
        return self.get_program(row["id"]) if row else None


class BestOfNSamplingStrategy(ParentSamplingStrategy):
    """Best-of-N sampling strategy that always returns the initial program as parent."""

    def sample_parent(self) -> Any:
        # Find the initial program (generation 0) in the specified island or globally
        if self.island_idx is not None:
            self.cursor.execute(
                """SELECT id FROM programs
                   WHERE generation = 0 AND island_idx = ? AND correct = 1
                   ORDER BY id LIMIT 1""",
                (self.island_idx,),
            )
        else:
            self.cursor.execute(
                """SELECT id FROM programs
                   WHERE generation = 0 AND correct = 1
                   ORDER BY id LIMIT 1"""
            )

        row = self.cursor.fetchone()
        if row:
            pid = row["id"]
            prog = self.get_program(pid)
            if prog:
                logger.info(
                    f"Best-of-N: Selected initial program {pid} "
                    f"(Gen: {prog.generation}, "
                    f"Score: {prog.combined_score or 0.0:.4f}, "
                    f"Island: {prog.island_idx})"
                )
                return prog

        # Fallback: if no generation 0 program found, try any correct program
        logger.warning(
            "No generation 0 program found, falling back to any correct program"
        )
        if self.island_idx is not None:
            self.cursor.execute(
                """SELECT id FROM programs
                   WHERE correct = 1 AND island_idx = ?
                   ORDER BY generation ASC, id ASC LIMIT 1""",
                (self.island_idx,),
            )
        else:
            self.cursor.execute(
                """SELECT id FROM programs
                   WHERE correct = 1
                   ORDER BY generation ASC, id ASC LIMIT 1"""
            )

        row = self.cursor.fetchone()
        if row:
            pid = row["id"]
            prog = self.get_program(pid)
            if prog:
                logger.info(
                    f"Best-of-N: Fallback to earliest correct program {pid} "
                    f"(Gen: {prog.generation}, "
                    f"Score: {prog.combined_score or 0.0:.4f}, "
                    f"Island: {prog.island_idx})"
                )
                return prog

        logger.warning("No suitable parent found for best-of-n strategy")
        return None


class SequentialSamplingStrategy(ParentSamplingStrategy):
    """Sequential sampling: always returns most recent correct program."""

    def sample_parent(self) -> Any:
        # Find the most recent correct program (highest generation)
        # in the specified island or globally
        if self.island_idx is not None:
            self.cursor.execute(
                """SELECT id FROM programs
                   WHERE correct = 1 AND island_idx = ?
                   ORDER BY generation DESC, id DESC LIMIT 1""",
                (self.island_idx,),
            )
        else:
            self.cursor.execute(
                """SELECT id FROM programs
                   WHERE correct = 1
                   ORDER BY generation DESC, id DESC LIMIT 1"""
            )

        row = self.cursor.fetchone()
        if row:
            pid = row["id"]
            prog = self.get_program(pid)
            if prog:
                logger.info(
                    f"Sequential: Selected most recent correct program "
                    f"{pid} (Gen: {prog.generation}, "
                    f"Score: {prog.combined_score or 0.0:.4f}, "
                    f"Island: {prog.island_idx})"
                )
                return prog

        # Fallback: if no correct program found, try any program
        logger.warning(
            "No correct program found for sequential strategy, "
            "falling back to any program"
        )
        if self.island_idx is not None:
            self.cursor.execute(
                """SELECT id FROM programs
                   WHERE island_idx = ?
                   ORDER BY generation DESC, id DESC LIMIT 1""",
                (self.island_idx,),
            )
        else:
            self.cursor.execute(
                """SELECT id FROM programs
                   ORDER BY generation DESC, id DESC LIMIT 1"""
            )

        row = self.cursor.fetchone()
        if row:
            pid = row["id"]
            prog = self.get_program(pid)
            if prog:
                logger.info(
                    f"Sequential: Fallback to most recent program {pid} "
                    f"(Gen: {prog.generation}, "
                    f"Score: {prog.combined_score or 0.0:.4f}, "
                    f"Island: {prog.island_idx})"
                )
                return prog

        logger.warning("No suitable parent found for sequential strategy")
        return None


class CombinedParentSelector:
    """Combined parent selector that handles all parent sampling strategies."""

    def __init__(
        self,
        cursor: sqlite3.Cursor,
        conn: sqlite3.Connection,
        config: Any,
        get_program_func: Callable[[str], Any],
        best_program_id: Optional[str] = None,
        beam_search_parent_id: Optional[str] = None,
        last_iteration: int = 0,
        update_metadata_func: Optional[Callable[[str, Optional[str]], None]] = None,
        get_best_program_func: Optional[Callable[[], Any]] = None,
    ):
        self.cursor = cursor
        self.conn = conn
        self.config = config
        self.get_program = get_program_func
        self.best_program_id = best_program_id
        self.beam_search_parent_id = beam_search_parent_id
        self.last_iteration = last_iteration
        self.update_metadata = update_metadata_func
        self.get_best_program_func = get_best_program_func

    def has_correct_programs(self, island_idx: Optional[int] = None) -> bool:
        """Check if there are any correct programs in the database."""
        if island_idx is not None:
            self.cursor.execute(
                "SELECT COUNT(*) FROM programs WHERE correct = 1 AND island_idx = ?",
                (island_idx,),
            )
        else:
            self.cursor.execute("SELECT COUNT(*) FROM programs WHERE correct = 1")
        count = self.cursor.fetchone()[0]
        return count > 0

    def get_incorrect_program_for_fix(
        self, island_idx: Optional[int] = None
    ) -> Optional[Any]:
        """
        Get an incorrect program that can be fixed.

        Randomly samples from all incorrect programs to encourage diverse
        fix attempts across different generations.

        Returns:
            An incorrect Program object, or None if no programs exist.
        """
        # Get all incorrect programs and randomly sample one
        if island_idx is not None:
            self.cursor.execute(
                """SELECT id FROM programs
                   WHERE correct = 0 AND island_idx = ?""",
                (island_idx,),
            )
        else:
            self.cursor.execute(
                """SELECT id FROM programs
                   WHERE correct = 0"""
            )

        rows = self.cursor.fetchall()
        if rows:
            # Randomly sample one incorrect program
            selected_row = rows[np.random.randint(len(rows))]
            prog = self.get_program(selected_row["id"])
            if prog:
                logger.info(
                    f"Selected incorrect program for fix: {prog.id} "
                    f"(Gen: {prog.generation}, "
                    f"Score: {prog.combined_score or 0.0:.4f}) "
                    f"[sampled from {len(rows)} incorrect programs]"
                )
                return prog

        return None

    def sample_parent_with_fix_mode(
        self, island_idx: Optional[int] = None
    ) -> Tuple[Any, bool]:
        """
        Sample a parent, returning fix mode indicator if no correct programs exist.

        Returns:
            Tuple of (program, needs_fix) where:
            - program: The sampled parent or incorrect program to fix
            - needs_fix: True if no correct programs exist and fix mode should be used
        """
        # First check if there are any correct programs
        if not self.has_correct_programs(island_idx):
            logger.warning(
                f"No correct programs found (island {island_idx}). Entering FIX mode."
            )
            incorrect_prog = self.get_incorrect_program_for_fix(island_idx)
            if incorrect_prog:
                return (incorrect_prog, True)
            else:
                raise ValueError("Database empty - no programs to sample or fix.")

        # Normal sampling - there are correct programs available
        parent = self.sample_parent(island_idx)
        return (parent, False)

    def sample_parent(self, island_idx: Optional[int] = None) -> Any:
        """Sample a parent using the configured sampling strategy."""
        strategy_name = self.config.parent_selection_strategy

        if strategy_name == "power_law":
            strategy = PowerLawSamplingStrategy(
                self.cursor,
                self.conn,
                self.config,
                self.get_program,
                self.best_program_id,
                island_idx,
            )
        elif strategy_name == "weighted":
            strategy = WeightedSamplingStrategy(
                self.cursor,
                self.conn,
                self.config,
                self.get_program,
                self.best_program_id,
                island_idx,
            )
        elif strategy_name == "beam_search":
            strategy = BeamSearchSamplingStrategy(
                cursor=self.cursor,
                conn=self.conn,
                config=self.config,
                get_program_func=self.get_program,
                best_program_id=self.best_program_id,
                island_idx=island_idx,
                beam_search_parent_id=self.beam_search_parent_id,
                last_iteration=self.last_iteration,
                update_metadata_func=self.update_metadata,
                get_best_program_func=self.get_best_program_func,
            )
        elif strategy_name == "best_of_n":
            strategy = BestOfNSamplingStrategy(
                self.cursor,
                self.conn,
                self.config,
                self.get_program,
                self.best_program_id,
                island_idx,
            )
        elif strategy_name == "sequential":
            strategy = SequentialSamplingStrategy(
                self.cursor,
                self.conn,
                self.config,
                self.get_program,
                self.best_program_id,
                island_idx,
            )
        else:
            raise ValueError(f"Unknown parent selection strategy: {strategy_name}")

        parent = strategy.sample_parent()

        # Fallback to best program if sampling failed
        if not parent:
            # Try best program first
            if self.best_program_id:
                parent = self.get_program(self.best_program_id)
                if (
                    parent
                    and parent.correct
                    and (island_idx is None or parent.island_idx == island_idx)
                ):
                    return parent

            # Final fallback: random correct program
            if island_idx is not None:
                self.cursor.execute(
                    """SELECT id FROM programs 
                       WHERE correct = 1 AND island_idx = ?
                       ORDER BY RANDOM() LIMIT 1""",
                    (island_idx,),
                )
            else:
                self.cursor.execute(
                    """SELECT id FROM programs 
                       ORDER BY RANDOM() LIMIT 1"""
                )
            row = self.cursor.fetchone()
            if row:
                parent = self.get_program(row["id"])

            if not parent:
                raise ValueError("Database empty or parent sampling failed.")

        return parent
