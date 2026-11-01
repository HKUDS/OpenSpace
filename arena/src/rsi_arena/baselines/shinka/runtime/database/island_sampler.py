"""Island sampling strategies for parent selection."""

import logging
import random
import sqlite3
from abc import ABC, abstractmethod
from typing import Any, List
import numpy as np

logger = logging.getLogger(__name__)


class IslandSampler(ABC):
    """Abstract base class for island sampling strategies."""

    def __init__(
        self,
        cursor: sqlite3.Cursor,
        conn: sqlite3.Connection,
        config: Any,
    ):
        self.cursor = cursor
        self.conn = conn
        self.config = config

    @abstractmethod
    def sample_island(self, initialized_islands: List[int]) -> int:
        """Sample an island index from the list of initialized islands.

        Args:
            initialized_islands: List of island indices that have correct programs

        Returns:
            Selected island index
        """
        pass

    def _get_island_program_counts(self, island_indices: List[int]) -> dict[int, int]:
        """Get the number of programs in each island.

        Args:
            island_indices: List of island indices to query

        Returns:
            Dictionary mapping island_idx to program count
        """
        if not island_indices:
            return {}

        placeholders = ",".join("?" * len(island_indices))
        query = f"""
            SELECT island_idx, COUNT(*) as count
            FROM programs
            WHERE island_idx IN ({placeholders}) AND correct = 1
            GROUP BY island_idx
        """
        self.cursor.execute(query, island_indices)

        counts = {island_idx: 0 for island_idx in island_indices}
        for row in self.cursor.fetchall():
            counts[row["island_idx"]] = row["count"]

        return counts

    def _get_island_best_fitness(self, island_indices: List[int]) -> dict[int, float]:
        """Get the best fitness (combined_score) from each island.

        Args:
            island_indices: List of island indices to query

        Returns:
            Dictionary mapping island_idx to best combined_score
        """
        if not island_indices:
            return {}

        placeholders = ",".join("?" * len(island_indices))
        query = f"""
            SELECT island_idx, MAX(combined_score) as best_fitness
            FROM programs
            WHERE island_idx IN ({placeholders}) AND correct = 1
            GROUP BY island_idx
        """
        self.cursor.execute(query, island_indices)

        fitness = {}
        for row in self.cursor.fetchall():
            fitness[row["island_idx"]] = row["best_fitness"]

        return fitness


class UniformIslandSampler(IslandSampler):
    """Uniformly sample from initialized islands (default behavior)."""

    def sample_island(self, initialized_islands: List[int]) -> int:
        """Uniformly sample an island."""
        return random.choice(initialized_islands)


class EqualIslandSampler(IslandSampler):
    """Sample the island with the fewest programs.

    If multiple islands have the same minimum count, sample uniformly among them.
    """

    def sample_island(self, initialized_islands: List[int]) -> int:
        """Sample island with fewest programs."""
        counts = self._get_island_program_counts(initialized_islands)

        min_count = min(counts.values())
        islands_with_min = [
            island_idx for island_idx, count in counts.items() if count == min_count
        ]

        sampled = random.choice(islands_with_min)
        logger.debug(
            f"EqualIslandSampler: Island counts = {counts}, "
            f"min_count = {min_count}, sampled = {sampled}"
        )
        return sampled


class ProportionalIslandSampler(IslandSampler):
    """Sample islands proportional to their best fitness using Boltzmann distribution.

    Uses a medium temperature for the Boltzmann distribution.
    """

    def __init__(
        self,
        cursor: sqlite3.Cursor,
        conn: sqlite3.Connection,
        config: Any,
        temperature: float = 1.0,
    ):
        super().__init__(cursor, conn, config)
        self.temperature = temperature

    def sample_island(self, initialized_islands: List[int]) -> int:
        """Sample island proportional to best fitness."""
        fitness_dict = self._get_island_best_fitness(initialized_islands)

        # Extract fitness values in the same order as initialized_islands
        fitness_values = np.array(
            [fitness_dict.get(island_idx, 0.0) for island_idx in initialized_islands]
        )

        # Apply Boltzmann distribution: exp(fitness / temperature)
        exp_values = np.exp(fitness_values / self.temperature)
        probabilities = exp_values / np.sum(exp_values)

        # Sample according to probabilities
        sampled_idx = np.random.choice(len(initialized_islands), p=probabilities)
        sampled_island = initialized_islands[sampled_idx]

        logger.debug(
            f"ProportionalIslandSampler: fitness = {fitness_dict}, "
            f"probabilities = {probabilities}, sampled = {sampled_island}"
        )
        return sampled_island


class WeightedIslandSampler(IslandSampler):
    """Sample islands considering both program count and fitness.

    More programs -> lower probability
    Higher fitness -> higher probability
    """

    def __init__(
        self,
        cursor: sqlite3.Cursor,
        conn: sqlite3.Connection,
        config: Any,
        fitness_weight: float = 1.0,
        count_weight: float = 1.0,
    ):
        super().__init__(cursor, conn, config)
        self.fitness_weight = fitness_weight
        self.count_weight = count_weight

    def sample_island(self, initialized_islands: List[int]) -> int:
        """Sample island using weighted combination of fitness and inverse count."""
        counts = self._get_island_program_counts(initialized_islands)
        fitness_dict = self._get_island_best_fitness(initialized_islands)

        # Calculate weights for each island
        weights = []
        for island_idx in initialized_islands:
            count = counts.get(island_idx, 1)
            fitness = fitness_dict.get(island_idx, 0.0)

            # Weight = fitness^fitness_weight / count^count_weight
            # More fitness -> higher weight, more programs -> lower weight
            weight = (fitness**self.fitness_weight) / (count**self.count_weight)
            weights.append(weight)

        # Normalize to probabilities
        weights = np.array(weights)
        if np.sum(weights) == 0:
            # Fallback to uniform if all weights are zero
            probabilities = np.ones(len(weights)) / len(weights)
        else:
            probabilities = weights / np.sum(weights)

        # Sample according to probabilities
        sampled_idx = np.random.choice(len(initialized_islands), p=probabilities)
        sampled_island = initialized_islands[sampled_idx]

        logger.debug(
            f"WeightedIslandSampler: counts = {counts}, fitness = {fitness_dict}, "
            f"weights = {weights}, probabilities = {probabilities}, "
            f"sampled = {sampled_island}"
        )
        return sampled_island


def create_island_sampler(
    cursor: sqlite3.Cursor,
    conn: sqlite3.Connection,
    config: Any,
    strategy: str = "uniform",
) -> IslandSampler:
    """Factory function to create island samplers.

    Args:
        cursor: Database cursor
        conn: Database connection
        config: Database configuration
        strategy: Sampling strategy name

    Returns:
        IslandSampler instance

    Raises:
        ValueError: If strategy is unknown
    """
    if strategy == "uniform":
        return UniformIslandSampler(cursor, conn, config)
    elif strategy == "equal":
        return EqualIslandSampler(cursor, conn, config)
    elif strategy == "proportional":
        return ProportionalIslandSampler(cursor, conn, config, temperature=1.0)
    elif strategy == "weighted":
        return WeightedIslandSampler(
            cursor, conn, config, fitness_weight=1.0, count_weight=1.0
        )
    else:
        raise ValueError(
            f"Unknown island sampling strategy: {strategy}. "
            f"Valid options: 'uniform', 'equal', 'proportional', 'weighted'"
        )
