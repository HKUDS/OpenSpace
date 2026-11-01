import logging
import sqlite3
from abc import ABC, abstractmethod
from typing import Optional, Callable, Any, List, Set, Literal

logger = logging.getLogger(__name__)


class ContextSelectorStrategy(ABC):
    """Abstract base class for context selection strategies."""

    def __init__(
        self,
        cursor: sqlite3.Cursor,
        conn: sqlite3.Connection,
        config: Any,
        get_program_func: Callable[[str], Any],
        best_program_id: Optional[str] = None,
        get_island_idx_func: Optional[Callable[[str], Optional[int]]] = None,
        program_from_row_func: Optional[Callable[[sqlite3.Row], Any]] = None,
    ):
        self.cursor = cursor
        self.conn = conn
        self.config = config
        self.get_program = get_program_func
        self.best_program_id = best_program_id
        self.get_island_idx = get_island_idx_func
        self.program_from_row = program_from_row_func

    @abstractmethod
    def sample_context(self, parent: Any, n: int) -> List[Any]:
        """Sample context programs for the given parent."""
        pass


class ArchiveInspirationSelector(ContextSelectorStrategy):
    """Strategy for selecting archive inspirations."""

    def sample_context(self, parent: Any, n: int) -> List[Any]:
        """Sample archive inspirations based on elites, best program, and random selection."""
        if n <= 0:
            return []
        if not hasattr(self.config, "elite_selection_ratio"):
            raise ConnectionError(
                "Config missing elite_selection_ratio for inspiration sampling."
            )

        parent_island_idx = (
            self.get_island_idx(parent.id) if self.get_island_idx else None
        )

        inspirations: List[Any] = []
        insp_ids: Set[str] = {parent.id}

        enforce_separation = getattr(self.config, "enforce_island_separation", False)

        # 1. Best program (only if correct)
        if self.best_program_id and self.best_program_id not in insp_ids:
            prog = self.get_program(self.best_program_id)
            if prog and prog.correct:
                if enforce_separation:
                    if prog.island_idx == parent_island_idx:
                        inspirations.append(prog)
                        insp_ids.add(prog.id)
                else:
                    inspirations.append(prog)
                    insp_ids.add(prog.id)

        # 2. Elites from parent's island
        num_elites = max(0, int(n * self.config.elite_selection_ratio))
        if num_elites > 0 and len(inspirations) < n and parent_island_idx is not None:
            self.cursor.execute(
                """
                SELECT p.id FROM programs p
                JOIN archive a ON p.id = a.program_id
                WHERE p.island_idx = ? AND p.correct = 1
                ORDER BY p.combined_score DESC
                LIMIT ?
                """,
                (parent_island_idx, num_elites + len(insp_ids)),
            )
            for row in self.cursor.fetchall():
                if len(inspirations) >= n:
                    break
                prog = self.get_program(row["id"])
                if prog and prog.id not in insp_ids:
                    inspirations.append(prog)
                    insp_ids.add(prog.id)

        # 3. Random correct programs from parent's island
        if len(inspirations) < n and parent_island_idx is not None:
            needed = n - len(inspirations)
            if needed > 0:
                placeholders_rand = ",".join("?" * len(insp_ids))
                sql_rand = f"""
                    SELECT p.id FROM programs p
                    JOIN archive a ON p.id = a.program_id
                    WHERE p.island_idx = ? AND p.correct = 1
                    AND p.id NOT IN ({placeholders_rand})
                    ORDER BY RANDOM() LIMIT ?
                """
                params_rand = [parent_island_idx] + list(insp_ids) + [needed]

                self.cursor.execute(sql_rand, params_rand)
                for row in self.cursor.fetchall():
                    prog = self.get_program(row["id"])
                    if prog:  # id is already not in insp_ids from query
                        inspirations.append(prog)

        # 4. Fallback to global random sampling if not enough inspirations
        # found on island
        if len(inspirations) < n and not enforce_separation:
            needed = n - len(inspirations)
            if needed > 0:
                placeholders_rand = ",".join("?" * len(insp_ids))
                sql_rand = f"""SELECT p.id FROM programs p
                                 JOIN archive a ON p.id = a.program_id
                                 WHERE p.correct = 1
                                 AND p.id NOT IN ({placeholders_rand})
                                 ORDER BY RANDOM() LIMIT ?
                                 """
                params_rand = list(insp_ids) + [needed]
                self.cursor.execute(sql_rand, params_rand)
                for row in self.cursor.fetchall():
                    prog = self.get_program(row["id"])
                    if prog:
                        inspirations.append(prog)

        if inspirations:
            inspiration_details = [
                f"{p.id} (Gen: {p.generation}, "
                f"Score: {p.combined_score or 0.0:.4f}, "
                f"Island: {p.island_idx})"
                for p in inspirations
            ]
            logger.info(
                f"Sampled {len(inspirations)} archive inspirations: "
                f"{inspiration_details}"
            )
        return inspirations


