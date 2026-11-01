---
name: spreadsheet-proof-gate
description: Edit Excel workbooks only after anchored preflight audit, then require deterministic Python/openpyxl post-write proof that every requested criterion is satisfied or explicitly reconciled before finalizing.
---

# Spreadsheet Proof Gate

Use this skill when you must modify, create, or validate an Excel workbook and the result must be proven against a user specification rather than merely described.

This skill combines:

- workbook discovery and identity confirmation
- pre-edit structure auditing
- safe workbook editing
- deterministic post-write verification from disk
- a machine-checkable pass/fail checklist for every requested criterion
- a hard finalization gate that rejects completion when any required criterion is unmet or unreconciled

This is a workflow skill. It does not require a specific editor, but it does require a direct Python verification pass with `openpyxl` before you report success.

## When to use

Use this skill when any of the following are true:

- the workbook path must be discovered
- multiple similar Excel files may exist
- the user requires exact sheet names, columns, counts, or samples
- the task edits an existing workbook
- a delegated agent may perform spreadsheet work
- prior summaries are inconsistent or not trustworthy
- exact post-edit workbook state matters
- you must prove that mandatory criteria were satisfied

## Core rule

Never finalize from narrative summary alone.

A workbook task is only complete when:

1. the target workbook was identified from the anchored workspace path
2. pre-edit structure matched the intended operation
3. the edit was performed on the correct file
4. the saved workbook was directly re-read from disk
5. every requested criterion was checked in a deterministic verification report
6. each criterion is marked as one of:
   - `PASS`
   - `FAIL`
   - `UNAVAILABLE-IN-SOURCE`
7. any non-pass result is explicitly reconciled before finalizing

If any mandatory criterion is still `FAIL`, do not declare success.

If a criterion is `UNAVAILABLE-IN-SOURCE`, only finalize if:
- the source workbook truly lacks the needed input,
- you state that clearly,
- and the user request did not require inventing unsupported data.

## Workspace anchoring rule

All discovery, reads, writes, and verification must be anchored to the exact workspace path provided for the task.

Rules:

- treat the provided workspace path as the authoritative root
- resolve the workbook to an exact path under that root
- do not silently switch to a similarly named file elsewhere
- do not trust a delegated tool's reported path without independently confirming it
- if the anchored file cannot be found, stop rather than guessing

## Outcome contract

Your output to yourself and to the user should be based on proof, not inference.

For every task, maintain three artifacts internally:

1. `Candidate inventory`
2. `Pre-edit audit checklist`
3. `Post-write proof checklist`

The post-write proof checklist is decisive.

# Workflow

## Phase 1: Discover candidate workbooks

Search under the exact workspace root for plausible Excel files:

- `.xlsx`
- `.xlsm`
- `.xls`

Prefer files that:

- match names mentioned by the user
- live in likely data/output/project folders
- have relevant modification times
- contain expected sheet names
- contain plausible row/column structure

If several files are plausible, inspect all plausible candidates before choosing.

## Minimum evidence before selecting a workbook

Before choosing the workbook to edit, confirm at least:

- exact anchored path
- file existence
- workbook readability
- sheet names
- approximate row counts in relevant sheets
- relevant headers in target sheets

Do not select a workbook based on filename similarity alone.

## Phase 2: Convert the user request into explicit criteria

Before editing, extract every requirement you can verify.

Possible criteria include:

- required workbook path or output path
- required sheet names
- sheets to preserve
- sheets to create
- target sheet to edit
- required columns
- required formulas or summaries
- minimum data rows
- exact row counts
- sample size
- marker/flag count
- selection rules
- category coverage requirements
- preservation of macros/formatting
- preservation of untouched sheets

Turn these into an explicit checklist.

### Criteria format

Represent each requested requirement as a discrete checkable item.

Good examples:

- `required-sheet: Summary exists`
- `required-sheet: Sample exists`
- `required-column: Sheet Sample has column "Selected"`
- `sample-count: exactly 25 marked rows in Sample`
- `coverage: at least 1 marked row where Product = Trade Finance`
- `preservation: source sheet RawData still exists`
- `formula: column H contains formulas for all populated rows`

