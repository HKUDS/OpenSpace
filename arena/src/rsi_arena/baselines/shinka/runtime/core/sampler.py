from typing import List, Optional, Tuple, Literal
import numpy as np
from rsi_arena.baselines.shinka.runtime.database import Program
from rsi_arena.baselines.shinka.runtime.database.inspirations import InspirationContextBuilder
from rsi_arena.baselines.shinka.runtime.prompts import (
    construct_eval_history_msg,
    perf_str,
    format_text_feedback_section,
    BASE_SYSTEM_MSG,
    DIFF_SYS_FORMAT,
    DIFF_ITER_MSG,
    FULL_ITER_MSG,
    FULL_SYS_FORMATS,
    CROSS_SYS_FORMAT,
    CROSS_ITER_MSG,
    get_cross_component,
    FIX_SYS_FORMAT,
    FIX_ITER_MSG,
    format_error_output_section,
)
from rsi_arena.baselines.shinka.runtime.prompts.prompts_init import INIT_SYSTEM_MSG, INIT_USER_MSG
from rsi_arena.baselines.shinka.runtime.defaults import default_patch_type_probs, default_patch_types
import logging

logger = logging.getLogger(__name__)


class PromptSampler:
    def __init__(
        self,
        task_sys_msg: Optional[str] = None,
        language: str = "python",
        patch_types: Optional[List[str]] = None,
        patch_type_probs: Optional[List[float]] = None,
        use_text_feedback: bool = False,
        inspiration_sort_order: Literal[
            "ascending", "chronological", "none"
        ] = "ascending",
    ):
        if patch_types is None:
            patch_types = default_patch_types()
        if patch_type_probs is None:
            patch_type_probs = default_patch_type_probs()

        self.task_sys_msg = task_sys_msg
        self.language = language
        self.patch_types = patch_types
        self.patch_type_probs = patch_type_probs
        # Check if probabilities sum to 1.0 w. tolerance for errors
        prob_sum = np.sum(patch_type_probs)
        if not np.isclose(prob_sum, 1.0, atol=1e-6):
            raise ValueError(
                f"Coding type probabilities must sum to 1.0, got {prob_sum:.6f}"
            )
        # Whether to use text feedback in the prompt
        self.use_text_feedback = use_text_feedback
        # Context builder for sorting inspirations (least-to-most by default)
        self.context_builder = InspirationContextBuilder(
            sort_order=inspiration_sort_order
        )

    def initial_program_prompt(self) -> Tuple[str, str]:
        """Generate the prompt for the initial program."""
        if self.task_sys_msg is None:
            sys_msg = INIT_SYSTEM_MSG
            task_description = "The user has not provided a task description."
        else:
            sys_msg = self.task_sys_msg
            task_description = self.task_sys_msg

        user_msg = INIT_USER_MSG.format(
            language=self.language,
            task_description=task_description,
        )
        return sys_msg, user_msg

    def sample(
        self,
        parent: Program,
        archive_inspirations: List[Program],
        top_k_inspirations: List[Program],
        meta_recommendations: Optional[str] = None,
    ) -> Tuple[str, str, str]:
        if self.task_sys_msg is None:
            sys_msg = BASE_SYSTEM_MSG
        else:
            sys_msg = self.task_sys_msg

        # Sample coding type
        # Filter out crossover if no inspirations
        if len(archive_inspirations) == 0 and len(top_k_inspirations) == 0:
            valid_types = [t for t in self.patch_types if t != "cross"]
            valid_probs = [
                p
                for t, p in zip(self.patch_types, self.patch_type_probs)
                if t != "cross"
            ]
            # Renormalize probabilities
            prob_sum = sum(valid_probs)
            if prob_sum > 0:
                valid_probs = [p / prob_sum for p in valid_probs]
            else:
                # Fallback: uniform distribution if all probs are zero
                if len(valid_types) > 0:
                    valid_probs = [1.0 / len(valid_types)] * len(valid_types)
                else:
                    # No valid types, fall back to original patch types
                    valid_types = self.patch_types
                    valid_probs = self.patch_type_probs
            patch_type = np.random.choice(valid_types, p=valid_probs)
        else:
            patch_type = np.random.choice(
                self.patch_types,
                p=self.patch_type_probs,
            )

        # Add meta-recommendations BEFORE format instructions (if provided)
        if meta_recommendations not in [None, "none"] and patch_type != "cross":
            sys_msg += "\n\n# Potential Recommendations"
            sys_msg += (
                "\nThe following are potential recommendations for the "
                "next program generation:\n"
            )
            sys_msg += f"\n{meta_recommendations}"
            logger.info(
                f"Added meta recommendation to system prompt: "
                f"{meta_recommendations[:80]}..."
            )
        else:
            logger.debug(
                f"No meta recommendation added: "
                f"meta_recommendations={bool(meta_recommendations)}, "
                f"patch_type={patch_type}"
            )

        # Add format instructions AFTER meta-recommendations
        if patch_type == "diff":
            sys_msg += DIFF_SYS_FORMAT
        elif patch_type == "full":
            # Randomly sample from different full rewrite variants
            full_variant_idx = np.random.randint(0, len(FULL_SYS_FORMATS))
            selected_format = FULL_SYS_FORMATS[full_variant_idx]
            sys_msg += selected_format
        elif patch_type == "cross":
            sys_msg += CROSS_SYS_FORMAT

        # Build sorted inspiration context (combines archive + top-k)
        sorted_inspirations = self.context_builder.build_context(
            archive_inspirations, top_k_inspirations
        )

        if len(sorted_inspirations) > 0:
            eval_history_msg = construct_eval_history_msg(
                sorted_inspirations,
                language=self.language,
                include_text_feedback=self.use_text_feedback,
            )
        else:
            eval_history_msg = ""

        # Format text feedback section for current program
        text_feedback_section = ""
        if self.use_text_feedback:
            text_feedback_section = "\n" + format_text_feedback_section(
                parent.text_feedback
            )

        if patch_type == "diff":
            iter_msg = DIFF_ITER_MSG.format(
                language=self.language,
                code_content=parent.code,
                performance_metrics=perf_str(
                    parent.combined_score, parent.public_metrics
                ),
                text_feedback_section=text_feedback_section,
            )
        elif patch_type == "full":
            iter_msg = FULL_ITER_MSG.format(
                language=self.language,
                code_content=parent.code,
                performance_metrics=perf_str(
                    parent.combined_score, parent.public_metrics
                ),
                text_feedback_section=text_feedback_section,
            )
        elif patch_type == "cross":
            iter_msg = CROSS_ITER_MSG.format(
                language=self.language,
                code_content=parent.code,
                performance_metrics=perf_str(
                    parent.combined_score, parent.public_metrics
                ),
                text_feedback_section=text_feedback_section,
            )
            iter_msg += "\n\n" + get_cross_component(
                archive_inspirations,
                top_k_inspirations,
                language=self.language,
            )
        elif patch_type == "paper":
            raise NotImplementedError("Paper edit not implemented.")
        else:
            raise ValueError(f"Invalid patch type: {patch_type}")

        return (
            sys_msg,
            eval_history_msg + "\n" + iter_msg,
            patch_type,
        )

    def sample_fix(
        self,
        incorrect_program: Program,
        ancestor_inspirations: Optional[List[Program]] = None,
    ) -> Tuple[str, str, str]:
        """
        Generate prompts for fixing an incorrect program.

        This is used when no correct parent exists in the database,
        and we need to fix an incorrect program using its error output.

        Args:
            incorrect_program: The incorrect program to fix
            ancestor_inspirations: Programs from the ancestry of the program
                (sorted chronologically, oldest first)
            meta_recommendations: Optional recommendations from meta summarizer

        Returns:
            Tuple of (system_message, user_message, patch_type="fix")
        """
        if self.task_sys_msg is None:
            sys_msg = BASE_SYSTEM_MSG
        else:
            sys_msg = self.task_sys_msg

        sys_msg += FIX_SYS_FORMAT.format(language=self.language)

        # Build eval history from ancestor inspirations (already chronological)
        if ancestor_inspirations and len(ancestor_inspirations) > 0:
            eval_history_msg = construct_eval_history_msg(
                ancestor_inspirations,
                language=self.language,
                include_text_feedback=self.use_text_feedback,
                correct=False,
            )
        else:
            eval_history_msg = ""

        # Format text feedback section
        text_feedback_section = ""
        if self.use_text_feedback and incorrect_program.text_feedback:
            text_feedback_section = "\n" + format_text_feedback_section(
                incorrect_program.text_feedback
            )

        # Extract stdout/stderr from metadata if available
        metadata = incorrect_program.metadata or {}
        stdout_log = metadata.get("stdout_log", "")
        stderr_log = metadata.get("stderr_log", "")

        error_output_section = format_error_output_section(
            stdout_log=stdout_log,
            stderr_log=stderr_log,
        )

        iter_msg = FIX_ITER_MSG.format(
            language=self.language,
            code_content=incorrect_program.code,
            text_feedback_section=text_feedback_section,
            error_output_section=error_output_section,
        )

        patch_type = "fix"
        logger.info(
            f"Generated FIX prompt for incorrect program "
            f"(Gen: {incorrect_program.generation}, "
            f"Score: {incorrect_program.combined_score or 0.0:.4f}, "
            f"Ancestors: {len(ancestor_inspirations or [])})"
        )

        return (
            sys_msg,
            eval_history_msg + "\n" + iter_msg if eval_history_msg else iter_msg,
            patch_type,
        )
