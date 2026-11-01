---
name: sandbox-file-discovery-and-validation
description: Use shell tools safely in sandboxed environments by discovering files from real workspace roots instead of assumed user folders, then explicitly verifying output artifacts after generation.
---

# Sandbox File Discovery and Validation

Use this skill when working in constrained or sandboxed shell environments where common folders like `~/Desktop`, `~/Documents`, or `~/Downloads` may not exist, may be inaccessible, or may not be where task files are stored.

The goal is to:
1. discover files from actual accessible roots,
2. avoid brittle path assumptions,
3. generate outputs in known locations, and
4. explicitly validate that outputs were created and match expectations.

## Core principles

- Do not assume GUI-era user folders exist.
- Start from the current working directory and other confirmed workspace roots.
- Use recursive search from known roots, not from `/` unless necessary.
- Prefer deterministic output paths you control.
- After creating an artifact, verify it exists, is readable, and matches the requested form.

## When to use this

Use this pattern when:
- you need to locate input files in an unfamiliar sandbox,
- the environment may be ephemeral or nonstandard,
- you are producing files for the user,
- success depends on the actual contents or structure of the output.

## Recommended workflow

### 1) Establish your real working roots

Begin by identifying where you are and what directories are actually available.

Example:
`pwd`
`ls -la`
`find . -maxdepth 2 -type d | sort`

If needed, inspect nearby likely roots:
`ls -la /tmp`
`ls -la /workspace 2>/dev/null || true`
`ls -la /workspaces 2>/dev/null || true`
`ls -la /mnt/data 2>/dev/null || true`

Treat only confirmed, readable directories as search roots.

## 2) Avoid assumed folders

Do not begin with paths like:
- `~/Desktop`
- `~/Documents`
- `~/Downloads`

unless you have already confirmed they exist and are relevant.

Bad:
`find ~/Desktop -name "*.xlsx"`

Better:
`find . -type f -name "*.xlsx"`
or, if a root is confirmed:
`find /workspace -type f -name "*.xlsx" 2>/dev/null`

## 3) Search from known roots with bounded, focused queries

Prefer targeted searches over broad filesystem scans.

Useful patterns:
`find . -type f | sort`
`find . -type f -iname "*report*"`
`find . -type f \( -iname "*.csv" -o -iname "*.xlsx" -o -iname "*.json" \)`

If multiple roots are known:
`find . /workspace /mnt/data -type f 2>/dev/null | sort`

Guidelines:
- suppress permission noise with `2>/dev/null` when appropriate,
- filter by extension or name fragment,
- sort output for stable inspection,
- keep searches bounded to known roots.

## 4) Choose a deterministic output location

When generating a file, write it somewhere explicit and easy to re-check.

Good patterns:
- current directory,
- a task-specific subdirectory like `./output`,
- a confirmed writable workspace directory.

Example:
`mkdir -p output`
`python make_result.py --out output/final.xlsx`

Avoid writing to guessed locations that may not exist.

## 5) Immediately validate the output artifact

Never assume generation succeeded just because a command exited successfully.

At minimum, confirm:
- the file exists,
- the path is the one you intended,
- the file is non-empty when applicable,
- the format or structure matches the task.

Basic checks:
`ls -l output/final.xlsx`
`test -f output/final.xlsx && echo "exists"`
`test -s output/final.xlsx && echo "non-empty"`
`file output/final.xlsx`

For text-like outputs:
`head -n 20 output/result.csv`
`wc -l output/result.csv`

For structured outputs, inspect with the relevant tool:
- CSV: preview headers and row counts
- JSON: parse and inspect keys
- ZIP-like formats such as `.xlsx`: verify internal structure or open with a library
- generated code/config: run syntax or schema checks if available

## 6) Validate against task requirements, not just file existence

A file existing is necessary but not sufficient.

Check the artifact against the requested deliverable:
- expected filename or location,
- expected sheet names, tabs, columns, or keys,
- expected formulas, calculations, or derived values,
- expected number of records,
- expected transformations or formatting.

Examples:
- If a workbook was requested, verify the workbook contains the required sheets.
- If formulas were required, verify formulas are present, not just pasted values.
- If a filtered dataset was requested, verify row counts and selected columns.
- If a renamed file was requested, verify the actual output name.

## 7) Report with evidence

When you finish, cite the exact artifact path and the validation you performed.

Good completion style:
- “Created `output/final.xlsx`.”
- “Verified it exists and is non-empty.”
- “Confirmed workbook contains sheets `Summary` and `Data`.”
- “Checked that computed totals are present in column F.”

This reduces false positives where an output was produced but not actually checked.

# Shell patterns to reuse

## Minimal discovery sequence

`pwd`
`ls -la`
`find . -maxdepth 3 -type f | sort`

## Targeted file search

`find . -type f \( -iname "*.xlsx" -o -iname "*.csv" -o -iname "*.tsv" \) | sort`

## Multi-root search with graceful fallback

`find . /workspace /mnt/data -type f 2>/dev/null | sort`

## Output creation and validation

`mkdir -p output`
`some_command > output/result.txt`
`test -f output/result.txt && test -s output/result.txt && echo "validated"`

# Practical decision rules

## Prefer this
- `pwd` to anchor yourself
- `find` from `.` or other verified roots
- explicit output directories
- post-generation checks tied to the requested deliverable

## Avoid this
- guessing user-centric folders
- searching the entire filesystem first
- declaring success after creation without inspection
- reporting only the command you ran instead of what you verified

# Example end-to-end pattern

1. Discover roots:
   `pwd && ls -la`

2. Find candidate inputs:
   `find . -type f \( -iname "*.xlsx" -o -iname "*.csv" \) | sort`

3. Create controlled output location:
   `mkdir -p output`

4. Generate artifact:
   `python transform.py input/source.csv output/final.csv`

5. Validate artifact exists and inspect content:
   `ls -l output/final.csv`
   `test -s output/final.csv`
   `head -n 5 output/final.csv`

6. Validate requirement-specific properties:
   - expected headers present,
   - row count reasonable,
   - transformations applied.

# Success criteria

This pattern is applied correctly when:
- file discovery starts from confirmed accessible roots,
- no critical step depends on unverified assumed directories,
- generated artifacts are written to known locations,
- outputs are explicitly inspected or checked,
- final reporting includes evidence of validation.