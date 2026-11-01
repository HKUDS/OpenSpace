from pathlib import Path
from typing import Optional, Union
from .apply_diff import write_git_diff, _mutable_ranges, EVOLVE_START, EVOLVE_END
from .marker_validation import validate_evolve_markers
from rsi_arena.baselines.shinka.runtime.llm import extract_between
from rsi_arena.baselines.shinka.runtime.utils.languages import (
    get_code_fence_languages,
    get_language_extension,
    has_block_comments,
)
import logging

logger = logging.getLogger(__name__)


def apply_full_patch(
    patch_str: str,
    original_str: Optional[str] = None,
    patch_dir: Optional[Union[str, Path]] = None,
    original_path: Optional[Union[str, Path]] = None,
    language: str = "python",
    verbose: bool = True,
) -> tuple[str, int, Optional[Path], Optional[str], Optional[str], Optional[Path]]:
    if original_str is None and original_path is None:
        raise ValueError("Either original_str or original_path must be provided")
    if original_str is None:
        if original_path is None:
            raise ValueError("original_path cannot be None")
        og_path = Path(original_path)
        original = og_path.read_text("utf-8")
    else:
        original = original_str

    # Validate language early and derive shared metadata.
    suffix = f".{get_language_extension(language)}"
    language_fences = get_code_fence_languages(language)

    error_message: Optional[str] = None
    # Init with original content and 0 applied patches in case of error
    updated_content: str = original
    num_applied: int = 0
    output_path: Optional[Path] = None

    # Extract code from language fences (accept canonical and common aliases).
    extracted_code = "none"
    for language_fence in language_fences:
        extracted_code = extract_between(
            patch_str,
            f"```{language_fence}",
            "```",
            False,
        )
        if (
            extracted_code is not None
            and not isinstance(extracted_code, dict)
            and extracted_code != "none"
        ):
            break

    # Handle the case where extract_between returns None, dict, or "none"
    if (
        extracted_code is None
        or isinstance(extracted_code, dict)
        or extracted_code == "none"
    ):
        error_message = "Could not extract code from patch string"
        return original, 0, None, error_message, None, None

    patch_code = str(extracted_code)

    if patch_dir is not None:
        patch_dir = Path(patch_dir)
        patch_dir.mkdir(parents=True, exist_ok=True)
        # Store the raw patch content
        patch_path = patch_dir / "rewrite.txt"
        patch_path.write_text(patch_code, "utf-8")

    try:
        # Get mutable ranges from original content
        mutable_ranges = _mutable_ranges(original)

        if not mutable_ranges:
            # No EVOLVE-BLOCK regions found, treat as error for full patch
            msg = "No EVOLVE-BLOCK regions found in original content"
            error_message = msg
            return original, 0, None, error_message, None, None

        # Build updated content by preserving immutable parts
        # and replacing mutable parts
        updated_content = ""
        last_end = 0

        # Detect EVOLVE markers presence in the patch content
        patch_has_start = EVOLVE_START.search(patch_code) is not None
        patch_has_end = EVOLVE_END.search(patch_code) is not None
        patch_has_both = patch_has_start and patch_has_end
        patch_has_none = not patch_has_start and not patch_has_end

        if patch_has_both:
            if has_block_comments(language):
                marker_err = validate_evolve_markers(patch_code, language)
                if marker_err is not None:
                    return original, 0, None, marker_err, None, None

            # Patch contains both EVOLVE-BLOCK markers, extract from them
            patch_mutable_ranges = _mutable_ranges(patch_code)
            # Patch contains EVOLVE-BLOCK markers, extract from them
            for i, (start, end) in enumerate(mutable_ranges):
                # Add immutable part before this mutable range
                updated_content += original[last_end:start]

                # Get corresponding mutable content from patch
                if i < len(patch_mutable_ranges):
                    patch_start, patch_end = patch_mutable_ranges[i]
                    replacement_content = patch_code[patch_start:patch_end]
                else:
                    # Not enough mutable regions in patch, keep original
                    replacement_content = original[start:end]

                updated_content += replacement_content
                last_end = end
        elif patch_has_none:
            # Patch doesn't contain EVOLVE-BLOCK markers
            # Assume entire patch content should replace all mutable regions
            if len(mutable_ranges) == 1:
                # Single mutable region. If the patch appears to be a full-file
                # rewrite that omitted EVOLVE markers, safely extract only the
                # content intended for the evolve block by matching immutable
                # prefix/suffix from the original file.
                start, end = mutable_ranges[0]

                # Immutable portions that remain outside the evolve block
                immutable_prefix = original[:start]
                immutable_suffix = original[end:]

                # Also compute the portions strictly outside the marker lines
                # to detect full-file patches that omitted EVOLVE markers.
                # Find the start and end marker line boundaries.
                start_match = None
                end_match = None
                for m in EVOLVE_START.finditer(original):
                    if m.end() == start:
                        start_match = m
                        break
                for m in EVOLVE_END.finditer(original):
                    if m.start() == end:
                        end_match = m
                        break

                prefix_outside = (
                    original[: start_match.start()] if start_match else immutable_prefix
                )
                suffix_outside = (
                    original[end_match.end() :] if end_match else immutable_suffix
                )

                # Heuristic: if patch includes the same immutable prefix/suffix
                # outside the markers, treat the middle part as the evolve-block
                # replacement. Be tolerant to a missing trailing newline in the
                # footer by checking both versions.
                suffix_opts = (suffix_outside, suffix_outside.rstrip("\r\n"))
                if patch_code.startswith(prefix_outside) and any(
                    patch_code.endswith(sfx) for sfx in suffix_opts
                ):
                    mid_start = len(prefix_outside)
                    # choose the matching suffix option to compute end
                    sfx = next(sfx for sfx in suffix_opts if patch_code.endswith(sfx))
                    mid_end = len(patch_code) - len(sfx)
                    replacement_content = patch_code[mid_start:mid_end]
                    # Ensure marker boundaries stay on their own lines.
                    # Add a leading newline only if there is a START marker.
                    if (
                        start_match is not None
                        and replacement_content
                        and not replacement_content.startswith("\n")
                    ):
                        replacement_content = "\n" + replacement_content
                    # Add a trailing newline only if there is an END marker.
                    if (
                        end_match is not None
                        and replacement_content
                        and not replacement_content.endswith("\n")
                    ):
                        replacement_content = replacement_content + "\n"
                    updated_content = (
                        immutable_prefix + replacement_content + immutable_suffix
                    )
                else:
                    # Otherwise, assume the patch_code represents only the
                    # evolve-block payload and insert it directly between markers.
                    # Ensure proper newline handling around the patch content.
                    payload = patch_code
                    if (
                        start_match is not None
                        and payload
                        and not payload.startswith("\n")
                    ):
                        payload = "\n" + payload
                    if end_match is not None and payload and not payload.endswith("\n"):
                        payload = payload + "\n"
                    updated_content = immutable_prefix + payload + immutable_suffix
            else:
                # Multiple EVOLVE-BLOCK regions found, ambiguous without markers
                error_message = (
                    "Multiple EVOLVE-BLOCK regions found but patch "
                    "doesn't specify which to replace"
                )
                return original, 0, None, error_message, None, None
        else:
            # Patch contains exactly one marker (START xor END).
            # Only safe to apply when original has a single evolve region.
            if len(mutable_ranges) != 1:
                error_message = (
                    "Patch contains only one EVOLVE-BLOCK marker, but the original "
                    f"has {len(mutable_ranges)} editable regions; cannot determine target"
                )
                return original, 0, None, error_message, None, None

            # Single target region in original
            start, end = mutable_ranges[0]
            immutable_prefix = original[:start]
            immutable_suffix = original[end:]

            # Find exact marker locations in original for newline policy
            start_match = None
            end_match = None
            for m in EVOLVE_START.finditer(original):
                if m.end() == start:
                    start_match = m
                    break
            for m in EVOLVE_END.finditer(original):
                if m.start() == end:
                    end_match = m
                    break

            # Compute outside-of-markers prefix/suffix from original
            prefix_outside = (
                original[: start_match.start()] if start_match else immutable_prefix
            )
            suffix_outside = (
                original[end_match.end() :] if end_match else immutable_suffix
            )

            # Extract payload based on which single marker is present in patch
            if patch_has_start and not patch_has_end:
                m = EVOLVE_START.search(patch_code)
                payload = patch_code[m.end() :] if m else patch_code
                # Trim footer if the patch included it
                for sfx in (suffix_outside, suffix_outside.rstrip("\r\n")):
                    if sfx and payload.endswith(sfx):
                        payload = payload[: -len(sfx)]
                        break
            elif patch_has_end and not patch_has_start:
                m = EVOLVE_END.search(patch_code)
                payload = patch_code[: m.start()] if m else patch_code
                # Trim header if the patch included it
                for pfx in (prefix_outside, prefix_outside.rstrip("\r\n")):
                    if pfx and payload.startswith(pfx):
                        payload = payload[len(pfx) :]
                        break
            else:
                payload = patch_code

            # Normalize newlines so markers remain on their own lines
            if start_match is not None and payload and not payload.startswith("\n"):
                payload = "\n" + payload
            if end_match is not None and payload and not payload.endswith("\n"):
                payload = payload + "\n"

            updated_content = immutable_prefix + payload + immutable_suffix

        # Add remaining immutable content after last mutable range
        if patch_has_both and mutable_ranges:
            updated_content += original[mutable_ranges[-1][1] :]

        num_applied = 1

    except Exception as e:
        error_message = f"Error applying full patch: {str(e)}"
        return original, 0, None, error_message, None, None

    # Validate marker placement in the assembled candidate. Block-comment
    # languages (Wolfram, Markdown) can silently fail when both markers end
    # up inside a single comment, hiding the body from the evaluator. The
    # fix-mode loop surfaces the returned error back to the LLM.
    marker_err = validate_evolve_markers(updated_content, language)
    if marker_err is not None:
        if verbose:
            logger.info(f"Rejecting full patch: {marker_err}")
        return original, 0, None, marker_err, None, None

    # If successful, proceed to write files if patch_dir is specified
    if patch_dir is not None:
        # Store the original string as a backup file
        backup_path = patch_dir / f"original{suffix}"
        backup_path.write_text(original, "utf-8")

        # Write the updated file
        output_path = patch_dir / f"main{suffix}"
        output_path.write_text(updated_content, "utf-8")

        # Write the git diff if requested
        diff_path = patch_dir / "edit.diff"
        write_git_diff(
            original,
            updated_content,
            filename=backup_path.name,
            out_path=diff_path,
        )
        patch_txt = diff_path.read_text("utf-8")
        # Print the patch file
        if verbose:
            logger.info(f"Patch file written to: {diff_path}")
            logger.info(f"Patch file content:\n{patch_txt}")
        return (
            updated_content,
            num_applied,
            output_path,
            error_message,
            patch_txt,
            diff_path,
        )
    else:
        return updated_content, num_applied, None, error_message, None, None
