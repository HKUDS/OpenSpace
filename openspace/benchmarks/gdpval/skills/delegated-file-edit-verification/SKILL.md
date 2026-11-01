---
name: delegated-file-edit-verification
description: Verify delegated file-edit results directly in the target workspace before accepting success, and rerun with absolute paths if the delegate's report conflicts with reality.
---

# Delegated File Edit Verification

Use this workflow whenever another agent, subprocess, or delegated tool claims it successfully created or modified a file. The goal is to prevent false acceptance of incomplete, misplaced, or incorrect edits.

This skill is especially useful for structured files such as spreadsheets, CSVs, databases, and generated artifacts, but the same pattern applies to any delegated file-edit task.

## Core principle

Never accept a delegated "done" message on summary alone.

Immediately verify the result yourself in the target workspace using direct commands that inspect:
- file existence
- exact path
- expected structure
- expected size or counts
- key headers, fields, or identifiers

If the delegate's summary and your verification disagree, treat the task as incomplete. Re-run the task using absolute paths and explicit acceptance checks.

## When to use this

Use this workflow when:
- a sub-agent says it wrote or updated a file
- the task depends on a specific workspace or output location
- the output is structured and can be checked mechanically
- prior executions showed path confusion, stale files, or inconsistent summaries
- correctness matters more than trusting the delegate's narrative

## Procedure

### 1. Record the intended output contract before delegating

Before handing off the task, write down the minimum facts that must be true if the task succeeds.

Include:
- absolute target path
- required file name
- required file type
- expected sheets, tabs, tables, or sections
- expected row counts or approximate size
- required headers or columns
- any sentinel values that should appear

Example contract:
- file exists at `/workspace/results/report.xlsx`
- workbook contains sheets `summary` and `data`
- `data` sheet has more than 1,000 rows
- headers include `id`, `created_at`, `status`

Do not rely on vague goals like "updated spreadsheet correctly."

### 2. Delegate with explicit acceptance criteria

When delegating, include:
- the absolute input path
- the absolute output path
- the exact required structure
- a request to report what was changed
- a request to include paths actually used

Good delegation prompt:
- Read `/workspace/input/source.xlsx`
- Write the cleaned workbook to `/workspace/output/cleaned.xlsx`
- Ensure sheets `summary` and `records` exist
- Ensure `records` includes headers `id`, `date`, `amount`
- Report the exact absolute path written and row counts per sheet

### 3. Verify immediately after the delegate reports success

Do not move on. Run direct checks in the target workspace right away.

For all files, verify:
- the file exists at the expected absolute path
- modification time is recent
- file size is plausible
- there is not a second similarly named file in another directory

For structured files, verify the internal structure too:
- workbook sheet names
- CSV header row
- row counts
- required columns
- sample records or sentinel values

### 4. Prefer direct machine checks over narrative summaries

Trust commands and file inspection, not the delegate's prose.

Examples of useful checks:
- list the exact file path
- print sheet names
- count rows
- print header names
- inspect a few sample rows
- compare timestamps before and after

### 5. If verification conflicts with the delegated summary, mark the task incomplete

Examples of conflicts:
- delegate says file exists, but it does not
- delegate claims sheet `data` exists, but workbook contains different sheets
- reported row count differs materially from actual count
- expected headers are missing
- output was written to a relative or unexpected location
- the delegate inspected one file but wrote another

When this happens:
- do not accept the result
- state the exact mismatch
- rerun the task
- require absolute paths for every input and output
- require a final report tied to those exact paths

### 6. Re-run with absolute paths and constrained scope

The most common recovery is to eliminate ambiguity.

On rerun:
- provide absolute paths only
- restate the acceptance contract
- instruct the delegate to verify its own output before reporting success
- ask it to print the exact path and structural facts used

Example rerun instruction:
- Re-run using only these absolute paths:
  - input: `/workspace/input/source.xlsx`
  - output: `/workspace/output/cleaned.xlsx`
