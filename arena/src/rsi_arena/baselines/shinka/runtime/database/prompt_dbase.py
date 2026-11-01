"""
SystemPrompt Database for meta-prompt evolution.

This module provides a database for storing and managing system prompts
during an evolutionary process. System prompts are evolved alongside
programs using similar mutation operators (diff, full, cross).
"""

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any, Dict, List, Optional
import numpy as np

logger = logging.getLogger(__name__)


def clean_nan_values(obj: Any) -> Any:
    """
    Recursively clean NaN values from a data structure, replacing them with
    None. This ensures JSON serialization works correctly.
    """
    import math

    if isinstance(obj, dict):
        return {key: clean_nan_values(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return [clean_nan_values(item) for item in obj]
    elif isinstance(obj, tuple):
        return tuple(clean_nan_values(item) for item in obj)
    elif isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    elif isinstance(obj, np.floating) and (np.isnan(obj) or np.isinf(obj)):
        return None
    elif hasattr(obj, "dtype") and np.issubdtype(obj.dtype, np.floating):
        if np.isscalar(obj):
            if np.isnan(obj) or np.isinf(obj):
                return None
            else:
                return float(obj)
        else:
            return clean_nan_values(obj.tolist())
    else:
        return obj


def prompt_db_retry(max_retries=5, initial_delay=0.1, backoff_factor=2):
    """
    A decorator to retry database operations on specific SQLite errors.
    """

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            delay = initial_delay
            for i in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except (
                    sqlite3.OperationalError,
                    sqlite3.DatabaseError,
                    sqlite3.IntegrityError,
                ) as e:
                    if i == max_retries - 1:
                        logger.error(
                            f"Prompt DB operation {func.__name__} failed after "
                            f"{max_retries} retries: {e}"
                        )
                        raise
                    logger.warning(
                        f"Prompt DB operation {func.__name__} failed with "
                        f"{type(e).__name__}: {e}. "
                        f"Retrying in {delay:.2f}s..."
                    )
                    time.sleep(delay)
                    delay *= backoff_factor
            raise RuntimeError(
                f"Prompt DB retry logic failed for function {func.__name__} without "
                "raising an exception."
            )

        return wrapper

    return decorator


@dataclass
class SystemPromptConfig:
    """Configuration for SystemPromptDatabase."""

    db_path: Optional[str] = None
    archive_size: int = 10
    # UCB selection parameters
    ucb_exploration_constant: float = 1.0  # Exploration constant (c) for UCB
    min_programs_for_fitness: int = 3  # Min programs before using fitness for selection
    # Epsilon-greedy exploration
    epsilon: float = 0.1  # Probability of random uniform sampling (0.0 to 1.0)
    # Optimistic prior for new prompts (instead of infinite UCB)
    use_optimistic_prior: bool = (
        True  # Use bounded optimistic prior instead of infinite UCB
    )


@dataclass
class SystemPrompt:
    """Represents a system prompt in the database."""

    # Prompt identification
    id: str
    prompt_text: str

    # Name and description (from LLM generation)
    name: Optional[str] = None
    description: Optional[str] = None

    # Evolution information
    parent_id: Optional[str] = None
    generation: int = 0  # Chronological prompt generation counter
    program_generation: int = 0  # Program generation at which this prompt was evolved
    patch_type: str = "init"  # "init", "diff", "full", "cross"
    timestamp: float = field(default_factory=time.time)

    # Fitness tracking (percentile-based)
    program_count: int = 0  # Total number of programs generated with this prompt
    correct_program_count: int = 0  # Number of correct programs (used for fitness)
    total_percentile: float = 0.0  # Sum of percentiles from correct programs
    fitness: float = 0.0  # Average percentile (0-1 scale, higher = better)

    # Raw data for percentile recomputation
    program_scores: List[float] = field(
        default_factory=list
    )  # Scores of correct programs

    # Legacy fields (kept for backward compatibility)
    total_improvement: float = 0.0  # Sum of improvements (deprecated)

    # Track associated programs
    program_ids: List[str] = field(default_factory=list)

    # Archive status
    in_archive: bool = False

    # Metadata
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict representation, cleaning NaN values for JSON."""
        data = asdict(self)
        return clean_nan_values(data)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SystemPrompt":
        """Create from dictionary representation."""
        # Ensure metadata is a dictionary
        data["metadata"] = (
            data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        )
        # Ensure program_ids is a list
        program_ids_val = data.get("program_ids")
        if isinstance(program_ids_val, list):
            data["program_ids"] = program_ids_val
        else:
            data["program_ids"] = []

        # Backward compatibility: if correct_program_count not present, use program_count
        if "correct_program_count" not in data:
            data["correct_program_count"] = data.get("program_count", 0)

        # Backward compatibility: if total_percentile not present, use 0.0
        # For old databases, fitness was based on improvement, not percentile
        if "total_percentile" not in data:
            data["total_percentile"] = 0.0

        # Backward compatibility: if program_scores not present, use empty list
        if "program_scores" not in data:
            data["program_scores"] = []

        # Filter out keys not in SystemPrompt fields
        prompt_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered_data = {k: v for k, v in data.items() if k in prompt_fields}

        return cls(**filtered_data)

    def update_fitness(
        self,
        percentile: float,
        correct: bool = True,
        improvement: float = 0.0,
        program_score: float = 0.0,
    ) -> None:
        """Update fitness with a new program's percentile score.

        Uses percentile-based fitness which is scale-invariant and automatically
        adjusts for performance saturation. Fitness represents the average
        percentile rank of programs generated with this prompt.

        Args:
            percentile: The percentile score (0-1), representing what fraction
                       of all programs this program beats
            correct: Whether the program was correct. Only correct programs
                     contribute to fitness calculation.
            improvement: Legacy improvement score (kept for backward compatibility)
            program_score: Raw program score (stored for percentile recomputation)
        """
        self.program_count += 1
        if correct:
            self.correct_program_count += 1
            self.total_percentile += percentile
            self.total_improvement += improvement  # Keep for backward compat
            self.program_scores.append(program_score)  # Store for recomputation
            if self.correct_program_count > 0:
                self.fitness = self.total_percentile / self.correct_program_count


class SystemPromptDatabase:
    """
    SQLite-backed database for storing and managing system prompts during
    meta-prompt evolution.
    """

    def __init__(
        self,
        config: SystemPromptConfig,
        read_only: bool = False,
    ):
        self.config = config
        self.conn: Optional[sqlite3.Connection] = None
        self.cursor: Optional[sqlite3.Cursor] = None
        self.read_only = read_only

        self.last_generation: int = 0
        self.best_prompt_id: Optional[str] = None

        db_path_str = getattr(self.config, "db_path", None)

        if db_path_str:
            db_file = Path(db_path_str).resolve()
            if not read_only:
                # Robustness check for unclean shutdown with WAL
                db_wal_file = Path(f"{db_file}-wal")
                db_shm_file = Path(f"{db_file}-shm")
                if (
                    db_file.exists()
                    and db_file.stat().st_size == 0
                    and (db_wal_file.exists() or db_shm_file.exists())
                ):
                    logger.warning(
                        f"Prompt database file {db_file} is empty but WAL/SHM files "
                        "exist. Removing WAL/SHM files to attempt recovery."
                    )
                    if db_wal_file.exists():
                        db_wal_file.unlink()
                    if db_shm_file.exists():
                        db_shm_file.unlink()
                db_file.parent.mkdir(parents=True, exist_ok=True)
                self.conn = sqlite3.connect(str(db_file), timeout=30.0)
                logger.debug(f"Connected to SystemPrompt SQLite database: {db_file}")
            else:
                if not db_file.exists():
                    raise FileNotFoundError(
                        f"Prompt database file not found for read-only connection: {db_file}"
                    )
                db_uri = f"file:{db_file}?mode=ro"
                self.conn = sqlite3.connect(db_uri, uri=True, timeout=30.0)
                logger.debug(
                    "Connected to SystemPrompt SQLite database in read-only mode: %s",
                    db_file,
                )
        else:
            self.conn = sqlite3.connect(":memory:")
            logger.info("Initialized in-memory SystemPrompt SQLite database.")

        self.conn.row_factory = sqlite3.Row
        self.cursor = self.conn.cursor()
        if not self.read_only:
            self._create_tables()
        self._load_metadata_from_db()

        count = self._count_prompts_in_db()
        logger.debug(f"SystemPrompt DB initialized with {count} prompts.")
        logger.debug(
            f"Last generation: {self.last_generation}. Best ID: {self.best_prompt_id}"
        )

    def _create_tables(self):
        if not self.cursor or not self.conn:
            raise ConnectionError("Prompt DB not connected.")

        # Set SQLite pragmas for better performance
        self.cursor.execute("PRAGMA journal_mode = WAL;")
        self.cursor.execute("PRAGMA busy_timeout = 30000;")
        self.cursor.execute("PRAGMA wal_autocheckpoint = 1000;")
        self.cursor.execute("PRAGMA synchronous = NORMAL;")
        self.cursor.execute("PRAGMA cache_size = -64000;")
        self.cursor.execute("PRAGMA temp_store = MEMORY;")
        self.cursor.execute("PRAGMA foreign_keys = ON;")

        self.cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS system_prompts (
                id TEXT PRIMARY KEY,
                prompt_text TEXT NOT NULL,
                name TEXT,
                description TEXT,
                parent_id TEXT,
                generation INTEGER NOT NULL,
                program_generation INTEGER NOT NULL DEFAULT 0,
                patch_type TEXT NOT NULL,
                timestamp REAL NOT NULL,
                program_count INTEGER NOT NULL DEFAULT 0,
                correct_program_count INTEGER NOT NULL DEFAULT 0,
                total_percentile REAL NOT NULL DEFAULT 0.0,
                total_improvement REAL NOT NULL DEFAULT 0.0,
                fitness REAL NOT NULL DEFAULT 0.0,
                program_scores TEXT,  -- JSON serialized List[float] for percentile recomputation
                program_ids TEXT,  -- JSON serialized List[str]
                metadata TEXT      -- JSON serialized Dict[str, Any]
            )
            """
        )

        # Migration: Add correct_program_count column if it doesn't exist
        try:
            self.cursor.execute(
                "ALTER TABLE system_prompts ADD COLUMN correct_program_count "
                "INTEGER NOT NULL DEFAULT 0"
            )
            self.conn.commit()
            logger.info("Added correct_program_count column to system_prompts table")
        except sqlite3.OperationalError:
            # Column already exists, ignore
            pass

        # Migration: Add program_generation column if it doesn't exist
        try:
            self.cursor.execute(
                "ALTER TABLE system_prompts ADD COLUMN program_generation "
                "INTEGER NOT NULL DEFAULT 0"
            )
            self.conn.commit()
            logger.info("Added program_generation column to system_prompts table")
        except sqlite3.OperationalError:
            # Column already exists, ignore
            pass

        # Migration: Add name column if it doesn't exist
        try:
            self.cursor.execute("ALTER TABLE system_prompts ADD COLUMN name TEXT")
            self.conn.commit()
            logger.info("Added name column to system_prompts table")
        except sqlite3.OperationalError:
            # Column already exists, ignore
            pass

        # Migration: Add description column if it doesn't exist
        try:
            self.cursor.execute(
                "ALTER TABLE system_prompts ADD COLUMN description TEXT"
            )
            self.conn.commit()
            logger.info("Added description column to system_prompts table")
        except sqlite3.OperationalError:
            # Column already exists, ignore
            pass

        # Migration: Add total_percentile column if it doesn't exist
        try:
            self.cursor.execute(
                "ALTER TABLE system_prompts ADD COLUMN total_percentile "
                "REAL NOT NULL DEFAULT 0.0"
            )
            self.conn.commit()
            logger.info("Added total_percentile column to system_prompts table")
        except sqlite3.OperationalError:
            # Column already exists, ignore
            pass

        # Migration: Add program_scores column if it doesn't exist
        try:
            self.cursor.execute(
                "ALTER TABLE system_prompts ADD COLUMN program_scores TEXT"
            )
            self.conn.commit()
            logger.info("Added program_scores column to system_prompts table")
        except sqlite3.OperationalError:
            # Column already exists, ignore
            pass

        # Add indices for common query patterns
        idx_cmds = [
            "CREATE INDEX IF NOT EXISTS idx_prompts_generation ON "
            "system_prompts(generation)",
            "CREATE INDEX IF NOT EXISTS idx_prompts_timestamp ON "
            "system_prompts(timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_prompts_fitness ON system_prompts(fitness)",
            "CREATE INDEX IF NOT EXISTS idx_prompts_parent_id ON "
            "system_prompts(parent_id)",
        ]
        for cmd in idx_cmds:
            self.cursor.execute(cmd)

        self.cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS prompt_archive (
                prompt_id TEXT PRIMARY KEY,
                FOREIGN KEY (prompt_id) REFERENCES system_prompts(id)
                    ON DELETE CASCADE
            )
            """
        )

        self.cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS prompt_metadata_store (
                key TEXT PRIMARY KEY, value TEXT
            )
            """
        )

        self.conn.commit()
        logger.debug("SystemPrompt database tables and indices created.")

    @prompt_db_retry()
    def _load_metadata_from_db(self):
        if not self.cursor:
            raise ConnectionError("Prompt DB cursor not available.")

        self.cursor.execute(
            "SELECT value FROM prompt_metadata_store WHERE key = 'last_generation'"
        )
        row = self.cursor.fetchone()
        self.last_generation = (
            int(row["value"]) if row and row["value"] is not None else 0
        )
        if not row or row["value"] is not None:
            if not self.read_only:
                self._update_metadata_in_db(
                    "last_generation", str(self.last_generation)
                )

        self.cursor.execute(
            "SELECT value FROM prompt_metadata_store WHERE key = 'best_prompt_id'"
        )
        row = self.cursor.fetchone()
        self.best_prompt_id = (
            str(row["value"])
            if row and row["value"] is not None and row["value"] != "None"
            else None
        )
        if not row or row["value"] is None or row["value"] == "None":
            if not self.read_only:
                self._update_metadata_in_db("best_prompt_id", None)

    @prompt_db_retry()
    def _update_metadata_in_db(self, key: str, value: Optional[str]):
        if not self.cursor or not self.conn:
            raise ConnectionError("Prompt DB not connected.")
        self.cursor.execute(
            "INSERT OR REPLACE INTO prompt_metadata_store (key, value) VALUES (?, ?)",
            (key, value),
        )
        self.conn.commit()

    @prompt_db_retry()
    def _count_prompts_in_db(self) -> int:
        if not self.cursor:
            return 0
        self.cursor.execute("SELECT COUNT(*) FROM system_prompts")
        return (self.cursor.fetchone() or {"COUNT(*)": 0})["COUNT(*)"]

    @prompt_db_retry()
    def add(self, prompt: SystemPrompt, verbose: bool = False) -> str:
        """
        Add a system prompt to the database.

        Args:
            prompt: The SystemPrompt object to add
            verbose: Enable verbose logging

        Returns:
            str: The ID of the added prompt
        """
        if self.read_only:
            raise PermissionError("Cannot add prompt in read-only mode.")
        if not self.cursor or not self.conn:
            raise ConnectionError("Prompt DB not connected.")

        # Pre-serialize JSON data
        program_scores_json = json.dumps(prompt.program_scores or [])
        program_ids_json = json.dumps(prompt.program_ids or [])
        metadata_json = json.dumps(prompt.metadata or {})

        # Begin transaction
        self.conn.execute("BEGIN TRANSACTION")

        try:
            self.cursor.execute(
                """
                INSERT INTO system_prompts
                   (id, prompt_text, name, description, parent_id, generation,
                    program_generation, patch_type, timestamp, program_count,
                    correct_program_count, total_percentile, total_improvement,
                    fitness, program_scores, program_ids, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    prompt.id,
                    prompt.prompt_text,
                    prompt.name,
                    prompt.description,
                    prompt.parent_id,
                    prompt.generation,
                    prompt.program_generation,
                    prompt.patch_type,
                    prompt.timestamp,
                    prompt.program_count,
                    prompt.correct_program_count,
                    prompt.total_percentile,
                    prompt.total_improvement,
                    prompt.fitness,
                    program_scores_json,
                    program_ids_json,
                    metadata_json,
                ),
            )

            self.conn.commit()
            logger.info(
                "SystemPrompt %s added to DB - generation: %s, patch_type: %s",
                prompt.id,
                prompt.generation,
                prompt.patch_type,
            )

        except sqlite3.IntegrityError as e:
            self.conn.rollback()
            logger.error(f"IntegrityError for prompt {prompt.id}: {e}")
            raise
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Error adding prompt {prompt.id}: {e}")
            raise

        # Update archive - force add for newly evolved prompts (those with a parent)
        # This ensures new prompts get a chance to be sampled and evaluated
        is_evolved = prompt.parent_id is not None
        self._update_archive(prompt, force=is_evolved)

        # Update best prompt tracking
        self._update_best_prompt(prompt)

        # Update generation tracking
        if prompt.generation > self.last_generation:
            self.last_generation = prompt.generation
            self._update_metadata_in_db("last_generation", str(self.last_generation))

        if verbose:
            logger.info(
                f"Added prompt {prompt.id[:8]}... (gen={prompt.generation}, "
                f"patch={prompt.patch_type}, fitness={prompt.fitness:.4f})"
            )

        return prompt.id

    def _prompt_from_row(self, row: sqlite3.Row) -> Optional[SystemPrompt]:
        """Helper to create a SystemPrompt object from a database row."""
        if not row:
            return None

        prompt_data = dict(row)

        # Handle JSON deserialization
        program_scores_text = prompt_data.get("program_scores")
        if program_scores_text:
            try:
                prompt_data["program_scores"] = json.loads(program_scores_text)
            except json.JSONDecodeError:
                prompt_data["program_scores"] = []
        else:
            prompt_data["program_scores"] = []

        program_ids_text = prompt_data.get("program_ids")
        if program_ids_text:
            try:
                prompt_data["program_ids"] = json.loads(program_ids_text)
            except json.JSONDecodeError:
                prompt_data["program_ids"] = []
        else:
            prompt_data["program_ids"] = []

        metadata_text = prompt_data.get("metadata")
        if metadata_text:
            try:
                prompt_data["metadata"] = json.loads(metadata_text)
            except json.JSONDecodeError:
                prompt_data["metadata"] = {}
        else:
            prompt_data["metadata"] = {}

        # Handle archive status
        prompt_data["in_archive"] = bool(prompt_data.get("in_archive", 0))

        # Backward compatibility: if correct_program_count not in row, use program_count
        if (
            "correct_program_count" not in prompt_data
            or prompt_data["correct_program_count"] is None
        ):
            prompt_data["correct_program_count"] = prompt_data.get("program_count", 0)

        # Backward compatibility: if total_percentile not in row, use 0.0
        if (
            "total_percentile" not in prompt_data
            or prompt_data["total_percentile"] is None
        ):
            prompt_data["total_percentile"] = 0.0

        return SystemPrompt.from_dict(prompt_data)

    @prompt_db_retry()
    def get(self, prompt_id: str) -> Optional[SystemPrompt]:
        """Get a system prompt by its ID."""
        if not self.cursor:
            raise ConnectionError("Prompt DB not connected.")
        self.cursor.execute("SELECT * FROM system_prompts WHERE id = ?", (prompt_id,))
        row = self.cursor.fetchone()
        return self._prompt_from_row(row)

    @prompt_db_retry()
    def get_all_prompts(self) -> List[SystemPrompt]:
        """Get all system prompts from the database."""
        if not self.cursor:
            raise ConnectionError("Prompt DB not connected.")
        self.cursor.execute(
            """
            SELECT p.*,
                   CASE WHEN a.prompt_id IS NOT NULL THEN 1 ELSE 0 END as in_archive
            FROM system_prompts p
            LEFT JOIN prompt_archive a ON p.id = a.prompt_id
            """
        )
        rows = self.cursor.fetchall()
        prompts = [self._prompt_from_row(row) for row in rows]
        return [p for p in prompts if p is not None]

    @prompt_db_retry()
    def get_archive(self) -> List[SystemPrompt]:
        """Get all prompts in the archive."""
        if not self.cursor:
            raise ConnectionError("Prompt DB not connected.")
        self.cursor.execute(
            """
            SELECT p.*, 1 as in_archive
            FROM system_prompts p
            JOIN prompt_archive a ON p.id = a.prompt_id
            ORDER BY p.fitness DESC
            """
        )
        rows = self.cursor.fetchall()
        prompts = [self._prompt_from_row(row) for row in rows]
        return [p for p in prompts if p is not None]

    @prompt_db_retry()
    def get_total_evolution_costs(self) -> float:
        """
        Calculate total LLM costs from all prompt evolution operations.

        Returns the sum of all costs stored in prompt metadata["llm"]["cost"].
        """
        if not self.cursor:
            raise ConnectionError("Prompt DB not connected.")

        total_costs = 0.0
        all_prompts = self.get_all_prompts()
        for prompt in all_prompts:
            if prompt.metadata:
                # LLM costs are stored in metadata["llm"]["cost"]
                llm_data = prompt.metadata.get("llm", {})
                if isinstance(llm_data, dict):
                    cost = llm_data.get("cost", 0.0)
                    if cost is not None:
                        total_costs += cost
        return total_costs

    @prompt_db_retry()
    def sample(
        self,
        exploration_constant: Optional[float] = None,
        epsilon: Optional[float] = None,
    ) -> Optional[SystemPrompt]:
        """
        Sample a prompt from the archive using UCB with epsilon-greedy exploration.

        Uses UCB1 formula: UCB = avg_improvement + c * sqrt(log(N) / n)
        where:
        - avg_improvement: average improvement (fitness) of the prompt
        - c: exploration constant (higher = more exploration)
        - N: total number of correct programs across all prompts
        - n: number of correct programs for this prompt

        With epsilon-greedy: with probability epsilon, sample uniformly at random
        instead of using UCB. This ensures exploration of the full archive.

        For new prompts with insufficient data, uses an optimistic prior instead
        of infinite UCB to prevent newest prompts from always being selected.

        Args:
            exploration_constant: Override for UCB exploration constant
            epsilon: Override for epsilon-greedy probability (0.0 to 1.0)

        Returns:
            A sampled SystemPrompt, or None if archive is empty
        """
        archive = self.get_archive()

        if not archive:
            logger.warning("No prompts in archive to sample from")
            return None

        # If only one prompt, return it
        if len(archive) == 1:
            return archive[0]

        # Get epsilon value (use config default if not specified)
        eps = epsilon if epsilon is not None else self.config.epsilon

        # Epsilon-greedy: with probability epsilon, sample uniformly at random
        if eps > 0 and np.random.random() < eps:
            idx = np.random.randint(len(archive))
            selected = archive[idx]
            logger.debug(
                f"Sampled prompt {selected.id[:8]}... "
                f"(epsilon-greedy random, eps={eps:.2f})"
            )
            return selected

        c = (
            exploration_constant
            if exploration_constant is not None
            else self.config.ucb_exploration_constant
        )
        min_programs = self.config.min_programs_for_fitness
        use_optimistic = self.config.use_optimistic_prior

        # Calculate total correct programs across all prompts
        total_correct = sum(p.correct_program_count for p in archive)

        # If no programs yet, use uniform random selection
        if total_correct == 0:
            idx = np.random.randint(len(archive))
            selected = archive[idx]
            logger.debug(
                f"Sampled prompt {selected.id[:8]}... (uniform, no programs yet)"
            )
            return selected

        # Calculate optimistic prior for new prompts (if enabled)
        # Use max observed fitness + exploration bonus as a bounded prior
        if use_optimistic:
            established_prompts = [
                p for p in archive if p.correct_program_count >= min_programs
            ]
            if established_prompts:
                max_fitness = max(p.fitness for p in established_prompts)
            else:
                max_fitness = 0.5  # Default assumption
            optimistic_prior = max_fitness + c  # Optimistic but bounded

        # Calculate UCB scores for each prompt
        ucb_scores = []
        for p in archive:
            if p.correct_program_count < min_programs:
                if use_optimistic:
                    # Use optimistic prior instead of infinite UCB
                    ucb_scores.append(optimistic_prior)
                else:
                    # Legacy behavior: infinite UCB for new prompts
                    ucb_scores.append(float("inf"))
            else:
                # UCB1 formula: avg_reward + c * sqrt(log(N) / n)
                exploitation = p.fitness
                exploration = c * np.sqrt(
                    np.log(total_correct) / p.correct_program_count
                )
                ucb_scores.append(exploitation + exploration)

        ucb_scores = np.array(ucb_scores)

        # Handle infinite scores (only if not using optimistic prior)
        inf_mask = np.isinf(ucb_scores)
        if np.any(inf_mask):
            # Randomly select among prompts with infinite UCB (need exploration)
            inf_indices = np.where(inf_mask)[0]
            idx = np.random.choice(inf_indices)
        else:
            # Select the prompt with highest UCB score
            idx = np.argmax(ucb_scores)

        selected = archive[idx]

        logger.debug(
            f"Sampled prompt {selected.id[:8]}... "
            f"(UCB={ucb_scores[idx]:.4f}, fitness={selected.fitness:.4f}, "
            f"correct_programs={selected.correct_program_count})"
        )
        return selected

    @prompt_db_retry()
    def update_fitness(
        self,
        prompt_id: str,
        percentile: float,
        program_id: Optional[str] = None,
        correct: bool = True,
        improvement: float = 0.0,
        program_score: float = 0.0,
    ) -> None:
        """
        Update the fitness of a prompt with a new program's percentile score.

        Uses percentile-based fitness which is scale-invariant and automatically
        adjusts for performance saturation. A prompt's fitness represents the
        average percentile rank of programs generated with it.

        Args:
            prompt_id: ID of the prompt to update
            percentile: The percentile score (0-1), representing what fraction
                       of all correct programs this program beats
            program_id: Optional ID of the program to associate
            correct: Whether the program was correct. Only correct programs
                     contribute to fitness calculation.
            improvement: Legacy improvement score (kept for backward compatibility)
            program_score: Raw program score (stored for percentile recomputation)
        """
        if self.read_only:
            raise PermissionError("Cannot update fitness in read-only mode.")
        if not self.cursor or not self.conn:
            raise ConnectionError("Prompt DB not connected.")

        # Get current values
        prompt = self.get(prompt_id)
        if not prompt:
            logger.warning(f"Prompt {prompt_id} not found for fitness update")
            return

        # Update values - always increment program_count
        new_count = prompt.program_count + 1
        new_correct_count = prompt.correct_program_count
        new_total_percentile = prompt.total_percentile
        new_total_improvement = prompt.total_improvement
        new_program_scores = prompt.program_scores.copy()

        # Only update fitness-related values for correct programs
        if correct:
            new_correct_count += 1
            new_total_percentile += percentile
            new_total_improvement += improvement
            new_program_scores.append(program_score)

        # Fitness is average percentile over correct programs only
        new_fitness = (
            new_total_percentile / new_correct_count if new_correct_count > 0 else 0.0
        )

        # Update program_ids list
        new_program_ids = prompt.program_ids.copy()
        if program_id:
            new_program_ids.append(program_id)

        # Serialize lists to JSON
        program_scores_json = json.dumps(new_program_scores)
        program_ids_json = json.dumps(new_program_ids)

        # Update database
        self.cursor.execute(
            """
            UPDATE system_prompts
            SET program_count = ?,
                correct_program_count = ?,
                total_percentile = ?,
                total_improvement = ?,
                fitness = ?,
                program_scores = ?,
                program_ids = ?
            WHERE id = ?
            """,
            (
                new_count,
                new_correct_count,
                new_total_percentile,
                new_total_improvement,
                new_fitness,
                program_scores_json,
                program_ids_json,
                prompt_id,
            ),
        )
        self.conn.commit()

        logger.debug(
            f"Updated prompt {prompt_id[:8]}... fitness: "
            f"{prompt.fitness:.4f} -> {new_fitness:.4f} "
            f"(percentile={percentile:.4f}, score={program_score:.4f}, "
            f"correct={correct}, total={new_count}, "
            f"correct_count={new_correct_count})"
        )

        # Re-evaluate archive membership
        prompt.program_count = new_count
        prompt.correct_program_count = new_correct_count
        prompt.total_percentile = new_total_percentile
        prompt.total_improvement = new_total_improvement
        prompt.program_scores = new_program_scores
        prompt.fitness = new_fitness
        self._update_archive(prompt)
        self._update_best_prompt(prompt)

    @prompt_db_retry()
    def get_best_prompt(self) -> Optional[SystemPrompt]:
        """Get the prompt with the highest fitness."""
        if not self.cursor:
            raise ConnectionError("Prompt DB not connected.")

        # Try to use tracked best_prompt_id first
        if self.best_prompt_id:
            prompt = self.get(self.best_prompt_id)
            if prompt:
                return prompt
            else:
                logger.warning(
                    f"Tracked best_prompt_id '{self.best_prompt_id}' not found."
                )
                if not self.read_only:
                    self._update_metadata_in_db("best_prompt_id", None)
                self.best_prompt_id = None

        # Find best by fitness (only consider prompts with enough correct programs)
        self.cursor.execute(
            """
            SELECT * FROM system_prompts
            WHERE correct_program_count >= ?
            ORDER BY fitness DESC
            LIMIT 1
            """,
            (self.config.min_programs_for_fitness,),
        )
        row = self.cursor.fetchone()

        if not row:
            # Fallback: get any prompt if none have enough programs
            self.cursor.execute(
                "SELECT * FROM system_prompts ORDER BY fitness DESC LIMIT 1"
            )
            row = self.cursor.fetchone()

        if row:
            prompt = self._prompt_from_row(row)
            if prompt and self.best_prompt_id != prompt.id:
                self.best_prompt_id = prompt.id
                if not self.read_only:
                    self._update_metadata_in_db("best_prompt_id", self.best_prompt_id)
            return prompt

        return None

    def _is_better(self, prompt1: SystemPrompt, prompt2: SystemPrompt) -> bool:
        """Compare two prompts by fitness."""
        # Prefer prompts with more correct data points
        min_programs = self.config.min_programs_for_fitness
        p1_reliable = prompt1.correct_program_count >= min_programs
        p2_reliable = prompt2.correct_program_count >= min_programs

        if p1_reliable and not p2_reliable:
            return True
        if p2_reliable and not p1_reliable:
            return False

        # Both reliable or both unreliable: compare fitness
        if prompt1.fitness != prompt2.fitness:
            return prompt1.fitness > prompt2.fitness

        # Tie-breaker: prefer newer prompts
        return prompt1.timestamp > prompt2.timestamp

    @prompt_db_retry()
    def _update_archive(self, prompt: SystemPrompt, force: bool = False) -> None:
        """Update the archive with the new prompt if it qualifies.

        Args:
            prompt: The prompt to potentially add to the archive
            force: If True, always add the prompt to the archive (replacing the
                   worst prompt if full). Used for newly evolved prompts to ensure
                   they get a chance to be sampled and evaluated.
        """
        if (
            not self.cursor
            or not self.conn
            or not hasattr(self.config, "archive_size")
            or self.config.archive_size <= 0
        ):
            logger.debug("Archive update skipped.")
            return

        self.cursor.execute("SELECT COUNT(*) FROM prompt_archive")
        count = (self.cursor.fetchone() or [0])[0]

        if count < self.config.archive_size:
            self.cursor.execute(
                "INSERT OR IGNORE INTO prompt_archive (prompt_id) VALUES (?)",
                (prompt.id,),
            )
        else:
            # Archive is full, find worst to replace
            self.cursor.execute(
                """
                SELECT a.prompt_id, p.fitness, p.program_count, 
                       p.correct_program_count, p.timestamp
                FROM prompt_archive a
                JOIN system_prompts p ON a.prompt_id = p.id
                """
            )
            archived_rows = self.cursor.fetchall()
            if not archived_rows:
                self.cursor.execute(
                    "INSERT OR IGNORE INTO prompt_archive (prompt_id) VALUES (?)",
                    (prompt.id,),
                )
                self.conn.commit()
                return

            # Find worst prompt in archive
            archive_prompts = []
            for r_data in archived_rows:
                archive_prompts.append(
                    SystemPrompt(
                        id=r_data["prompt_id"],
                        prompt_text="",
                        fitness=r_data["fitness"],
                        program_count=r_data["program_count"],
                        correct_program_count=r_data["correct_program_count"] or 0,
                        timestamp=r_data["timestamp"],
                    )
                )

            worst_in_archive = archive_prompts[0]
            for p_archived in archive_prompts[1:]:
                if self._is_better(worst_in_archive, p_archived):
                    worst_in_archive = p_archived

            # Force add: always replace worst (for newly evolved prompts)
            # Normal add: only replace if new prompt is better
            should_replace = force or self._is_better(prompt, worst_in_archive)

            if should_replace:
                self.cursor.execute(
                    "DELETE FROM prompt_archive WHERE prompt_id = ?",
                    (worst_in_archive.id,),
                )
                self.cursor.execute(
                    "INSERT INTO prompt_archive (prompt_id) VALUES (?)",
                    (prompt.id,),
                )
                if force:
                    logger.info(
                        f"Prompt {prompt.id[:8]}... force-added to archive, "
                        f"replacing {worst_in_archive.id[:8]}..."
                    )
                else:
                    logger.info(
                        f"Prompt {prompt.id[:8]}... replaced "
                        f"{worst_in_archive.id[:8]}... in archive."
                    )

        self.conn.commit()

    @prompt_db_retry()
    def _update_best_prompt(self, prompt: SystemPrompt) -> None:
        """Update the best prompt tracking."""
        current_best = None
        if self.best_prompt_id:
            current_best = self.get(self.best_prompt_id)

        if current_best is None or self._is_better(prompt, current_best):
            self.best_prompt_id = prompt.id
            if not self.read_only:
                self._update_metadata_in_db("best_prompt_id", self.best_prompt_id)

            log_msg = f"New best prompt: {prompt.id[:8]}..."
            if current_best:
                log_msg += (
                    f" (fitness: {current_best.fitness:.4f} -> {prompt.fitness:.4f})"
                )
            else:
                log_msg += f" (fitness: {prompt.fitness:.4f})"
            logger.info(log_msg)

    @prompt_db_retry()
    def get_prompts_by_generation(self, generation: int) -> List[SystemPrompt]:
        """Get all prompts from a specific generation."""
        if not self.cursor:
            raise ConnectionError("Prompt DB not connected.")
        self.cursor.execute(
            "SELECT * FROM system_prompts WHERE generation = ?", (generation,)
        )
        rows = self.cursor.fetchall()
        prompts = [self._prompt_from_row(row) for row in rows]
        return [p for p in prompts if p is not None]

    @prompt_db_retry()
    def get_lineage(
        self, prompt_id: str, max_ancestors: int = 10
    ) -> List[SystemPrompt]:
        """Get the ancestry of a prompt by walking up the parent chain."""
        if not self.cursor:
            raise ConnectionError("Prompt DB not connected.")

        ancestors: List[SystemPrompt] = []
        current_id = prompt_id

        for _ in range(max_ancestors):
            self.cursor.execute(
                "SELECT parent_id FROM system_prompts WHERE id = ?", (current_id,)
            )
            row = self.cursor.fetchone()
            if not row or not row["parent_id"]:
                break

            parent_id = row["parent_id"]
            parent = self.get(parent_id)
            if parent:
                ancestors.append(parent)
                current_id = parent_id
            else:
                break

        # Reverse to get chronological order (oldest ancestor first)
        ancestors.reverse()
        return ancestors

    def recompute_all_percentiles(
        self,
        all_program_scores: Optional[List[float]] = None,
        program_id_to_score: Optional[Dict[str, float]] = None,
    ) -> None:
        """
        Recompute percentile-based fitness for all prompts.

        This recalculates each prompt's fitness as the average percentile of
        its programs' scores relative to a reference population of scores.

        Args:
            all_program_scores: List of all correct program scores from the
                main programs database. Percentiles are computed against this.
            program_id_to_score: Optional mapping from program_id to current
                score. If provided, uses current scores for each prompt's
                programs (matching webUI). If None, uses stored program_scores.
        """
        if self.read_only:
            raise PermissionError("Cannot recompute in read-only mode.")
        if not self.cursor or not self.conn:
            raise ConnectionError("Prompt DB not connected.")

        # Get all prompts with their scores
        prompts = self.get_all_prompts()

        # Use provided scores or collect from prompts
        if all_program_scores is not None:
            all_scores = [s for s in all_program_scores if s is not None]
        else:
            # Fallback: collect all scores from all prompts
            all_scores = []
            for p in prompts:
                all_scores.extend(p.program_scores)

        if not all_scores:
            logger.warning("No program scores found, skipping percentile recompute")
            return

        logger.info(
            f"Recomputing percentiles for {len(prompts)} prompts "
            f"using {len(all_scores)} total scores"
        )

        # Recompute percentiles for each prompt
        for prompt in prompts:
            # Get scores for this prompt's programs
            if program_id_to_score is not None:
                # Use current scores from main database (matches webUI)
                scores_for_prompt = [
                    program_id_to_score[pid]
                    for pid in prompt.program_ids
                    if pid in program_id_to_score
                ]
            else:
                # Fallback: use stored program_scores
                scores_for_prompt = prompt.program_scores

            if not scores_for_prompt:
                continue

            # Compute percentile for each score
            # Use strict "beats" (score > s) to match webUI calculation
            # Divide by (n-1) to exclude self - best program should beat 100%
            percentiles = []
            num_other_programs = len(all_scores) - 1
            for score in scores_for_prompt:
                if num_other_programs <= 0:
                    percentiles.append(1.0)  # Only program = 100%
                else:
                    beats = sum(1 for s in all_scores if score > s)
                    percentile = beats / num_other_programs
                    percentiles.append(percentile)

            # Update prompt
            new_total_percentile = sum(percentiles)
            new_fitness = (
                new_total_percentile / len(percentiles) if percentiles else 0.0
            )

            self.cursor.execute(
                """
                UPDATE system_prompts
                SET total_percentile = ?, fitness = ?
                WHERE id = ?
                """,
                (new_total_percentile, new_fitness, prompt.id),
            )

        self.conn.commit()
        logger.info("Percentile recomputation complete")

    def save(self, path: Optional[str] = None) -> None:
        """Save/commit the database state."""
        if not self.conn or not self.cursor:
            logger.warning("No DB connection, skipping save.")
            return

        self._update_metadata_in_db("last_generation", str(self.last_generation))
        self.conn.commit()
        logger.info(
            f"SystemPrompt database committed. Last generation: "
            f"{self.last_generation}. Best: {self.best_prompt_id}"
        )

    def close(self):
        """Closes the database connection."""
        if self.conn:
            self.conn.close()

    def print_summary(self) -> None:
        """Print a summary of the prompt database."""
        from rich.console import Console
        from rich.table import Table

        console = Console()

        total = self._count_prompts_in_db()
        archive = self.get_archive()
        best = self.get_best_prompt()

        console.print(f"\n[bold]SystemPrompt Database Summary[/bold]")
        console.print(f"Total prompts: {total}")
        console.print(f"Archive size: {len(archive)}/{self.config.archive_size}")

        if best:
            console.print(
                f"Best prompt: {best.id[:8]}... "
                f"(fitness={best.fitness:.4f}, programs={best.program_count})"
            )

        if archive:
            table = Table(title="Archive Prompts")
            table.add_column("ID", style="cyan")
            table.add_column("Gen", justify="right")
            table.add_column("Patch", style="green")
            table.add_column("Fitness", justify="right")
            table.add_column("Programs", justify="right")

            for p in archive[:10]:  # Show top 10
                table.add_row(
                    p.id[:8] + "...",
                    str(p.generation),
                    p.patch_type,
                    f"{p.fitness:.4f}",
                    str(p.program_count),
                )

            console.print(table)


def create_system_prompt(
    prompt_text: str,
    parent_id: Optional[str] = None,
    generation: int = 0,
    program_generation: int = 0,
    patch_type: str = "init",
    metadata: Optional[Dict[str, Any]] = None,
    name: Optional[str] = None,
    description: Optional[str] = None,
) -> SystemPrompt:
    """
    Factory function to create a new SystemPrompt with a unique ID.

    Args:
        prompt_text: The system prompt text
        parent_id: ID of parent prompt (if evolved from another)
        generation: Chronological prompt generation counter
        program_generation: Program generation at which this prompt was evolved
        patch_type: How this prompt was created ("init", "diff", "full", "cross")
        metadata: Additional metadata
        name: Short name for the prompt (from LLM generation)
        description: Description of the prompt approach (from LLM generation)

    Returns:
        A new SystemPrompt instance
    """
    return SystemPrompt(
        id=str(uuid.uuid4()),
        prompt_text=prompt_text,
        name=name,
        description=description,
        parent_id=parent_id,
        generation=generation,
        program_generation=program_generation,
        patch_type=patch_type,
        timestamp=time.time(),
        metadata=metadata or {},
    )