Bad examples:

- `workbook looks right`
- `sampling seems okay`
- `most tabs present`

## Phase 3: Pre-edit audit

Before any write, inspect the workbook structure against the extracted criteria.

### Required pre-edit checks

For each relevant workbook:

1. confirm exact anchored path
2. list all sheet names
3. identify target sheets
4. inspect headers
5. count relevant rows
6. note formulas, merged cells, tables, filters, protections, or macros if relevant
7. confirm whether required columns exist already or must be created
8. confirm whether the planned edit is structurally safe

### Identity consistency check

If you inspect the workbook multiple times or with multiple tools, the inspections must agree on:

- exact path
- sheet names
- relevant row counts
- used range or dimensions when available
- relevant headers

If they disagree, treat workbook identity as unconfirmed.

### Conflict reconciliation rule

If two inspections disagree on sheet names, row counts, dimensions, or headers, do not edit until you can produce all of the following:

- an independent direct read from the exact anchored path
- explicit evidence that this is the intended workbook
- a reconciled explanation of which earlier inspection was wrong and why
- a fresh verified inventory of sheets, dimensions, row counts, and headers from the anchored file

If you cannot reconcile the conflict, the decision is no-go.

## Phase 4: Pre-edit go/no-go gate

Proceed only if all are true:

- the workbook identity is confirmed
- the target sheet is unambiguous
- required columns exist or can be added safely
- row counts are plausible for the requested operation
- no unresolved inspection conflicts remain
- the requested edit is possible from the available source data

Otherwise stop and report the mismatch.

## Phase 5: Perform the edit

Only after the workbook passes pre-edit audit:

- edit only the intended workbook
- preserve untouched sheets unless instructed otherwise
- preserve names unless renaming was requested
- preserve formatting/macros/formulas when required
- keep a clear mapping from user requirements to written cells/rows

Record at minimum:

- workbook path edited
- output path written
- sheets modified
- columns added or populated
- rows added or updated
- formulas inserted
- rows marked/selected
- any assumptions made

## Phase 6: Deterministic post-write proof

This phase is mandatory.

After saving, directly inspect the saved workbook from disk with Python and `openpyxl`. Do not finalize from memory, delegated prose, or a generic success message.

The verification pass must produce a compact, machine-checkable checklist covering every extracted criterion.

## Verification standard

The verification must be:

- direct: reads the workbook from disk
- deterministic: script-based, not conversational
- anchored: uses exact workspace paths
- criterion-based: checks each requirement explicitly
- decisive: any unresolved failure blocks completion

## Required proof outputs

At minimum, the verification must report:

- inspected file path
- whether the file exists
- workbook sheet names
- per-sheet headers for relevant sheets
- per-sheet non-empty row counts
- marker/selected-row counts if relevant
- formula presence if relevant
- preservation of required source sheets
- existence of requested output files
- criterion checklist with status for each item

## Status vocabulary

Each criterion must end in exactly one status:

- `PASS` — requirement satisfied by direct inspection
- `FAIL` — requirement not satisfied
- `UNAVAILABLE-IN-SOURCE` — requirement could not be satisfied because required source information was absent from the workbook or inputs

Do not replace these with softer wording like:
- looks okay
- appears complete
- probably satisfied
- seems unavailable

## Finalization gate

You may finalize only when every required criterion is either:

- `PASS`, or
- `UNAVAILABLE-IN-SOURCE` with explicit reconciliation

You must not finalize when:

- any mandatory criterion remains `FAIL`
- a claimed output file was not verified from disk
- the workbook path is uncertain
- post-write proof was not run
- you only have delegated summary evidence
- requested sample counts and actual marked counts differ without reconciliation
- required coverage criteria are missing and not repaired
- required sheet names or headers are missing

If direct proof says the workbook is incomplete, that proof is authoritative.

# Recommended proof procedure

## Step 1: Build the criteria list

Create a numbered list of all checkable requirements from the user request.

