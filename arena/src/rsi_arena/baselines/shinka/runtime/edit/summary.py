from unidiff import PatchSet
from unidiff.errors import UnidiffParseError
import logging

logger = logging.getLogger(__name__)


def summarize_diff(diff_file_path: str) -> dict:
    summary = {}
    try:
        with open(diff_file_path, "r", encoding="utf-8") as f:
            patch = PatchSet(f)

        for patched_file in patch:
            file_summary = {"added": 0, "deleted": 0, "modified": 0}
            for hunk in patched_file:
                added_lines = []
                removed_lines = []
                for line in hunk:
                    if line.is_added:
                        added_lines.append(line)
                    elif line.is_removed:
                        removed_lines.append(line)

                # Infer modifications (line replacements) from paired adds/removes
                num_modifications = min(len(added_lines), len(removed_lines))
                file_summary["modified"] += num_modifications
                file_summary["added"] += len(added_lines) - num_modifications
                deleted_count = len(removed_lines) - num_modifications
                file_summary["deleted"] += deleted_count

            summary[patched_file.path] = file_summary
    except UnidiffParseError as e:
        logger.info(f"Error parsing diff file {diff_file_path}:")
        logger.info(e)
        # Return an empty summary or handle as per specific requirements
    except Exception as e:
        logger.info(f"An unexpected error occurred while processing {diff_file_path}:")
        logger.info(e)
        # Return an empty summary or handle as per specific requirements

    return summary
