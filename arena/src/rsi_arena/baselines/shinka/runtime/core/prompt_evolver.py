"""
SystemPromptEvolver and SystemPromptSampler for meta-prompt evolution.

This module provides utilities for evolving system prompts using
similar mutation operators as program evolution (diff, full).
"""

import logging
import re
from typing import List, Optional, Tuple
import numpy as np

from rsi_arena.baselines.shinka.runtime.database.prompt_dbase import (
    SystemPromptDatabase,
    SystemPrompt,
    create_system_prompt,
)
from rsi_arena.baselines.shinka.runtime.prompts.prompts_prompt_evo import (
    construct_diff_evolution_prompt,
    construct_full_evolution_prompt,
)

logger = logging.getLogger(__name__)


def _extract_llm_metadata(response) -> dict:
    """
    Extract LLM metadata from a QueryResult response.

    Args:
        response: QueryResult from LLM query

    Returns:
        Dictionary containing LLM metadata for storage
    """
    return {
        "model_name": getattr(response, "model_name", None),
        "input_tokens": getattr(response, "input_tokens", 0),
        "output_tokens": getattr(response, "output_tokens", 0),
        "thinking_tokens": getattr(response, "thinking_tokens", 0),
        "cost": getattr(response, "cost", 0.0),
        "input_cost": getattr(response, "input_cost", 0.0),
        "output_cost": getattr(response, "output_cost", 0.0),
        "llm_kwargs": getattr(response, "kwargs", {}),
        "model_posteriors": getattr(response, "model_posteriors", {}),
        "num_tool_calls": getattr(response, "num_tool_calls", 0),
        "num_total_queries": getattr(response, "num_total_queries", 1),
    }


def _extract_between(
    content: str,
    start: str,
    end: str,
) -> Optional[str]:
    """
    Extract text between start and end tags.

    Args:
        content: The input string containing the tags
        start: The start tag (e.g., "<NAME>")
        end: The end tag (e.g., "</NAME>")

    Returns:
        The extracted text, or None if not found
    """
    pattern = f"{re.escape(start)}\\s*(.*?)\\s*{re.escape(end)}"
    match = re.search(pattern, content, re.DOTALL)
    if match:
        return match.group(1).strip()
    return None