Example:

1. output workbook exists at `<path>`
2. sheet `Sample` exists
3. sheet `Summary` exists
4. `Sample` has column `Selected`
5. exactly 25 rows are marked selected
6. at least one selected row has `Product = Trade Finance`
7. source sheet `RawData` still exists

## Step 2: Run a direct Python inspection

Use a standalone script or inline Python. `openpyxl` is the default.

### Verification script requirements

Your script should print, in a compact format:

- file path
- existence
- sheet list
- relevant headers
- relevant row counts
- relevant marked-row counts
- any criterion-specific counts
- one line per criterion with a status

## Recommended verification script template

Adapt this template to the task.

```python
from pathlib import Path
from openpyxl import load_workbook

WORKBOOK = Path("TARGET.xlsx")

TRUTHY = {"x", "yes", "true", "1", "y"}
MARKER_CANDIDATES = {"mark", "marked", "flag", "selected", "include", "status"}

def norm(v):
    if v is None:
        return ""
    return str(v).strip()

def lower(v):
    return norm(v).lower()

def first_nonempty_row(ws, max_scan=20):
    for r in ws.iter_rows(min_row=1, max_row=min(ws.max_row, max_scan), values_only=True):
        vals = list(r)
        if any(norm(v) != "" for v in vals):
            return vals
    return []

def data_rows(ws, header_row_idx=1):
    count = 0
    for row in ws.iter_rows(min_row=header_row_idx + 1, values_only=True):
        if any(norm(v) != "" for v in row):
            count += 1
    return count

def marker_index(headers):
    normalized = [lower(h) for h in headers]
    for i, h in enumerate(normalized):
        if h in MARKER_CANDIDATES:
            return i
    return None

def is_truthy(v):
    return lower(v) in TRUTHY

def marked_rows(ws, header_row_idx=1, idx=None):
    if idx is None:
        return 0
    count = 0
    for row in ws.iter_rows(min_row=header_row_idx + 1, values_only=True):
        if not any(norm(v) != "" for v in row):
            continue
        value = row[idx] if idx < len(row) else None
        if is_truthy(value):
            count += 1
    return count

criteria = []

print(f"WORKBOOK: {WORKBOOK}")
print(f"EXISTS: {WORKBOOK.exists()}")

if not WORKBOOK.exists():
    print("CRITERION|output-exists|FAIL|Workbook file missing")
    raise SystemExit(0)

wb = load_workbook(WORKBOOK, data_only=False)
print(f"SHEETS: {wb.sheetnames}")

for ws in wb.worksheets:
    headers = first_nonempty_row(ws)
    idx = marker_index(headers) if headers else None
    print(f"SHEET|{ws.title}|HEADERS|{headers}")
    print(f"SHEET|{ws.title}|DATA_ROWS|{data_rows(ws, header_row_idx=1)}")
    if idx is not None:
        print(f"SHEET|{ws.title}|MARKER_COLUMN|{headers[idx]}")
        print(f"SHEET|{ws.title}|MARKED_ROWS|{marked_rows(ws, header_row_idx=1, idx=idx)}")
    else:
        print(f"SHEET|{ws.title}|MARKER_COLUMN|<not found>")

# Add explicit criteria below. Examples:
required_sheets = ["Sample", "Summary"]
for s in required_sheets:
    status = "PASS" if s in wb.sheetnames else "FAIL"
    reason = "present" if status == "PASS" else "missing"
    print(f"CRITERION|required-sheet:{s}|{status}|{reason}")
```

## Step 3: Add task-specific criteria to the script

The generic script is not enough by itself. Extend it to test the actual request, such as:

- exact sample count
- marker count by sheet
- presence of category coverage
- formulas in a target column
- exact column names
- output file existence
- preservation of source sheets
- expected row count delta
- values meeting business rules

If the task depends on category coverage, test category coverage explicitly rather than assuming it from total counts.

Example criteria additions:

- `exactly 40 marked rows`
- `at least one marked row for each required region`
- `column "Review Note" populated for all marked rows`
- `sheet "Summary" cell B2 contains numeric total`
- `sheet "RawData" still present after save`

