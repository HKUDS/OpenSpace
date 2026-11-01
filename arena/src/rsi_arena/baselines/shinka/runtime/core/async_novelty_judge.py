"""
Async novelty judge for concurrent novelty assessment.
Provides non-blocking novelty checking with concurrent LLM calls.
"""

import asyncio
import logging
from typing import List, Optional, Dict, Any, Tuple
from .novelty_judge import NoveltyJudge
from ..llm import AsyncLLMClient
from ..database import Program

logger = logging.getLogger(__name__)


class AsyncNoveltyJudge:
    """Async version of NoveltyJudge for concurrent novelty assessment."""

    def __init__(
        self,
        sync_novelty_judge: NoveltyJudge,
        async_llm_client: Optional[AsyncLLMClient] = None,
    ):
        """Initialize with existing sync novelty judge.

        Args:
            sync_novelty_judge: The synchronous NoveltyJudge instance
            async_llm_client: Optional async LLM client for novelty checks
        """
        self.sync_judge = sync_novelty_judge
        self.async_llm_client = async_llm_client

    async def should_check_novelty_async(
        self, code_embedding: List[float], current_gen: int, parent_program: Program, db
    ) -> bool:
        """Async version of should_check_novelty.

        Since this involves database access, we handle it in the main thread to avoid
        SQLite threading issues.
        """
        try:
            # Check basic conditions without database access
            if not code_embedding or current_gen == 0 or not parent_program:
                return False

            # Check if parent program has island information and islands are initialized
            # This needs to be done in main thread due to SQLite threading restrictions
            if (
                parent_program.island_idx is not None
                and hasattr(db, "island_manager")
                and db.island_manager is not None
                and hasattr(db.island_manager, "are_all_islands_initialized")
                and db.island_manager.are_all_islands_initialized()
            ):
                return True

            return False
        except Exception as e:
            logger.error(f"Error in async should_check_novelty: {e}")
            return False

    async def assess_novelty_with_rejection_sampling_async(
        self,
        exec_fname: str,
        code_embedding: List[float],
        parent_program: Program,
        db,
    ) -> Tuple[bool, Dict[str, Any]]:
        """Async version of novelty assessment matching sync runner logic.

        Args:
            exec_fname: Path to executable file
            code_embedding: Code embedding vector
            parent_program: Parent program
            db: Database instance

        Returns:
            Tuple of (should_accept, novelty_metadata)
        """
        novelty_metadata = {
            "novelty_checks_performed": 0,
            "novelty_total_cost": 0.0,
            "novelty_explanation": "",
            "max_similarity": 0.0,
            "similarity_scores": [],
        }

        try:
            # Compute similarities with programs in island (same as sync version)
            if parent_program.island_idx is None:
                return True, novelty_metadata

            loop = asyncio.get_event_loop()
            similarity_scores = await loop.run_in_executor(
                None,
                db.compute_similarity_thread_safe,
                code_embedding,
                parent_program.island_idx,
            )

            if not similarity_scores:
                logger.info(
                    "NOVELTY CHECK: Accepting program due to no similarity scores."
                )
                novelty_metadata["similarity_scores"] = []
                return True, novelty_metadata

            max_similarity = max(similarity_scores)
            sorted_similarity_scores = sorted(similarity_scores, reverse=True)
            formatted_similarities = [f"{s:.2f}" for s in sorted_similarity_scores]

            logger.info(f"Top-5 similarity scores: {formatted_similarities[:5]}")

            novelty_metadata["max_similarity"] = max_similarity
            novelty_metadata["similarity_scores"] = similarity_scores

            # First check: embedding similarity threshold (same as sync version)
            if max_similarity <= self.sync_judge.similarity_threshold:
                logger.info(
                    f"NOVELTY CHECK: Accepting program due to low similarity "
                    f"({max_similarity:.3f} <= {self.sync_judge.similarity_threshold})"
                )
                return True, novelty_metadata

            # High similarity detected - check with LLM if configured (same as sync version)
            should_reject = True
            novelty_cost = 0.0

            if self.async_llm_client is not None:
                # Get the most similar program for LLM comparison (thread-safe)
                loop = asyncio.get_event_loop()
                most_similar_program = await loop.run_in_executor(
                    None,
                    db.get_most_similar_program_thread_safe,
                    code_embedding,
                    parent_program.island_idx,
                )

                if most_similar_program:
                    try:
                        # Read the current proposed code
                        loop = asyncio.get_event_loop()
                        proposed_code = await loop.run_in_executor(
                            None, self._read_code_file, exec_fname
                        )

                        if proposed_code:
                            (
                                is_novel,
                                explanation,
                                cost,
                            ) = await self._check_llm_novelty_async(
                                proposed_code, most_similar_program
                            )
                            should_reject = not is_novel
                            novelty_cost = cost
                            novelty_metadata["novelty_checks_performed"] = 1
                            novelty_metadata["novelty_total_cost"] = cost
                            novelty_metadata["novelty_explanation"] = explanation
                    except Exception as e:
                        logger.warning(f"Error reading code for novelty check: {e}")
                        should_reject = True  # Default to rejection on error

            if should_reject:
                logger.info(
                    f"NOVELTY CHECK: Rejecting program due to high similarity "
                    f"({max_similarity:.3f} > {self.sync_judge.similarity_threshold})"
                    + (
                        f" and LLM novelty check (cost: {novelty_cost:.4f})"
                        if novelty_cost > 0
                        else ""
                    )
                )
                return False, novelty_metadata
            else:
                logger.info(
                    f"NOVELTY CHECK: Accepting program despite high similarity "
                    f"({max_similarity:.3f} > {self.sync_judge.similarity_threshold}) "
                    f"due to LLM novelty check (cost: {novelty_cost:.4f})."
                )
                return True, novelty_metadata

        except Exception as e:
            logger.error(f"Error in async novelty assessment: {e}")
            return True, {"novelty_checks_performed": 0, "novelty_total_cost": 0.0}

    async def _check_llm_novelty_async(
        self, proposed_code: str, most_similar_program: Program
    ) -> Tuple[bool, str, float]:
        """
        Async version of LLM novelty check matching sync runner logic.

        Args:
            proposed_code: The newly generated code
            most_similar_program: The most similar existing program

        Returns:
            Tuple of (is_novel, explanation, api_cost)
        """
        if not self.async_llm_client:
            logger.warning("Novelty LLM not configured, skipping novelty check")
            return True, "No novelty LLM configured", 0.0

        # Import novelty prompts (same as sync version)
        from ..prompts import NOVELTY_SYSTEM_MSG, NOVELTY_USER_MSG

        user_msg = NOVELTY_USER_MSG.format(
            language=self.sync_judge.language,
            existing_code=most_similar_program.code,
            proposed_code=proposed_code,
        )

        try:
            response = await self.async_llm_client.query(
                msg=user_msg,
                system_msg=NOVELTY_SYSTEM_MSG,
            )

            if response is None or response.content is None:
                logger.warning("Novelty LLM returned empty response")
                return True, "LLM response was empty", 0.0

            content = response.content.strip()
            api_cost = response.cost or 0.0

            # Parse the response (same as sync version)
            is_novel = content.upper().startswith(
                "NOVEL"
            ) or content.upper().startswith("**NOVEL**")
            explanation = content
            return is_novel, explanation, api_cost

        except Exception as e:
            logger.error(f"Error in novelty LLM check: {e}")
            return True, f"Error in novelty check: {e}", 0.0

    async def _single_novelty_check_async(
        self, code_content: str, similar_program: Program, parent_program: Program
    ) -> Tuple[bool, float, str]:
        """Perform a single async novelty check against a similar program.

        Returns:
            Tuple of (is_novel, cost, explanation)
        """
        try:
            # Construct novelty prompt
            novelty_prompt = self._construct_novelty_prompt(
                code_content, similar_program, parent_program
            )

            # Query LLM asynchronously
            response = await self.async_llm_client.query(
                msg=novelty_prompt,
                system_msg="You are a code novelty assessor. Determine if the new code is sufficiently different from the existing code.",
            )

            if not response or not response.content:
                return True, 0.0, "No response from LLM"

            # Parse response for novelty decision
            is_novel = self._parse_novelty_response(response.content)
            cost = response.cost if hasattr(response, "cost") else 0.0

            return is_novel, cost, response.content[:200]  # Truncate explanation

        except Exception as e:
            logger.warning(f"Single novelty check failed: {e}")
            return True, 0.0, f"Error: {str(e)}"

    def _read_code_file(self, exec_fname: str) -> Optional[str]:
        """Read code file content."""
        try:
            with open(exec_fname, "r") as f:
                return f.read()
        except Exception as e:
            logger.warning(f"Failed to read code file {exec_fname}: {e}")
            return None

    def log_novelty_skip_message(self, reason: str):
        """Log novelty skip message."""
        self.sync_judge.log_novelty_skip_message(reason)

    # Delegate other methods to sync judge
    def __getattr__(self, name):
        """Delegate unknown methods to sync novelty judge."""
        return getattr(self.sync_judge, name)