def _parse_prompt_response(
    content: str,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Parse NAME, DESCRIPTION, and PROMPT from LLM response.

    Falls back to using the entire content as prompt text if tags not found.

    Args:
        content: The LLM response content

    Returns:
        Tuple of (name, description, prompt_text)
    """
    name = _extract_between(content, "<NAME>", "</NAME>")
    description = _extract_between(content, "<DESCRIPTION>", "</DESCRIPTION>")
    prompt_text = _extract_between(content, "<PROMPT>", "</PROMPT>")

    # Fallback: if PROMPT tag not found, use entire content (backwards compat)
    if prompt_text is None:
        logger.warning("No <PROMPT> tag found in response, using raw content")
        prompt_text = content.strip()

    return name, description, prompt_text


class SystemPromptSampler:
    """
    Samples system prompts from the archive using UCB with epsilon-greedy exploration.

    UCB balances exploitation (high-fitness prompts) with exploration
    (under-sampled prompts) using the formula:
        UCB = avg_improvement + c * sqrt(log(N) / n)

    The exploration constant (c) controls exploration vs exploitation:
    - Higher c: more exploration of under-sampled prompts
    - Lower c: more exploitation of high-fitness prompts

    Epsilon-greedy adds random uniform sampling with probability epsilon,
    ensuring diverse exploration across the full archive.
    """

    def __init__(
        self,
        prompt_db: SystemPromptDatabase,
        exploration_constant: float = 1.0,
        min_programs_for_fitness: int = 3,
        epsilon: float = 0.1,
    ):
        """
        Initialize the SystemPromptSampler.

        Args:
            prompt_db: The SystemPromptDatabase to sample from
            exploration_constant: UCB exploration constant (c)
            min_programs_for_fitness: Min correct programs before using fitness
            epsilon: Probability of random uniform sampling (0.0 to 1.0)
        """
        self.prompt_db = prompt_db
        self.exploration_constant = exploration_constant
        self.min_programs_for_fitness = min_programs_for_fitness
        self.epsilon = epsilon

    def sample(self) -> Optional[SystemPrompt]:
        """
        Sample a prompt from the archive using UCB with epsilon-greedy.

        Returns:
            A sampled SystemPrompt, or None if archive is empty
        """
        return self.prompt_db.sample(
            exploration_constant=self.exploration_constant,
            epsilon=self.epsilon,
        )

    def get_best_prompt(self) -> Optional[SystemPrompt]:
        """
        Get the best performing prompt from the database.

        Returns:
            The best SystemPrompt by fitness, or None if no prompts exist
        """
        return self.prompt_db.get_best_prompt()

    def get_archive(self) -> List[SystemPrompt]:
        """
        Get all prompts in the archive.

        Returns:
            List of SystemPrompt objects in the archive
        """
        return self.prompt_db.get_archive()


class SystemPromptEvolver:
    """
    Evolves system prompts using LLM-based mutation operators.

    Supports three mutation types:
    - diff: Targeted modifications to specific parts of the prompt
    - full: Complete rewrite of the prompt
    """

    def __init__(
        self,
        llm_client,
        patch_types: Optional[List[str]] = None,
        patch_type_probs: Optional[List[float]] = None,
        llm_kwargs: Optional[dict] = None,
    ):
        """
        Initialize the SystemPromptEvolver.

        Args:
            llm_client: LLM client for generating prompt mutations
            patch_types: List of mutation types (default: ["diff", "full"])
            patch_type_probs: Probabilities for each patch type
            llm_kwargs: Additional kwargs for LLM queries
        """
        self.llm_client = llm_client

        for p in patch_types:
            if p not in ["diff", "full"]:
                raise ValueError(f"Invalid patch type: {p}")
        if patch_types is None:
            patch_types = ["diff", "full"]
        if patch_type_probs is None:
            patch_type_probs = [0.7, 0.3]

        # Normalize probabilities
        prob_sum = sum(patch_type_probs)
        if not np.isclose(prob_sum, 1.0, atol=1e-6):
            patch_type_probs = [p / prob_sum for p in patch_type_probs]
            logger.warning("Prompt evolution probabilities normalized to sum to 1.0")

        self.patch_types = patch_types
        self.patch_type_probs = patch_type_probs
        self.llm_kwargs = llm_kwargs or {}

    def evolve(
        self,
        parent_prompt: SystemPrompt,
        next_generation: int,
        program_generation: int = 0,
        top_programs: Optional[List] = None,
        language: str = "python",
        include_text_feedback: bool = False,
        global_scratchpad: Optional[str] = None,
    ) -> Tuple[Optional[SystemPrompt], str, float]:
        """
        Generate a new prompt by evolving the parent.

        Args:
            parent_prompt: The prompt to evolve
            next_generation: The next chronological prompt generation number
            program_generation: Current program generation (for tracking)
            top_programs: Top-k performing programs to use as context
            language: Programming language for code examples
            include_text_feedback: Whether to include text feedback in context
            global_scratchpad: Global insights from meta-reviewing

        Returns:
            Tuple of (new_prompt, patch_type, api_cost)
            Returns (None, patch_type, cost) if evolution fails
        """
        # Sample patch type
        patch_type = self._sample_patch_type()

        num_programs = len(top_programs) if top_programs else 0
        has_scratchpad = global_scratchpad is not None and len(global_scratchpad) > 0
        logger.info(
            f"Evolving prompt {parent_prompt.id[:8]}... using {patch_type} mutation "
            f"with {num_programs} top programs as context"
            + (", with global scratchpad" if has_scratchpad else "")
        )

        # Generate mutation based on patch type
        # Both diff and full use the same context now
        if patch_type == "diff":
            result = self._diff_mutate(
                parent_prompt,
                top_programs,
                language,
                include_text_feedback,
                global_scratchpad,
            )
        elif patch_type == "full":
            result = self._full_rewrite(
                parent_prompt,
                top_programs,
                language,
                include_text_feedback,
                global_scratchpad,
            )
        else:
            logger.error(f"Unknown patch type: {patch_type}")
            return None, patch_type, 0.0

        new_text, name, description, cost, llm_metadata = result

        if not new_text:
            logger.warning(f"Prompt evolution failed for {patch_type}")
            return None, patch_type, cost

        # Build metadata with parent info and LLM generation data
        metadata = {
            "parent_fitness": parent_prompt.fitness,
            "parent_program_count": parent_prompt.program_count,
            "parent_generation": parent_prompt.generation,
            "num_context_programs": num_programs,
        }

        # Add LLM metadata if available
        if llm_metadata:
            metadata["llm"] = llm_metadata

        # Create new SystemPrompt with full metadata
        new_prompt = create_system_prompt(
            prompt_text=new_text,
            parent_id=parent_prompt.id,
            generation=next_generation,
            program_generation=program_generation,
            patch_type=patch_type,
            metadata=metadata,
            name=name,
            description=description,
        )

        logger.info(
            f"Created new prompt {new_prompt.id[:8]}... "
            f"(gen={new_prompt.generation}, prog_gen={program_generation}, patch={patch_type}"
            + (f", name={name}" if name else "")
            + ")"
        )

        return new_prompt, patch_type, cost

    def _sample_patch_type(self) -> str:
        """Sample a patch type."""
        return np.random.choice(self.patch_types, p=self.patch_type_probs)

    def _diff_mutate(
        self,
        parent_prompt: SystemPrompt,
        top_programs: Optional[List] = None,
        language: str = "python",
        include_text_feedback: bool = False,
        global_scratchpad: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str], Optional[str], float, Optional[dict]]:
        """
        Apply diff-style mutation to the prompt.

        Makes targeted modifications to specific parts of the prompt.

        Returns:
            Tuple of (prompt_text, name, description, cost, llm_metadata)
        """
        system_msg, user_msg = construct_diff_evolution_prompt(
            parent_prompt,
            top_programs,
            language,
            include_text_feedback,
            global_scratchpad,
        )

        try:
            response = self.llm_client.query(
                msg=user_msg,
                system_msg=system_msg,
                llm_kwargs=self.llm_kwargs,
            )

            if response is None or not response.content:
                logger.warning("Empty response from LLM for diff mutation")
                return None, None, None, 0.0, None

            cost = response.cost or 0.0

            # Extract LLM metadata from response
            llm_metadata = _extract_llm_metadata(response)

            # Parse NAME, DESCRIPTION, and PROMPT from response
            parsed = _parse_prompt_response(response.content)
            name, description, new_text = parsed

            # Basic validation
            if not new_text or len(new_text) < 50:
                logger.warning(
                    f"Generated prompt too short "
                    f"({len(new_text) if new_text else 0} chars)"
                )
                return None, None, None, cost, llm_metadata

            logger.debug(f"Diff mutation: {len(new_text)} char prompt")
            if name:
                logger.debug(f"Prompt name: {name}")
            return new_text, name, description, cost, llm_metadata

        except Exception as e:
            logger.error(f"Error in diff mutation: {e}")
            return None, None, None, 0.0, None

    def _full_rewrite(
        self,
        parent_prompt: SystemPrompt,
        top_programs: Optional[List] = None,
        language: str = "python",
        include_text_feedback: bool = False,
        global_scratchpad: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str], Optional[str], float, Optional[dict]]:
        """
        Apply full rewrite mutation to the prompt.

        Completely rewrites the prompt while preserving successful patterns.

        Returns:
            Tuple of (prompt_text, name, description, cost, llm_metadata)
        """
        system_msg, user_msg = construct_full_evolution_prompt(
            parent_prompt,
            top_programs,
            language,
            include_text_feedback,
            global_scratchpad,
        )

        try:
            response = self.llm_client.query(
                msg=user_msg,
                system_msg=system_msg,
                llm_kwargs=self.llm_kwargs,
            )

            if response is None or not response.content:
                logger.warning("Empty response from LLM for full rewrite")
                return None, None, None, 0.0, None

            cost = response.cost or 0.0

            # Extract LLM metadata from response
            llm_metadata = _extract_llm_metadata(response)

            # Parse NAME, DESCRIPTION, and PROMPT from response
            parsed = _parse_prompt_response(response.content)
            name, description, new_text = parsed

            # Basic validation
            if not new_text or len(new_text) < 50:
                logger.warning(
                    f"Generated prompt too short "
                    f"({len(new_text) if new_text else 0} chars)"
                )
                return None, None, None, cost, llm_metadata

            logger.debug(f"Full rewrite generated {len(new_text)} char prompt")
            if name:
                logger.debug(f"Prompt name: {name}")
            return new_text, name, description, cost, llm_metadata

        except Exception as e:
            logger.error(f"Error in full rewrite: {e}")
            return None, None, None, 0.0, None


class AsyncSystemPromptEvolver:
    """
    Async version of SystemPromptEvolver for use with ShinkaEvolveRunner.
    """

    def __init__(
        self,
        llm_client,  # AsyncLLMClient
        patch_types: Optional[List[str]] = None,
        patch_type_probs: Optional[List[float]] = None,
        llm_kwargs: Optional[dict] = None,
    ):
        """
        Initialize the AsyncSystemPromptEvolver.

        Args:
            llm_client: Async LLM client for generating prompt mutations
            patch_types: List of mutation types to use
            patch_type_probs: Probabilities for each patch type
            llm_kwargs: Additional kwargs for LLM queries
        """
        self.llm_client = llm_client

        if patch_types is None:
            patch_types = ["diff", "full"]
        if patch_type_probs is None:
            patch_type_probs = [0.7, 0.3]

        # Normalize probabilities
        prob_sum = sum(patch_type_probs)
        if not np.isclose(prob_sum, 1.0, atol=1e-6):
            patch_type_probs = [p / prob_sum for p in patch_type_probs]

        self.patch_types = patch_types
        self.patch_type_probs = patch_type_probs
        self.llm_kwargs = llm_kwargs or {}

    async def evolve(
        self,
        parent_prompt: SystemPrompt,
        next_generation: int,
        program_generation: int = 0,
        top_programs: Optional[List] = None,
        language: str = "python",
        include_text_feedback: bool = False,
        global_scratchpad: Optional[str] = None,
    ) -> Tuple[Optional[SystemPrompt], str, float]:
        """
        Async version of evolve.

        Args:
            parent_prompt: The prompt to evolve
            next_generation: The next chronological prompt generation number
            program_generation: Current program generation (for tracking)
            top_programs: Top-k performing programs to use as context
            language: Programming language for code examples
            include_text_feedback: Whether to include text feedback in context
            global_scratchpad: Global insights from meta-reviewing

        Returns:
            Tuple of (new_prompt, patch_type, api_cost)
        """
        # Sample patch type
        patch_type = self._sample_patch_type()

        num_programs = len(top_programs) if top_programs else 0
        has_scratchpad = global_scratchpad is not None and len(global_scratchpad) > 0
        logger.info(
            f"[Async] Evolving prompt {parent_prompt.id[:8]}... "
            f"using {patch_type} mutation with {num_programs} top programs"
            + (", with global scratchpad" if has_scratchpad else "")
        )

        # Generate mutation based on patch type
        # Both diff and full use the same context now
        if patch_type == "diff":
            result = await self._diff_mutate_async(
                parent_prompt,
                top_programs,
                language,
                include_text_feedback,
                global_scratchpad,
            )
        elif patch_type == "full":
            result = await self._full_rewrite_async(
                parent_prompt,
                top_programs,
                language,
                include_text_feedback,
                global_scratchpad,
            )
        else:
            logger.error(f"Unknown patch type: {patch_type}")
            return None, patch_type, 0.0

        new_text, name, description, cost, llm_metadata = result

        if not new_text:
            logger.warning(f"Async prompt evolution failed for {patch_type}")
            return None, patch_type, cost

        # Build metadata with parent info and LLM generation data
        metadata = {
            "parent_fitness": parent_prompt.fitness,
            "parent_program_count": parent_prompt.program_count,
            "parent_generation": parent_prompt.generation,
            "num_context_programs": num_programs,
        }

        # Add LLM metadata if available
        if llm_metadata:
            metadata["llm"] = llm_metadata

        # Create new SystemPrompt with full metadata
        new_prompt = create_system_prompt(
            prompt_text=new_text,
            parent_id=parent_prompt.id,
            generation=next_generation,
            program_generation=program_generation,
            patch_type=patch_type,
            metadata=metadata,
            name=name,
            description=description,
        )

        logger.info(
            f"[Async] Created new prompt {new_prompt.id[:8]}... "
            f"(gen={new_prompt.generation}, prog_gen={program_generation}, patch={patch_type}"
            + (f", name={name}" if name else "")
            + ")"
        )

        return new_prompt, patch_type, cost

    def _sample_patch_type(self) -> str:
        """Sample a patch type."""
        return np.random.choice(self.patch_types, p=self.patch_type_probs)

    async def _diff_mutate_async(
        self,
        parent_prompt: SystemPrompt,
        top_programs: Optional[List] = None,
        language: str = "python",
        include_text_feedback: bool = False,
        global_scratchpad: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str], Optional[str], float, Optional[dict]]:
        """Async diff mutation."""
        system_msg, user_msg = construct_diff_evolution_prompt(
            parent_prompt,
            top_programs,
            language,
            include_text_feedback,
            global_scratchpad,
        )

        try:
            # Pass None if llm_kwargs is empty to let client sample params
            kwargs = self.llm_kwargs if self.llm_kwargs else None
            response = await self.llm_client.query(
                msg=user_msg,
                system_msg=system_msg,
                llm_kwargs=kwargs,
            )

            if response is None or not response.content:
                return None, None, None, 0.0, None

            cost = response.cost or 0.0

            # Extract LLM metadata from response
            llm_metadata = _extract_llm_metadata(response)

            # Parse NAME, DESCRIPTION, and PROMPT from response
            parsed = _parse_prompt_response(response.content)
            name, description, new_text = parsed

            if not new_text or len(new_text) < 50:
                return None, None, None, cost, llm_metadata

            return new_text, name, description, cost, llm_metadata

        except Exception as e:
            logger.error(f"Error in async diff mutation: {e}")
            return None, None, None, 0.0, None

    async def _full_rewrite_async(
        self,
        parent_prompt: SystemPrompt,
        top_programs: Optional[List] = None,
        language: str = "python",
        include_text_feedback: bool = False,
        global_scratchpad: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str], Optional[str], float, Optional[dict]]:
        """Async full rewrite mutation."""
        system_msg, user_msg = construct_full_evolution_prompt(
            parent_prompt,
            top_programs,
            language,
            include_text_feedback,
            global_scratchpad,
        )

        try:
            # Pass None if llm_kwargs is empty to let client sample params
            kwargs = self.llm_kwargs if self.llm_kwargs else None
            response = await self.llm_client.query(
                msg=user_msg,
                system_msg=system_msg,
                llm_kwargs=kwargs,
            )

            if response is None or not response.content:
                return None, None, None, 0.0, None

            cost = response.cost or 0.0

            # Extract LLM metadata from response
            llm_metadata = _extract_llm_metadata(response)

            # Parse NAME, DESCRIPTION, and PROMPT from response
            parsed = _parse_prompt_response(response.content)
            name, description, new_text = parsed

            if not new_text or len(new_text) < 50:
                return None, None, None, cost, llm_metadata

            return new_text, name, description, cost, llm_metadata

        except Exception as e:
            logger.error(f"Error in async full rewrite: {e}")
            return None, None, None, 0.0, None