## Step 4: Reconcile non-pass results

For each non-pass criterion:

### If status is `FAIL`

Do one of:

- repair the workbook and re-run proof
- report failure clearly
- ask for clarification if the request was ambiguous

Do not declare completion while a mandatory `FAIL` remains.

### If status is `UNAVAILABLE-IN-SOURCE`

State explicitly:

- what source input was missing
- which criterion could not be satisfied from source data
- whether the task allowed this limitation
- whether the workbook was left unchanged or partially completed because of it

Use `UNAVAILABLE-IN-SOURCE` only for true source limitations, not for editing mistakes.

# Required machine-checkable checklist

Every post-write verification must include a checklist in this style:

- `CRITERION|<name>|PASS|<reason>`
- `CRITERION|<name>|FAIL|<reason>`
- `CRITERION|<name>|UNAVAILABLE-IN-SOURCE|<reason>`

This format is intentionally rigid so you can reason from concrete output.

## Checklist design rules

- one criterion per line
- criterion names should be specific
- reasons should be short and factual
- do not collapse multiple requirements into one broad criterion
- if a requirement is critical, give it its own line

# Decision rules

## Hard no-go conditions before editing

Do not edit if:

- workbook path is unconfirmed
- candidate workbooks remain ambiguous
- required target sheet cannot be identified
- repeated inspections conflict and are unreconciled
- the workbook cannot be read from the anchored path

## Hard fail conditions after editing

Do not finalize if:

- a required criterion is `FAIL`
- sample count differs from request and is unexplained
- required category or rule-based coverage is missing
- direct verification cannot read the saved workbook
- only delegated-agent summary evidence exists
- required output path was not verified
- required source sheet disappeared unexpectedly

## When delegated summaries disagree with direct proof

Trust direct proof from the deterministic script.

If a delegated agent claimed success but your direct verification shows missing sheets, wrong counts, missing categories, or missing output files, the task is not complete.

# Practical checklist

## Before editing

- anchor to the exact workspace root
- locate plausible Excel files
- inspect all plausible candidates
- record exact target path
- list sheet names
- inspect relevant headers
- count relevant rows
- detect inspection conflicts
- reconcile any conflict by direct anchored read
- convert the user request into explicit criteria
- decide go/no-go

## After editing

- save the workbook to the intended path
- re-open it from disk
- run deterministic Python/openpyxl verification
- print workbook facts
- print one criterion line per requirement
- repair any `FAIL` if safe
- re-run verification
- finalize only if every mandatory item is `PASS` or explicitly reconciled `UNAVAILABLE-IN-SOURCE`

# Final response pattern

When reporting to the user, base your summary only on verified facts from the direct proof output.

Include:

- exact workbook path verified
- whether the output file exists
- sheet names present
- relevant row counts
- marked-row counts if relevant
- the outcome of mandatory criteria
- any remaining limitation explicitly

Preferred phrasing:

- "I verified the saved workbook directly from disk with Python/openpyxl."
- "The file exists at `...`."
- "The workbook contains sheets `...`."
- "The verification checklist found `N` pass items, `M` fail items, and `K` unavailable-in-source items."
- "Because one mandatory criterion failed, I am not declaring completion."
- "Because all mandatory criteria passed, the workbook is ready."

# Anti-patterns to avoid

Do not:

- finalize because an agent said it wrote the file
- rely only on workbook-edit success messages
- treat category coverage as satisfied without checking categories directly
- assume sheet names from memory
- quote counts without direct disk inspection
- ignore a failed verification line
- downgrade a real failure into vague prose
- call a task complete when proof shows mandatory gaps

# Why this skill exists

Spreadsheet tasks often fail not during editing but during final judgment. A workbook can be modified successfully and still be wrong.

This skill prevents false completion by forcing:

- anchored workbook identity checks
- deterministic post-write inspection
- criterion-by-criterion proof
- a hard stop when mandatory requirements remain unsatisfied

When workbook correctness matters, proof beats summary.