class TopKInspirationSelector(ContextSelectorStrategy):
    """Strategy for selecting top-k inspirations from archive."""

    def sample_context(
        self, parent: Any, excluded_programs: List[Any], k: int
    ) -> List[Any]:
        """
        Get the top-k best performing programs from the archive excluding the parent
        and already selected archive inspirations.
        """
        if k <= 0:
            return []

        enforce_separation = getattr(self.config, "enforce_island_separation", False)
        parent_island_idx = parent.island_idx

        if enforce_separation and parent_island_idx is None:
            logger.debug(
                f"Parent {parent.id} has no island and island separation is enforced, "
                "skipping top-k inspirations."
            )
            return []

        # Build set of IDs to exclude (parent + archive inspirations)
        excluded_ids: Set[str] = {parent.id}
        excluded_ids.update(prog.id for prog in excluded_programs)

        if not hasattr(self.config, "archive_size") or self.config.archive_size <= 0:
            return []

        # Query archive for programs
        placeholders = ",".join("?" * len(excluded_ids))

        if enforce_separation and parent_island_idx is not None:
            # Only search within parent's island
            query = f"""
                SELECT p.*
                FROM programs p
                JOIN archive a ON p.id = a.program_id
                WHERE p.island_idx = ? AND p.id NOT IN ({placeholders}) 
                AND p.correct = 1
            """
            params = [parent_island_idx] + list(excluded_ids)
            search_scope = f"island {parent_island_idx}"
        else:
            # Search globally across all islands
            query = f"""
                SELECT p.*
                FROM programs p
                JOIN archive a ON p.id = a.program_id
                WHERE p.id NOT IN ({placeholders}) 
                AND p.correct = 1
            """
            params = list(excluded_ids)
            search_scope = "all islands"

        self.cursor.execute(query, params)
        archive_rows = self.cursor.fetchall()

        if not archive_rows:
            logger.debug(
                f"No eligible archived programs for top-k selection on {search_scope}."
            )
            return []

        archive_programs = [
            self.program_from_row(row) for row in archive_rows if self.program_from_row
        ]
        archive_programs = [p for p in archive_programs if p]

        if not archive_programs:
            return []

        # Sort by performance - prioritize combined_score, then average metrics
        def sort_key(prog: Any) -> float:
            if prog.combined_score is not None:
                return prog.combined_score
            elif prog.public_metrics:
                return sum(prog.public_metrics.values()) / len(prog.public_metrics)
            else:
                return -float("inf")

        sorted_programs = sorted(archive_programs, key=sort_key, reverse=True)

        # Return top-k programs
        top_k = sorted_programs[:k]

        if top_k:
            inspiration_details = [
                f"{p.id} (Gen: {p.generation}, "
                f"Score: {p.combined_score or 0.0:.4f}, "
                f"Island: {p.island_idx})"
                for p in top_k
            ]
            logger.info(
                f"Selected {len(top_k)} top-k inspirations from archive on "
                f"{search_scope}: {inspiration_details}"
            )

        return top_k