- Before reporting success, verify:
  - file exists
  - workbook sheets are `summary` and `records`
  - `records` row count exceeds 1000
  - headers include `id`, `date`, `amount`
- Report the exact output path, sheet names, row counts, and headers

### 7. Re-verify after the rerun

Repeat your independent verification. Only accept the task when your checks match the expected contract.

## Verification checklist

Use this checklist before accepting a delegated file-edit result:

- [ ] I know the exact absolute output path.
- [ ] I confirmed the file exists there.
- [ ] I checked modification time or other evidence it was freshly written.
- [ ] I verified the file's internal structure.
- [ ] I checked row counts, record counts, or comparable size indicators.
- [ ] I checked key headers, fields, or identifiers.
- [ ] My direct inspection matches the delegate's report.
- [ ] If there was any mismatch, I reran with absolute paths.

## Examples

## Example: generic file existence check

Use direct shell checks first.

```text
ls -l /workspace/output/result.xlsx
stat /workspace/output/result.xlsx
find /workspace -name 'result.xlsx' -o -name '*result*'
```

Interpretation:
- confirm the file exists where expected
- compare timestamps and sizes
- detect misplaced outputs

## Example: CSV verification

```text
test -f /workspace/output/data.csv
head -n 3 /workspace/output/data.csv
python - <<'PY'
import csv
path = "/workspace/output/data.csv"
with open(path, newline="") as f:
    reader = csv.reader(f)
    rows = list(reader)
print("rows_including_header:", len(rows))
print("headers:", rows[0])
PY
```

Check:
- file exists
- headers are correct
- row count is plausible

## Example: spreadsheet verification

```text
python - <<'PY'
from openpyxl import load_workbook
path = "/workspace/output/report.xlsx"
wb = load_workbook(path, read_only=True, data_only=True)
print("sheets:", wb.sheetnames)
for ws in wb.worksheets:
    print(ws.title, ws.max_row, ws.max_column)
    headers = [c for c in next(ws.iter_rows(min_row=1, max_row=1, values_only=True))]
    print("headers:", headers)
PY
```

Check:
- expected sheets exist
- row counts are plausible
- key headers are present

## Example: delegate summary conflict

Delegate says:
- wrote `/workspace/output/report.xlsx`
- created sheet `data`
- row count is 2,500

Your verification shows:
- file exists at `./report.xlsx` instead
- workbook has sheets `Sheet1` and `summary`
- no `data` sheet exists

Correct response:
- mark the task incomplete
- do not accept the delegated summary
- rerun using absolute paths and explicit checks

## Recommended response template when verification fails

Use a concise recovery message like this:

- Your report conflicts with direct verification.
- Expected output: `/absolute/path/to/output.ext`
- Observed issue: file missing / wrong sheets / wrong row count / missing headers
- Treating task as incomplete.
- Re-run using only absolute paths.
- Before reporting success, include:
  - exact output path
  - structure summary
  - counts
  - key headers

## Anti-patterns to avoid

Do not:
- accept "done" without checking the file yourself
- trust relative paths when workspace ambiguity is possible
- rely on a delegate's sample output without checking the actual artifact
- accept a file just because a similarly named file exists somewhere
- assume a workbook or dataset matches the report without verifying sheets and counts
- treat conflicting evidence as "probably fine"

## Why this works

Delegated file-edit failures often come from:
- writing to the wrong directory
- inspecting a different file than the one modified
- stale files from earlier runs
- mistaken row counts or sheet names
- overconfident summaries that skip verification

This workflow catches those failures early by making acceptance depend on direct evidence in the target workspace.

## Success criterion

A delegated file-edit task is complete only when:
1. the expected artifact exists at the exact intended path, and
2. independent verification confirms its required structure and content, and
3. that verification agrees with the delegated report.

If any of these fail, rerun with absolute paths.