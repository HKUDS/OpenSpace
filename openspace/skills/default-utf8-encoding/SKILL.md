---
name: default-utf8-encoding
description: Baseline file encoding policy. Always use UTF-8 for created or edited text files unless the project explicitly requires another encoding.
---

# UTF-8 Baseline

This is a mandatory baseline skill for local development.

## Rules

1. Default encoding for all newly created or edited text files is UTF-8.
2. Preserve existing non-UTF-8 encoding only when the target file is already using it and changing encoding may break runtime behavior.
3. When uncertain about file encoding, prefer safe read/modify/write steps that keep file content valid and avoid mojibake.
4. For JSON, Markdown, YAML, Python, TypeScript, and shell scripts, treat UTF-8 as the standard unless the repository explicitly states otherwise.

## Output Hygiene

1. Avoid introducing garbled characters caused by encoding mismatch.
2. Keep line endings and formatting consistent with repository conventions.
