"""
Prompt sampling for OpenEvolve
"""

import logging
import random
from typing import Any, Dict, List, Optional, Tuple, Union

from rsi_arena.baselines.openevolve.runtime.config import PromptConfig
from rsi_arena.baselines.openevolve.runtime.prompt.templates import TemplateManager
from rsi_arena.baselines.openevolve.runtime.utils.format_utils import format_metrics_safe
from rsi_arena.baselines.openevolve.runtime.utils.metrics_utils import (
    safe_numeric_average,
    get_fitness_score,
    format_feature_coordinates,
)

logger = logging.getLogger(__name__)


class PromptSampler:
    """Generates prompts for code evolution"""

    def __init__(self, config: PromptConfig):
        self.config = config
        self.template_manager = TemplateManager(custom_template_dir=config.template_dir)

        # Store custom template mappings
        self.system_template_override = None
        self.user_template_override = None

        # Only log once to reduce duplication
        if not hasattr(logger, "_prompt_sampler_logged"):
            logger.info("Initialized prompt sampler")
            logger._prompt_sampler_logged = True

    def set_templates(
        self, system_template: Optional[str] = None, user_template: Optional[str] = None
    ) -> None:
        """
        Set custom templates to use for this sampler

        Args:
            system_template: Template name for system message
            user_template: Template name for user message
        """
        self.system_template_override = system_template
        self.user_template_override = user_template
        logger.info(f"Set custom templates: system={system_template}, user={user_template}")

    def build_prompt(
        self,
        current_program: str = "",
        parent_program: str = "",
        program_metrics: Dict[str, float] = {},
        previous_programs: List[Dict[str, Any]] = [],
        top_programs: List[Dict[str, Any]] = [],
        inspirations: List[Dict[str, Any]] = [],  # Add inspirations parameter
        language: str = "python",
        evolution_round: int = 0,
        diff_based_evolution: bool = True,
        template_key: Optional[str] = None,
        program_artifacts: Optional[Dict[str, Union[str, bytes]]] = None,
        feature_dimensions: Optional[List[str]] = None,
        current_changes_description: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, str]:
        """
        Build a prompt for the LLM

        Args:
            current_program: Current program code
            parent_program: Parent program from which current was derived
            program_metrics: Dictionary of metric names to values
            previous_programs: List of previous program attempts
            top_programs: List of top-performing programs (best by fitness)
            inspirations: List of inspiration programs (diverse/creative examples)
            language: Programming language
            evolution_round: Current evolution round
            diff_based_evolution: Whether to use diff-based evolution (True) or full rewrites (False)
            template_key: Optional override for template key
            program_artifacts: Optional artifacts from program evaluation
            **kwargs: Additional keys to replace in the user prompt

        Returns:
            Dictionary with 'system' and 'user' keys
        """
        # Select template based on evolution mode (with overrides)
        if template_key:
            # Use explicitly provided template key
            user_template_key = template_key
        elif self.user_template_override:
            # Use the override set with set_templates
            user_template_key = self.user_template_override
        else:
            # Default behavior: diff-based vs full rewrite
            user_template_key = "diff_user" if diff_based_evolution else "full_rewrite_user"

        # Get the template
        user_template = self.template_manager.get_template(user_template_key)

        # Use system template override if set
        if self.system_template_override:
            system_message = self.template_manager.get_template(self.system_template_override)
        else:
            system_message = self.config.system_message
            # If system_message is a template name rather than content, get the template
            if system_message in self.template_manager.templates:
                system_message = self.template_manager.get_template(system_message)

        if self.config.programs_as_changes_description:
            if self.config.system_message_changes_description:
                system_message_changes_description = (
                    self.config.system_message_changes_description.strip()
                )
            else:
                system_message_changes_description = self.template_manager.get_template(
                    "system_message_changes_description"
                )

            system_message = self.template_manager.get_template(
                "system_message_with_changes_description"
            ).format(
                system_message=system_message,
                system_message_changes_description=system_message_changes_description,
            )

        # Format metrics
        metrics_str = self._format_metrics(program_metrics)

        # Identify areas for improvement
        improvement_areas = self._identify_improvement_areas(
            current_program, parent_program, program_metrics, previous_programs, feature_dimensions
        )

        # Format evolution history
        evolution_history = self._format_evolution_history(
            previous_programs, top_programs, inspirations, language, feature_dimensions
        )

        # Format artifacts section if enabled and available
        artifacts_section = ""
        if self.config.include_artifacts and program_artifacts:
            artifacts_section = self._render_artifacts(program_artifacts)

        # Apply stochastic template variations if enabled
        if self.config.use_template_stochasticity:
            user_template = self._apply_template_variations(user_template)

        # Calculate fitness and feature coordinates for the new template format
        feature_dimensions = feature_dimensions or []
        fitness_score = get_fitness_score(program_metrics, feature_dimensions)
        feature_coords = format_feature_coordinates(program_metrics, feature_dimensions)

        # Format the final user message
        user_message = user_template.format(
            metrics=metrics_str,
            fitness_score=f"{fitness_score:.4f}",
            feature_coords=feature_coords,
            feature_dimensions=", ".join(feature_dimensions) if feature_dimensions else "None",
            improvement_areas=improvement_areas,
            evolution_history=evolution_history,
            current_program=current_program,
            language=language,
            artifacts=artifacts_section,
            **kwargs,
        )

        if self.config.programs_as_changes_description:
            user_message = self.template_manager.get_template(
                "user_message_with_changes_description"
            ).format(
                user_message=user_message,
                changes_description=current_changes_description.rstrip(),
            )

        return {
            "system": system_message,
            "user": user_message,
        }

    def _format_metrics(self, metrics: Dict[str, float]) -> str:
        """Format metrics for the prompt using safe formatting"""
        # Use safe formatting to handle mixed numeric and string values
        formatted_parts = []
        for name, value in metrics.items():
            if isinstance(value, (int, float)):
                try:
                    formatted_parts.append(f"- {name}: {value:.4f}")
                except (ValueError, TypeError):
                    formatted_parts.append(f"- {name}: {value}")
            else:
                formatted_parts.append(f"- {name}: {value}")
        return "\n".join(formatted_parts)

    def _identify_improvement_areas(
        self,
        current_program: str,
        parent_program: str,
        metrics: Dict[str, float],
        previous_programs: List[Dict[str, Any]],
        feature_dimensions: Optional[List[str]] = None,
    ) -> str:
        """Identify improvement areas with proper fitness/feature separation"""

        improvement_areas = []
        feature_dimensions = feature_dimensions or []

        # Calculate fitness (excluding feature dimensions)
        current_fitness = get_fitness_score(metrics, feature_dimensions)

        # Track fitness changes (not individual metrics)
        if previous_programs:
            prev_metrics = previous_programs[-1].get("metrics", {})
            prev_fitness = get_fitness_score(prev_metrics, feature_dimensions)

            if current_fitness > prev_fitness:
                msg = self.template_manager.get_fragment(
                    "fitness_improved", prev=prev_fitness, current=current_fitness
                )
                improvement_areas.append(msg)
            elif current_fitness < prev_fitness:
                msg = self.template_manager.get_fragment(
                    "fitness_declined", prev=prev_fitness, current=current_fitness
                )
                improvement_areas.append(msg)
            elif abs(current_fitness - prev_fitness) < 1e-6:  # Essentially unchanged
                msg = self.template_manager.get_fragment("fitness_stable", current=current_fitness)
                improvement_areas.append(msg)

        # Note feature exploration (not good/bad, just informational)
        if feature_dimensions:
            feature_coords = format_feature_coordinates(metrics, feature_dimensions)
            if feature_coords == "":
                msg = self.template_manager.get_fragment("no_feature_coordinates")
            else:
                msg = self.template_manager.get_fragment(
                    "exploring_region", features=feature_coords
                )
            improvement_areas.append(msg)

        # Code length check (configurable threshold)
        threshold = (
            self.config.suggest_simplification_after_chars or self.config.code_length_threshold
        )
        if threshold and len(current_program) > threshold:
            msg = self.template_manager.get_fragment("code_too_long", threshold=threshold)
            improvement_areas.append(msg)

        # Default guidance if nothing specific
        if not improvement_areas:
            improvement_areas.append(self.template_manager.get_fragment("no_specific_guidance"))

        return "\n".join(f"- {area}" for area in improvement_areas)

    def _format_evolution_history(
        self,
        previous_programs: List[Dict[str, Any]],
        top_programs: List[Dict[str, Any]],
        inspirations: List[Dict[str, Any]],
        language: str,
        feature_dimensions: Optional[List[str]] = None,
    ) -> str:
        """Format the evolution history for the prompt"""
        # Get templates
        history_template = self.template_manager.get_template("evolution_history")
        previous_attempt_template = self.template_manager.get_template("previous_attempt")
        top_program_template = self.template_manager.get_template("top_program")

        # Format previous attempts (most recent first)
        previous_attempts_str = ""
        selected_previous = previous_programs[-min(3, len(previous_programs)) :]

        for i, program in enumerate(reversed(selected_previous)):
            attempt_number = len(previous_programs) - i
            changes = program.get("changes_description") or program.get("metadata", {}).get(
                "changes", self.template_manager.get_fragment("attempt_unknown_changes")
            )

            # Format performance metrics using safe formatting
            performance_parts = []
            for name, value in program.get("metrics", {}).items():
                if isinstance(value, (int, float)):
                    try:
                        performance_parts.append(f"{name}: {value:.4f}")
                    except (ValueError, TypeError):
                        performance_parts.append(f"{name}: {value}")
                else:
                    performance_parts.append(f"{name}: {value}")
            performance_str = ", ".join(performance_parts)

            # Determine outcome based on comparison with parent (only numeric metrics)
            parent_metrics = program.get("metadata", {}).get("parent_metrics", {})
            outcome = self.template_manager.get_fragment("attempt_mixed_metrics")

            # Safely compare only numeric metrics
            program_metrics = program.get("metrics", {})

            # Check if all numeric metrics improved
            numeric_comparisons_improved = []
            numeric_comparisons_regressed = []

            for m in program_metrics:
                prog_value = program_metrics.get(m, 0)
                parent_value = parent_metrics.get(m, 0)

                # Only compare if both values are numeric
                if isinstance(prog_value, (int, float)) and isinstance(parent_value, (int, float)):
                    if prog_value > parent_value:
                        numeric_comparisons_improved.append(True)
                    else:
                        numeric_comparisons_improved.append(False)

                    if prog_value < parent_value:
                        numeric_comparisons_regressed.append(True)
                    else:
                        numeric_comparisons_regressed.append(False)

            # Determine outcome based on numeric comparisons
            if numeric_comparisons_improved and all(numeric_comparisons_improved):
                outcome = self.template_manager.get_fragment("attempt_all_metrics_improved")
            elif numeric_comparisons_regressed and all(numeric_comparisons_regressed):
                outcome = self.template_manager.get_fragment("attempt_all_metrics_regressed")

            previous_attempts_str += (
                previous_attempt_template.format(
                    attempt_number=attempt_number,
                    changes=changes,
                    performance=performance_str,
                    outcome=outcome,
                )
                + "\n\n"
            )

        # Format top programs
        top_programs_str = ""
        selected_top = top_programs[: min(self.config.num_top_programs, len(top_programs))]

        for i, program in enumerate(selected_top):
            use_changes = self.config.programs_as_changes_description
            program_code = (
                program.get("changes_description", "") if use_changes else program.get("code", "")
            )
            if not program_code:
                program_code = "<missing changes_description>" if use_changes else ""

            # Calculate fitness score (prefers combined_score, excludes feature dimensions)
            score = get_fitness_score(program.get("metrics", {}), feature_dimensions or [])

            # Extract key features (this could be more sophisticated)
            key_features = program.get("key_features", [])
            if not key_features:
                key_features = []
                for name, value in program.get("metrics", {}).items():
                    if isinstance(value, (int, float)):
                        try:
                            key_features.append(
                                self.template_manager.get_fragment("top_program_metrics_prefix")
                                + f" {name} ({value:.4f})"
                            )
                        except (ValueError, TypeError):
                            key_features.append(
                                self.template_manager.get_fragment("top_program_metrics_prefix")
                                + f" {name} ({value})"
                            )
                    else:
                        key_features.append(
                            self.template_manager.get_fragment("top_program_metrics_prefix")
                            + f" {name} ({value})"
                        )

            key_features_str = ", ".join(key_features)

            top_programs_str += (
                top_program_template.format(
                    program_number=i + 1,
                    score=f"{score:.4f}",
                    language=("text" if self.config.programs_as_changes_description else language),
                    program_snippet=program_code,
                    key_features=key_features_str,
                )
                + "\n\n"
            )

        # Format diverse programs using num_diverse_programs config
        diverse_programs_str = ""
        diverse_programs: List[Dict[str, Any]] = []
        if (
            self.config.num_diverse_programs > 0
            and len(top_programs) > self.config.num_top_programs
        ):
            # Skip the top programs we already included
            remaining_programs = top_programs[self.config.num_top_programs :]

            # Sample diverse programs from the remaining
            num_diverse = min(self.config.num_diverse_programs, len(remaining_programs))
            if num_diverse > 0:
                # Use random sampling to get diverse programs
                diverse_programs = random.sample(remaining_programs, num_diverse)

                diverse_programs_str += (
                    "\n\n## "
                    + self.template_manager.get_fragment("diverse_programs_title")
                    + "\n\n"
                )

                for i, program in enumerate(diverse_programs):
                    use_changes = self.config.programs_as_changes_description
                    program_code = (
                        program.get("changes_description", "")
                        if use_changes
                        else program.get("code", "")
                    )
                    if not program_code:
                        program_code = "<missing changes_description>" if use_changes else ""

                    # Calculate fitness score (prefers combined_score, excludes feature dimensions)
                    score = get_fitness_score(program.get("metrics", {}), feature_dimensions or [])

                    # Extract key features
                    key_features = program.get("key_features", [])
                    if not key_features:
                        key_features = [
                            self.template_manager.get_fragment("diverse_program_metrics_prefix")
                            + f" {name}"
                            for name in list(program.get("metrics", {}).keys())[
                                :2
                            ]  # Just first 2 metrics
                        ]

                    key_features_str = ", ".join(key_features)

                    diverse_programs_str += (
                        top_program_template.format(
                            program_number=f"D{i + 1}",
                            score=f"{score:.4f}",
                            language=(
                                "text" if self.config.programs_as_changes_description else language
                            ),
                            program_snippet=program_code,
                            key_features=key_features_str,
                        )
                        + "\n\n"
                    )

        # Combine top and diverse programs
        combined_programs_str = top_programs_str + diverse_programs_str

        # Deduplicate: drop inspirations already shown in the top/diverse sections so
        # the same program does not appear twice in the prompt (GitHub issue #452).
        shown_ids = {p.get("id") for p in selected_top if p.get("id") is not None}
        shown_ids |= {p.get("id") for p in diverse_programs if p.get("id") is not None}
        deduped_inspirations = [
            p for p in inspirations if p.get("id") is None or p.get("id") not in shown_ids
        ]

        # Format inspirations section
        inspirations_section_str = self._format_inspirations_section(
            deduped_inspirations, language, feature_dimensions
        )

        # Combine into full history
        return history_template.format(
            previous_attempts=previous_attempts_str.strip(),
            top_programs=combined_programs_str.strip(),
            inspirations_section=inspirations_section_str,
        )

    def _format_inspirations_section(
        self,
        inspirations: List[Dict[str, Any]],
        language: str,
        feature_dimensions: Optional[List[str]] = None,
    ) -> str:
        """
        Format the inspirations section for the prompt

        Args:
            inspirations: List of inspiration programs
            language: Programming language

        Returns:
            Formatted inspirations section string
        """
        if not inspirations:
            return ""

        # Get templates
        inspirations_section_template = self.template_manager.get_template("inspirations_section")
        inspiration_program_template = self.template_manager.get_template("inspiration_program")

        inspiration_programs_str = ""

        for i, program in enumerate(inspirations):
            use_changes = self.config.programs_as_changes_description
            program_code = (
                program.get("changes_description", "") if use_changes else program.get("code", "")
            )
            if not program_code:
                program_code = "<missing changes_description>" if use_changes else ""

            # Calculate fitness score (prefers combined_score, excludes feature dimensions)
            score = get_fitness_score(program.get("metrics", {}), feature_dimensions or [])

            # Determine program type based on metadata and score
            program_type = self._determine_program_type(program, feature_dimensions or [])

            # Extract unique features (emphasizing diversity rather than just performance)
            unique_features = self._extract_unique_features(program)

            inspiration_programs_str += (
                inspiration_program_template.format(
                    program_number=i + 1,
                    score=f"{score:.4f}",
                    program_type=program_type,
                    language=("text" if self.config.programs_as_changes_description else language),
                    program_snippet=program_code,
                    unique_features=unique_features,
                )
                + "\n\n"
            )

        return inspirations_section_template.format(
            inspiration_programs=inspiration_programs_str.strip()
        )

    def _determine_program_type(
        self, program: Dict[str, Any], feature_dimensions: Optional[List[str]] = None
    ) -> str:
        """
        Determine the type/category of an inspiration program

        Args:
            program: Program dictionary

        Returns:
            String describing the program type
        """
        metadata = program.get("metadata", {})
        score = get_fitness_score(program.get("metrics", {}), feature_dimensions or [])

        # Check metadata for explicit type markers
        if metadata.get("diverse", False):
            return self.template_manager.get_fragment("inspiration_type_diverse")
        if metadata.get("migrant", False):
            return self.template_manager.get_fragment("inspiration_type_migrant")
        if metadata.get("random", False):
            return self.template_manager.get_fragment("inspiration_type_random")
        # Classify based on score ranges
        if score >= 0.8:
            return self.template_manager.get_fragment("inspiration_type_score_high_performer")
        elif score >= 0.6:
            return self.template_manager.get_fragment("inspiration_type_score_alternative")
        elif score >= 0.4:
            return self.template_manager.get_fragment("inspiration_type_score_experimental")
        else:
            return self.template_manager.get_fragment("inspiration_type_score_exploratory")

    def _extract_unique_features(self, program: Dict[str, Any]) -> str:
        """
        Extract unique features of an inspiration program

        Args:
            program: Program dictionary

        Returns:
            String describing unique aspects of the program
        """
        features = []

        # Extract from metadata if available
        metadata = program.get("metadata", {})
        if "changes" in metadata:
            changes = metadata["changes"]
            if (
                isinstance(changes, str)
                and self.config.include_changes_under_chars
                and len(changes) < self.config.include_changes_under_chars
            ):
                features.append(
                    self.template_manager.get_fragment("inspiration_changes_prefix").format(
                        changes=changes
                    )
                )

        # Analyze metrics for standout characteristics
        metrics = program.get("metrics", {})
        for metric_name, value in metrics.items():
            if isinstance(value, (int, float)):
                if value >= 0.9:
                    features.append(
                        f"{self.template_manager.get_fragment('inspiration_metrics_excellent').format(metric_name=metric_name, value=value)}"
                    )
                elif value <= 0.3:
                    features.append(
                        f"{self.template_manager.get_fragment('inspiration_metrics_alternative').format(metric_name=metric_name)}"
                    )

        # Code-based features (simple heuristics)
        code = program.get("code", "")
        if code:
            code_lower = code.lower()
            if "class" in code_lower and "def __init__" in code_lower:
                features.append(self.template_manager.get_fragment("inspiration_code_with_class"))
            if "numpy" in code_lower or "np." in code_lower:
                features.append(self.template_manager.get_fragment("inspiration_code_with_numpy"))
            if "for" in code_lower and "while" in code_lower:
                features.append(
                    self.template_manager.get_fragment("inspiration_code_with_mixed_iteration")
                )
            if (
                self.config.concise_implementation_max_lines
                and len(code.split("\n")) <= self.config.concise_implementation_max_lines
            ):
                features.append(
                    self.template_manager.get_fragment("inspiration_code_with_concise_line")
                )
            elif (
                self.config.comprehensive_implementation_min_lines
                and len(code.split("\n")) >= self.config.comprehensive_implementation_min_lines
            ):
                features.append(
                    self.template_manager.get_fragment("inspiration_code_with_comprehensive_line")
                )

        # Default if no specific features found
        if not features:
            program_type = self._determine_program_type(program)
            features.append(
                self.template_manager.get_fragment("inspiration_no_features_postfix").format(
                    program_type=program_type
                )
            )

        # Use num_top_programs as limit for features (similar to how we limit programs)
        feature_limit = self.config.num_top_programs
        return ", ".join(features[:feature_limit])

    def _apply_template_variations(self, template: str) -> str:
        """Apply stochastic variations to the template"""
        result = template

        # Apply variations defined in the config
        for key, variations in self.config.template_variations.items():
            if variations and f"{{{key}}}" in result:
                chosen_variation = random.choice(variations)
                result = result.replace(f"{{{key}}}", chosen_variation)

        return result

    def _render_artifacts(self, artifacts: Dict[str, Union[str, bytes]]) -> str:
        """
        Render artifacts for prompt inclusion

        Args:
            artifacts: Dictionary of artifact name to content

        Returns:
            Formatted string for prompt inclusion (empty string if no artifacts)
        """
        if not artifacts:
            return ""

        sections = []

        # Process all artifacts using .items()
        for key, value in artifacts.items():
            content = self._safe_decode_artifact(value)
            # Truncate if too long
            if len(content) > self.config.max_artifact_bytes:
                content = content[: self.config.max_artifact_bytes] + "\n... (truncated)"

            sections.append(f"### {key}\n```\n{content}\n```")

        if sections:
            return (
                "## "
                + self.template_manager.get_fragment("artifact_title")
                + "\n\n"
                + "\n\n".join(sections)
            )
        else:
            return ""

    def _safe_decode_artifact(self, value: Union[str, bytes]) -> str:
        """
        Safely decode an artifact value to string

        Args:
            value: Artifact value (string or bytes)

        Returns:
            String representation of the value
        """
        if isinstance(value, str):
            # Apply security filter if enabled
            if self.config.artifact_security_filter:
                return self._apply_security_filter(value)
            return value
        elif isinstance(value, bytes):
            try:
                decoded = value.decode("utf-8", errors="replace")
                if self.config.artifact_security_filter:
                    return self._apply_security_filter(decoded)
                return decoded
            except Exception:
                return f"<binary data: {len(value)} bytes>"
        else:
            return str(value)

    def _apply_security_filter(self, text: str) -> str:
        """
        Apply security filtering to artifact text

        Args:
            text: Input text

        Returns:
            Filtered text with potential secrets/sensitive info removed
        """
        import re

        # Remove ANSI escape sequences
        ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
        filtered = ansi_escape.sub("", text)

        # Redaction patterns for common sensitive values (can be expanded).
        # Named "redaction_patterns" rather than "secret_*" so SAST tools do not
        # misread this redaction table as a hardcoded secret.
        redaction_patterns = [
            (r"[A-Za-z0-9]{32,}", "<REDACTED_TOKEN>"),  # Long alphanumeric tokens
            (r"sk-[A-Za-z0-9]{48}", "<REDACTED_API_KEY>"),  # OpenAI-style API keys
            (r"password[=:]\s*[^\s]+", "password=<REDACTED>"),  # Password assignments
            (r"token[=:]\s*[^\s]+", "token=<REDACTED>"),  # Token assignments
        ]

        for pattern, replacement in redaction_patterns:
            filtered = re.sub(pattern, replacement, filtered, flags=re.IGNORECASE)

        return filtered
