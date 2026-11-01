"""
Async meta summarizer for concurrent meta-analysis.
Provides non-blocking meta summarization with concurrent LLM calls.
"""

import asyncio
import logging
from typing import List, Optional, Tuple
from pathlib import Path
from .summarizer import MetaSummarizer
from ..llm import AsyncLLMClient
from ..database import Program
from ..prompts import (
    construct_individual_program_msg,
    META_STEP1_SYSTEM_MSG,
    META_STEP1_USER_MSG,
    META_STEP2_SYSTEM_MSG,
    META_STEP2_USER_MSG,
    META_STEP3_SYSTEM_MSG,
    META_STEP3_USER_MSG,
)

logger = logging.getLogger(__name__)


class AsyncMetaSummarizer:
    """Async version of MetaSummarizer for concurrent meta-analysis."""

    def __init__(
        self,
        sync_summarizer: MetaSummarizer,
        async_llm_client: Optional[AsyncLLMClient] = None,
    ):
        """Initialize with existing sync summarizer.

        Args:
            sync_summarizer: The synchronous MetaSummarizer instance
            async_llm_client: Optional async LLM client for meta analysis
        """
        self.sync_summarizer = sync_summarizer
        self.async_llm_client = async_llm_client

    async def update_meta_memory_async(
        self, best_program: Optional[Program] = None
    ) -> Tuple[Optional[str], float]:
        """
        Async version of update_meta_memory.
        Perform 3-step meta-analysis and update internal state.
        Returns tuple of (updated_recommendations, total_cost) or
        (None, 0.0) if no update occurred.
        """
        if not self.async_llm_client:
            logger.warning("No async meta LLM client configured")
            return None, 0.0

        # Use recently evaluated programs for memory scratchpad
        # Make a copy to avoid issues if the list is modified during processing
        programs_to_analyze = (
            self.sync_summarizer.evaluated_since_last_meta.copy()
            if self.sync_summarizer.evaluated_since_last_meta
            else []
        )

        if len(programs_to_analyze) == 0:
            logger.info("No programs evaluated since last meta query, skipping")
            return None, 0.0

        total_meta_cost = 0.0

        try:
            # Step 1: Create individual program summaries
            (
                individual_summaries,
                step1_cost,
            ) = await self._step1_individual_summaries_async(programs_to_analyze)
            total_meta_cost += step1_cost
            if not individual_summaries:
                logger.error("Step 1 failed - no individual summaries generated")
                return None, total_meta_cost

            # Step 2: Generate global insights scratchpad
            global_insights, step2_cost = await self._step2_global_insights_async(
                individual_summaries, best_program
            )
            total_meta_cost += step2_cost
            if not global_insights:
                logger.error("Step 2 failed - no global insights generated")
                return None, total_meta_cost

            # Step 3: Generate recommendations based on insights
            (
                recommendations,
                step3_cost,
            ) = await self._step3_generate_recommendations_async(
                global_insights, best_program
            )
            total_meta_cost += step3_cost
            if not recommendations:
                logger.error("Step 3 failed - no recommendations generated")
                return None, total_meta_cost

            # Update internal state of sync summarizer
            # Concatenate new individual summaries to existing ones
            if self.sync_summarizer.meta_summary:
                self.sync_summarizer.meta_summary += "\n\n" + individual_summaries
            else:
                self.sync_summarizer.meta_summary = individual_summaries

            self.sync_summarizer.meta_scratch_pad = global_insights
            self.sync_summarizer.meta_recommendations = recommendations

            # Store the newly generated recommendations in history immediately
            if recommendations and isinstance(recommendations, str):
                self.sync_summarizer.meta_recommendations_history.append(
                    recommendations
                )
                logger.debug(
                    f"Added new recommendations to history "
                    f"(total: {len(self.sync_summarizer.meta_recommendations_history)})"
                )

            logger.info(
                f"==> Meta-analysis completed successfully with 3-step process (total cost: ${total_meta_cost:.4f})"
            )
        except Exception as e:
            logger.error(f"Failed to complete 3-step meta-analysis: {e}")
            return None, total_meta_cost

        # Clear the evaluated programs list immediately after processing
        # This ensures that only programs added AFTER this meta update
        # will be saved as "unprocessed" programs
        num_processed = len(self.sync_summarizer.evaluated_since_last_meta)
        self.sync_summarizer.total_programs_processed += num_processed
        self.sync_summarizer.evaluated_since_last_meta = []
        logger.info(
            f"Processed and cleared {num_processed} programs from meta memory "
            f"(total processed: {self.sync_summarizer.total_programs_processed})"
        )

        return (
            (
                self.sync_summarizer.meta_recommendations
                if isinstance(self.sync_summarizer.meta_recommendations, str)
                else None
            ),
            total_meta_cost,
        )

    async def _step1_individual_summaries_async(
        self, programs_to_analyze: List[Program]
    ) -> Tuple[Optional[str], float]:
        """Async version of Step 1: Create individual summaries for each program using batch queries."""
        if not programs_to_analyze:
            logger.warning("No programs to analyze in Step 1")
            return None, 0.0

        # Create individual program messages for batch processing
        user_messages, generation_ids, patch_names, correct_programs = [], [], [], []
        for program in programs_to_analyze:
            individual_program_msg = construct_individual_program_msg(
                program,
                language=self.sync_summarizer.language,
                include_text_feedback=self.sync_summarizer.use_text_feedback,
            )
            generation_ids.append(program.generation)
            patch_names.append(program.metadata["patch_name"])
            correct_programs.append(program.correct)
            user_msg = META_STEP1_USER_MSG.replace(
                "{individual_program_msg}", individual_program_msg
            )
            user_messages.append(user_msg)

        # Use async batch query to process all programs
        num_programs = len(programs_to_analyze)
        logger.info(
            f"==> Step 1 - Processing {num_programs} programs with async batch query"
        )
        responses = await self.async_llm_client.batch_kwargs_query(
            num_samples=num_programs,
            msg=user_messages,
            system_msg=META_STEP1_SYSTEM_MSG,
        )

        if not responses:
            logger.error("Step 1: Failed to get responses from async meta LLM client")
            return None, 0.0

        # Filter out None responses and combine summaries
        valid_responses = [r for r in responses if r is not None]
        if not valid_responses:
            logger.error("Step 1: All batch responses were None")
            return None, 0.0

        # Combine all individual summaries
        combined_summaries = []
        total_cost = 0.0
        for i, response in enumerate(valid_responses):
            if response and response.content:
                program_summary = response.content.strip()
                program_summary += "\n**Program Identifier:** "
                program_summary += f"Generation {generation_ids[i]} - Patch Name {patch_names[i]} - Correct Program: {correct_programs[i]}"
                combined_summaries.append(program_summary)
                total_cost += response.cost or 0.0
            else:
                logger.warning(f"Step 1: Empty response for program {i}")

        # Sort combined_summaries by generation (using generation_ids)
        # Zip together summaries and their generation, sort, then extract summaries
        summaries_with_gen = list(zip(generation_ids, combined_summaries))
        summaries_with_gen.sort(key=lambda x: x[0])
        combined_summaries = [summary for _, summary in summaries_with_gen]

        if not combined_summaries:
            logger.error("Step 1: No valid summaries generated")
            return None, total_cost

        # Join all summaries with double newlines
        final_summary = "\n\n".join(combined_summaries)
        logger.info(
            f"==> Step 1 - {len(combined_summaries)}/{num_programs} "
            f"individual summaries generated (cost: ${total_cost:.4f})"
        )
        return final_summary, total_cost

    async def _step2_global_insights_async(
        self, individual_summaries: str, best_program: Optional[Program] = None
    ) -> Tuple[Optional[str], float]:
        """Async version of Step 2: Generate global insights from individual summaries."""
        previous_insights = (
            self.sync_summarizer.meta_scratch_pad or "*No previous insights available.*"
        )

        # Format best program information
        if best_program:
            best_program_info = construct_individual_program_msg(
                best_program,
                language=self.sync_summarizer.language,
                include_text_feedback=self.sync_summarizer.use_text_feedback,
            )
        else:
            best_program_info = "*No best program information available.*"

        user_msg = (
            META_STEP2_USER_MSG.replace("{individual_summaries}", individual_summaries)
            .replace("{previous_insights}", previous_insights)
            .replace("{best_program_info}", best_program_info)
        )

        response = await self.async_llm_client.query(
            msg=user_msg,
            system_msg=META_STEP2_SYSTEM_MSG,
        )

        if response is None:
            logger.error("Step 2: Failed to get response from async meta LLM client")
            return None, 0.0

        cost = response.cost or 0.0
        logger.info(f"==> Step 2 - Global insights generated (cost: ${cost:.4f})")
        return response.content.strip(), cost

    async def _step3_generate_recommendations_async(
        self, global_insights: str, best_program: Optional[Program] = None
    ) -> Tuple[Optional[str], float]:
        """Async version of Step 3: Generate recommendations based on global insights."""
        previous_recommendations = (
            self.sync_summarizer.meta_recommendations
            or "*No previous recommendations available.*"
        )

        # Format best program information
        if best_program:
            best_program_info = construct_individual_program_msg(
                best_program,
                language=self.sync_summarizer.language,
                include_text_feedback=self.sync_summarizer.use_text_feedback,
            )
        else:
            best_program_info = "*No best program information available.*"

        user_msg = (
            META_STEP3_USER_MSG.replace("{global_insights}", global_insights)
            .replace("{previous_recommendations}", previous_recommendations)
            .replace(
                "{max_recommendations}", str(self.sync_summarizer.max_recommendations)
            )
            .replace("{best_program_info}", best_program_info)
        )

        response = await self.async_llm_client.query(
            msg=user_msg,
            system_msg=META_STEP3_SYSTEM_MSG,
        )

        if response is None:
            logger.error("Step 3: Failed to get response from async meta LLM client")
            return None, 0.0

        cost = response.cost or 0.0
        logger.info(f"==> Step 3 - Recommendations generated (cost: ${cost:.4f})")
        return response.content.strip(), cost

    async def perform_final_summary_async(
        self,
        results_dir: str,
        best_program: Optional[Program] = None,
        db_config=None,
    ) -> tuple[bool, float]:
        """Async version of perform_final_summary.

        Returns:
            Tuple of (success, meta_cost)
        """
        if not self.async_llm_client:
            logger.info("No async meta LLM client configured, skipping final summary")
            return False, 0.0

        unprocessed_count = len(self.sync_summarizer.evaluated_since_last_meta)
        if unprocessed_count == 0:
            logger.info("No unprocessed programs for final summary")
            return False, 0.0

        logger.info(
            f"Performing final meta summary for {unprocessed_count} "
            f"remaining programs..."
        )

        updated_recs, meta_cost = await self.update_meta_memory_async(best_program)
        if updated_recs:
            await self.write_meta_output_async(results_dir)
            logger.info(f"Final meta summary completed (cost: ${meta_cost:.4f})")

            # Store the final meta cost in the best program's metadata
            if meta_cost > 0 and best_program and db_config:
                try:
                    import json

                    def update_metadata():
                        from rsi_arena.baselines.shinka.runtime.database import (
                            ProgramDatabase,
                        )

                        thread_db = ProgramDatabase(db_config)
                        try:
                            if best_program.metadata is None:
                                best_program.metadata = {}

                            # Accumulate meta_cost if it already exists
                            existing_meta_cost = best_program.metadata.get(
                                "meta_cost", 0.0
                            )
                            best_program.metadata["meta_cost"] = (
                                existing_meta_cost + meta_cost
                            )

                            metadata_json = json.dumps(best_program.metadata)
                            thread_db.cursor.execute(
                                ("UPDATE programs SET metadata = ? WHERE id = ?"),
                                (metadata_json, best_program.id),
                            )
                            thread_db.conn.commit()
                        finally:
                            thread_db.close()

                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, update_metadata)
                    logger.info(
                        f"Stored final meta cost ${meta_cost:.4f} "
                        f"in best program metadata"
                    )
                except Exception as e:
                    logger.warning(f"Failed to store final meta cost in database: {e}")

            return True, meta_cost
        else:
            logger.warning("Final meta summary failed to generate recommendations")
            return False, 0.0

    async def write_meta_output_async(self, results_dir: str) -> None:
        """Async version of write_meta_output - write files in thread pool."""
        output_str = ""

        if self.sync_summarizer.meta_summary:
            output_str += "# INDIVIDUAL PROGRAM SUMMARIES\n\n"
            output_str += (
                "The following are summaries of individual programs "
                "evaluated since the last meta update:\n\n"
            )
            output_str += str(self.sync_summarizer.meta_summary)
            output_str += "\n\n"

        if self.sync_summarizer.meta_scratch_pad:
            output_str += "# GLOBAL INSIGHTS SCRATCHPAD\n\n"
            output_str += (
                "The following are global insights about optimization "
                "approaches and their effectiveness:\n\n"
            )
            output_str += str(self.sync_summarizer.meta_scratch_pad)
            output_str += "\n\n"

        if self.sync_summarizer.meta_recommendations:
            output_str += "# META RECOMMENDATIONS\n\n"
            output_str += (
                "The following are actionable recommendations for the next "
                "program generations:\n\n"
            )
            output_str += str(self.sync_summarizer.meta_recommendations)

        if output_str:
            # Create meta subdirectory if it doesn't exist
            meta_dir = Path(results_dir) / "meta"
            meta_dir.mkdir(parents=True, exist_ok=True)

            meta_path = (
                meta_dir / f"meta_{self.sync_summarizer.total_programs_processed}.txt"
            )
            # Write file in thread pool to avoid blocking event loop
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._write_file, meta_path, output_str)
            logger.info(f"Wrote meta output to {meta_path}")

    def _write_file(self, path: Path, content: str) -> None:
        """Helper to write file synchronously (for thread pool)."""
        with path.open("w", encoding="utf-8") as f:
            f.write(content)

    # Delegate read-only methods to sync summarizer
    def add_evaluated_program(self, program: Program) -> None:
        """Add newly evaluated program to the tracking list."""
        return self.sync_summarizer.add_evaluated_program(program)

    def should_update_meta(self, meta_rec_interval: Optional[int]) -> bool:
        """Check if meta update should be performed based on interval."""
        return self.sync_summarizer.should_update_meta(meta_rec_interval)

    def get_current(self):
        """Get current meta recommendations without updating."""
        return self.sync_summarizer.get_current()

    def get_sampled_recommendation(self):
        """Sample a single recommendation from the current recommendations."""
        return self.sync_summarizer.get_sampled_recommendation()

    def save_meta_state(self, filepath: str) -> None:
        """Save the meta state to a file (delegated to sync)."""
        return self.sync_summarizer.save_meta_state(filepath)

    def load_meta_state(self, filepath: str) -> bool:
        """Load the meta state from a file (delegated to sync)."""
        return self.sync_summarizer.load_meta_state(filepath)

    # Delegate other methods to sync summarizer
    def __getattr__(self, name):
        """Delegate unknown methods to sync summarizer."""
        return getattr(self.sync_summarizer, name)