class CombinedContextSelector:
    """Combined context selector that handles both archive inspirations and top-k selection."""

    def __init__(
        self,
        cursor: sqlite3.Cursor,
        conn: sqlite3.Connection,
        config: Any,
        get_program_func: Callable[[str], Any],
        best_program_id: Optional[str] = None,
        get_island_idx_func: Optional[Callable[[str], Optional[int]]] = None,
        program_from_row_func: Optional[Callable[[sqlite3.Row], Any]] = None,
    ):
        self.archive_selector = ArchiveInspirationSelector(
            cursor=cursor,
            conn=conn,
            config=config,
            get_program_func=get_program_func,
            best_program_id=best_program_id,
            get_island_idx_func=get_island_idx_func,
            program_from_row_func=program_from_row_func,
        )
        self.topk_selector = TopKInspirationSelector(
            cursor=cursor,
            conn=conn,
            config=config,
            get_program_func=get_program_func,
            best_program_id=best_program_id,
            get_island_idx_func=get_island_idx_func,
            program_from_row_func=program_from_row_func,
        )

    def sample_context(
        self, parent: Any, num_archive: int, num_topk: int
    ) -> tuple[List[Any], List[Any]]:
        """Sample both archive inspirations and top-k inspirations."""
        archive_inspirations = self.archive_selector.sample_context(parent, num_archive)
        top_k_inspirations = self.topk_selector.sample_context(
            parent, archive_inspirations, num_topk
        )
        return archive_inspirations, top_k_inspirations


class InspirationContextBuilder:
    """
    Builds and orders inspiration programs for prompt construction.

    Combines archive and top-k inspirations and sorts them for optimal
    prompt presentation (e.g., least-to-most by score so the best programs
    appear last and have more influence due to recency bias in LLMs).
    """

    def __init__(
        self,
        sort_order: Literal["ascending", "chronological", "none"] = "ascending",
    ):
        """
        Initialize the context builder.

        Args:
            sort_order: How to sort inspirations:
                - "ascending": by score, worst to best (least-to-most)
                - "chronological": by generation, oldest to newest
                - "none": preserve original order
        """
        self.sort_order = sort_order

    def _get_score(self, prog: Any) -> float:
        """Get the score for sorting. Handles None values."""
        if prog.combined_score is not None:
            return prog.combined_score
        elif prog.public_metrics:
            return sum(prog.public_metrics.values()) / len(prog.public_metrics)
        return -float("inf")

    def _get_generation(self, prog: Any) -> int:
        """Get the generation for sorting. Handles None values."""
        if prog.generation is not None:
            return prog.generation
        return 0

    def build_context(
        self,
        archive_inspirations: List[Any],
        top_k_inspirations: List[Any],
    ) -> List[Any]:
        """
        Combine and sort inspirations for prompt construction.

        Args:
            archive_inspirations: Programs sampled from the archive
            top_k_inspirations: Top-k best programs from the archive

        Returns:
            Combined and sorted list of inspiration programs
        """
        # Combine all inspirations
        all_inspirations = archive_inspirations + top_k_inspirations

        if not all_inspirations:
            return []

        # Remove duplicates while preserving order (in case of overlap)
        seen_ids: Set[str] = set()
        unique_inspirations: List[Any] = []
        for prog in all_inspirations:
            if prog.id not in seen_ids:
                seen_ids.add(prog.id)
                unique_inspirations.append(prog)

        # Sort based on configured order
        if self.sort_order == "ascending":
            # Least-to-most: worst programs first, best last (by score)
            sorted_inspirations = sorted(
                unique_inspirations, key=self._get_score, reverse=False
            )
            sort_values = [f"{self._get_score(p):.4f}" for p in sorted_inspirations]
            sort_label = "scores"
        elif self.sort_order == "chronological":
            # Oldest to newest: by generation
            sorted_inspirations = sorted(
                unique_inspirations, key=self._get_generation, reverse=False
            )
            sort_values = [str(self._get_generation(p)) for p in sorted_inspirations]
            sort_label = "gens"
        else:
            # No sorting - preserve original order
            sorted_inspirations = unique_inspirations
            sort_values = [f"{self._get_score(p):.4f}" for p in sorted_inspirations]
            sort_label = "scores"

        if sorted_inspirations:
            logger.info(
                f"Built context: {len(sorted_inspirations)} programs "
                f"({self.sort_order}, {sort_label}: {sort_values})"
            )

        return sorted_inspirations
