---
name: spreadsheet-audit-edit-validate
description: Safely update Excel workbooks by first locating candidate files, verifying workbook structure before editing, and running post-write validation against required sheets, populated columns, and sample counts.
---

# Spreadsheet Audit Edit Validate

Use this workflow whenever you need to modify or generate an Excel workbook and the task depends on matching an expected workbook structure. The goal is to avoid editing the wrong file, writing into the wrong sheet, or delivering a workbook that does not satisfy the user's required tabs, columns, or counts.
Use this workflow whenever you need to create or modify an Excel workbook and the task depends on matching an expected workbook structure. The goal is to avoid editing the wrong file, writing into the wrong sheet, or delivering a workbook that does not satisfy the user's required tabs, columns, or counts.

**Important:** This workflow has two distinct modes depending on the task type:

| Task Type | Pre-edit Audit | Post-write Validation |
|-----------|----------------|----------------------|
| **Modify existing workbook** | Full audit required (Phases 1-2) | Full validation required (Phase 4) |
| **Create new workbook from source/reference data** | Skip pre-edit audit (no existing file to audit) | Full validation required (Phase 4) |

This is a workflow skill, not a tool-specific recipe. Apply it with any spreadsheet-capable tooling.

## When to use

Use this workflow when:

- You are modifying an existing workbook AND the workbook path is uncertain or must be discovered from the filesystem
- The workbook path is uncertain or must be discovered from the filesystem
- Multiple similar `.xlsx` or `.xlsm` files may exist
- The user specifies required sheet names, row counts, sample counts, or populated columns
- A silent structure mismatch would cause downstream errors

**Before proceeding, determine which workflow mode applies:**

**Mode A: Modify existing workbook** - Use the full workflow (all phases) when there is an existing workbook file that must be updated in place, the user references an existing file, or the task requires preserving existing data/formulas/formatting.

**Mode B: Create new workbook from source/reference data** - Skip Phase 1 (locate) and Phase 2 (pre-edit audit) when the task requires generating a new workbook from scratch, source data exists elsewhere but the target workbook does not exist, or the user asks to "create", "generate", or "build" a report. Phase 4 (post-write validation) is still MANDATORY for create mode.

## Core principle

**For modify-existing mode:** Never write first.

**For create-new mode:** You must create the file, but validate thoroughly after creation.

Always perform three phases in order:

1. Find candidate workbook files
2. Verify workbook structure before editing
3. Validate the saved workbook against the user specification

**Note:** For create-new mode, skip directly to Phase 3 (create), then perform Phase 4 (validation). Phase 4 is mandatory for all modes.

If any check fails, stop and resolve the mismatch before continuing.

## Workspace-path anchoring rule

All spreadsheet operations must be anchored to the exact workspace path provided for the task.
- For **modify-existing mode**: Discovery, inspection, editing, and validation must use the anchored path
- For **create-new mode**: The output path must be under the anchored workspace; validate the created file from that path

- Treat the provided workspace path as the authoritative root for all file operations
- Resolve the selected workbook to an exact path under that workspace before editing
- Do not switch to a different directory, fallback path, temp copy, or similarly named workbook unless the user explicitly authorizes it
- If a delegated tool reports a workbook path, independently verify that exact path from the current workspace before trusting the result
- If the workbook cannot be found at the anchored workspace path, stop rather than guessing

# Workflow

## Phase 1: Locate candidate Excel files

Search the filesystem broadly enough to find plausible workbooks, then narrow to the file most likely intended by the user.

### What to look for

Search for:

- `.xlsx`
- `.xlsm`
- `.xls`
- files whose names resemble the user request
- recently modified files
- files inside likely project/output/data directories

### Selection heuristics

Prefer files that:

- Match names mentioned by the user
- Are in the working directory or attached-data area
- Have expected modification dates
- Contain sheet names matching the task
- Have row/column structure consistent with the requested operation

If several files are plausible, inspect them all before choosing.

### Minimum evidence before selecting a file

Before deciding "this is the workbook to edit", confirm at least:

- exact path
- workbook sheet names
- approximate row counts in relevant sheets
- whether the file is readable and not obviously corrupt

The exact path must be recorded as a workspace-anchored path, not a vague filename.

## Phase 2: Pre-edit audit

Before making any edits, inspect the workbook and compare it to the user specification.

### Build a checklist from the user request

Extract the following if present:

- required tab names
- tabs to create, preserve, or modify
- expected columns
- key identifiers
- required row counts or minimum rows
- sample size or number of marked rows
- user-mandated sampling criteria that must appear in the selected sample
- whether task instructions permit a fallback when a required criterion is unavailable in the source population
- formulas, summaries, or computed fields
- whether formatting/macros must be preserved

Turn these into explicit checks.

### Verify workbook structure

For each relevant workbook:

1. List all sheet names
2. Identify the target sheet(s)
3. Count rows in each relevant sheet
4. Inspect header row values
5. Confirm key columns exist
6. Note any merged cells, formulas, filters, tables, or protected sheets that may affect edits

### Workbook identity consistency check

Before any write, confirm that repeated inspections agree on workbook identity and structure.

At minimum, compare across inspections:

- exact workbook path
- sheet-name list
- relevant row counts
- relevant sheet dimensions or used ranges when available
- header values for each target sheet

If these differ across runs or tools in any unexplained way, treat the workbook identity as unconfirmed and do not edit until the discrepancy is resolved.

### Conflict-resolution requirement for inconsistent inspections

If repeated reads disagree on sheet names, dimensions, or headers, do not rely on summaries, delegated-tool claims, or majority vote.

Before any edit, require all of the following:

- an independent direct read of the workbook from the exact anchored path
- explicit path evidence showing the inspected file is the same intended workbook
- a reconciled statement of which earlier inspection was wrong and why
- a fresh verified inventory of sheet names, relevant dimensions, row counts, and headers taken from that exact path

If you cannot produce this reconciliation, the go/no-go decision is automatically no-go.

### Pre-edit go/no-go decision

Proceed only if all of the following are true:

- The workbook is the correct one
- The target sheet names match or can be mapped unambiguously
- The required columns are present or can be created safely
- The row counts are plausible for the requested operation
- There is no unresolved ambiguity about where edits belong
- Repeated inspections agree on workbook identity, sheet names, row counts, dimensions, and relevant headers
- Any earlier conflicting inspection has been reconciled by an independent direct read with exact path evidence

If not, stop and report the discrepancy.

## Phase 3: Perform the edit

After the workbook passes the pre-edit audit:

- Preserve untouched sheets and workbook structure
- Avoid renaming sheets unless requested
- Write only to the intended workbook and target tabs
- Preserve formulas/macros/formatting when the task requires it
- Keep a clear mapping between user requirements and the cells/rows being updated

During the edit, record what changed:

- workbook path
- edited sheets
- rows added/updated
- columns populated
- formulas inserted
- samples marked or flags applied

## Phase 4: Post-write validation


### Mode B special consideration

For **create-new (Mode B)** workflows, Phase 4 is your ONLY validation opportunity since Phases 1-2 were skipped. Be especially thorough:

- Verify ALL required sheets exist (no pre-edit baseline to compare against)
- Verify ALL required columns are populated with actual data (not formulas returning blanks)
- Verify sample/row counts match specifications exactly
- Do not assume creation succeeded just because no error was raised during write

After saving, re-open or re-read the saved workbook and verify the output against the original specification.

This phase is mandatory.

### Independent verification requirement

Post-write validation must include an independent direct file read of the saved workbook from the anchored workspace path.

- Do not rely only on delegated-agent summaries, success messages, or memory of what was written
- Re-open or inspect the saved file using a direct read step that independently confirms the workbook now on disk
- If a delegated tool performed the edit, separately verify the resulting file yourself from the workspace path before declaring success
- If direct verification fails, returns an error, or cannot confirm the workbook structure, do not declare completion

### Required checks

At minimum, validate:

- the expected workbook file exists at the output path
- the validated file path exactly matches the selected pre-edit workbook path or an explicitly requested output path
- required tab names are present
- edited tab names exactly match the required names
- relevant sheets contain the expected row counts or non-empty data regions
- required columns are populated
- sample-mark counts match the requested count
- every user-mandated sampling criterion is represented in the selected sample, or the workbook documents that the criterion was unavailable in the source population and the task instructions explicitly permit that fallback
- formulas or derived fields exist where required
- no required source sheets disappeared during the write

### Validate sheet names

Check that:

- every required tab exists
- tab spelling matches the request
- no expected tab was accidentally renamed
- any newly created tabs were created with the exact required names
- sheet names match the pre-edit understanding unless a requested change explains the difference

### Validate row counts

Compare actual counts to the specification:

- exact row count if the user gave one
- otherwise minimum expected non-empty rows
- verify the count after writing, not only before
- compare post-write counts to pre-edit counts and expected deltas

If the operation selects or marks a subset, verify both:

- total eligible rows
- total rows actually marked

### Validate populated columns

For each required column:

- confirm the column header exists
- confirm cells are populated where expected
- spot-check several rows
- ensure values are not all blank, null, or formula errors

For flags or marks, verify the mark is present in the intended rows and absent elsewhere unless specified.

### Validate sample counts

If the task required marking or selecting `N` rows:

- count rows with the mark
- confirm the count equals `N`
- if selection rules exist, confirm marked rows satisfy them
- ensure duplicate rows were not marked unless allowed
- explicitly reconcile requested sample count, calculated sample size, and actual marked-row count
- if the user mandated named sampling criteria, verify each required criterion appears in the selected sample
- if any required sampling criterion is absent, mark the run no-go unless the workbook itself documents that the criterion was unavailable in the source population and the task instructions explicitly permit that fallback
- if any of those counts differ, do not declare success until you explain the difference and either repair it or report failure

### Spot-check content quality

Perform a small but meaningful audit:

- inspect a few representative rows
- inspect at least one row near the top, middle, and bottom when possible
- confirm derived values look plausible
- confirm formulas reference the intended cells/ranges
- confirm no obvious truncation, header shift, or column misalignment occurred

### Completion rejection conditions

Reject completion if any of the following occur:

- the workbook path differs across inspections without a clear explanation
- sheet names differ across inspections without a requested change
- row counts differ across inspections without a justified reason tied to the edit
- header values or used-range dimensions differ across inspections without reconciliation before edit
- requested sample count, computed sample size, and actual marked-row count are not explicitly reconciled
- any user-mandated sampling criterion is missing from the selected sample, unless the workbook documents that the criterion was unavailable in the source population and the task instructions explicitly allow that fallback
- direct post-write file verification cannot be completed successfully
- the only evidence of success is a delegated tool's summary

## Decision rules

### If pre-edit validation fails

Do not edit blindly. Instead:

- search for additional candidate files
- inspect neighboring workbooks
- ask for clarification if ambiguity remains
- explain the mismatch clearly

### If post-write validation fails

Do not declare success. Instead:

- identify the specific failed check
- repair the workbook if safe
- re-run validation
- if a required sampling criterion remains absent, keep the outcome as no-go unless the workbook documents source-population unavailability and the task instructions permit that fallback
- report any remaining unresolved issue explicitly

# Practical checklist

Use this checklist in order.

## Before editing

- Anchor all file operations to the exact provided workspace path
- Locate all plausible Excel files
- Choose the most likely target workbook
- Record the exact workbook path
- Read sheet names
- Read relevant headers
- Count rows in relevant sheets
- If any repeated inspection disagrees on sheet names, dimensions, or headers, perform an independent direct read from the exact path and reconcile the conflict before editing
- Repeat key inspection if needed and confirm the same path, sheet names, row counts, dimensions, and headers
- Map user requirements to workbook structure
- Confirm there is no ambiguity

## After editing

- Re-open saved workbook from the same anchored path
- Independently direct-read the saved file, not just delegated output
- Confirm required tab names
- Confirm row counts
- Confirm required columns populated
- Confirm sample/mark counts
- Confirm every user-mandated sampling criterion appears in the selected sample, or verify that the workbook documents source-population unavailability and the task instructions allow that fallback
- Explicitly reconcile requested sample count versus actual marked-row count before reporting success
- Spot-check representative rows
- Reject completion if path, sheet names, counts, headers, or required sampling coverage are inconsistent
- Only then report completion

# Recommended audit record

When working autonomously, keep a compact internal record like this:

- Workspace root: `<path>`
- Target file: `<path>`
- Candidate files checked: `<paths>`
- Required tabs: `<list>`
- Existing tabs before edit: `<list>`
- Relevant sheet row counts before edit: `<sheet -> count>`
- Relevant sheet dimensions before edit: `<sheet -> range or size>`
- Headers verified before edit: `<sheet -> headers>`
- Columns required: `<list>`
- Requested sample count / computed sample size: `<value>`
- Edits made: `<summary>`
- Tabs after edit: `<list>`
- Row counts after edit: `<sheet -> count>`
- Actual marked-row count after edit: `<value>`
- Independent direct-read verification completed: `<yes/no>`
- Validation results: `<pass/fail per check>`

# Example validation plan

Suppose the user asks for:

- add a tab named `Reviewed`
- populate columns `ID`, `Status`, and `Notes`
- mark exactly 25 samples

A proper execution plan is:

1. Search for candidate workbook files within the provided workspace path
2. Inspect each candidate's sheet names
3. Choose the workbook that contains the expected source data
4. Verify source row count is sufficient for 25 samples
5. Re-check the chosen workbook path and structure if there was any tool disagreement
6. If any inspection conflicts on sheet names, dimensions, or headers, perform an independent direct read from the exact path and reconcile the conflict before editing
7. Edit the workbook
8. Save the workbook
9. Re-open it with a direct file read from the same workspace path
10. Check that `Reviewed` exists
11. Check `ID`, `Status`, and `Notes` headers exist in `Reviewed`
12. Check those columns are populated in expected rows
13. Count marked samples and confirm exactly 25
14. Explicitly reconcile requested sample count versus actual marked-row count
15. Spot-check several rows for correctness

# Example pseudo-code

This pseudo-code illustrates the workflow independent of any specific library.

1. Find files matching Excel extensions under the provided workspace path
2. For each candidate:
   - open workbook metadata
   - list sheet names
   - read header rows from likely sheets
   - estimate row counts
3. Select the best candidate and record its exact path
4. Repeat inspection as needed until path, sheet names, row counts, dimensions, and headers are consistent
5. If any inspection conflicts, perform an independent direct read from that exact path and reconcile the discrepancy before proceeding
6. Assert required sheets/columns are present or intentionally creatable
7. Perform edits
8. Save workbook
9. Re-open saved workbook with a direct file read
10. Assert:
   - required sheets exist
   - required columns exist
   - row counts match expectations
   - marked row count equals requested sample size
   - requested sample count and actual marked-row count are explicitly reconciled
   - validated path matches the intended path
11. If any assertion fails, repair or report failure

# Common failure modes this workflow prevents

- Editing the wrong workbook because names were similar
- Drifting to a workbook outside the provided workspace path
- Assuming a sheet exists without verifying
- Writing data into a sheet with shifted headers
- Proceeding after conflicting inspections without independently reconciling the exact workbook path and structure
- Delivering a workbook with missing required tabs
- Marking the wrong number of samples
- Reporting success based only on delegated summaries
- Reporting success without re-reading the output
- Losing required sheets during write operations

# Output expectations for autonomous agents

When reporting completion, include concise validation evidence such as:

- workspace root used
- selected workbook path
- relevant sheet names found
- row counts before and after
- required tabs confirmed
- required columns confirmed populated
- sample-mark count confirmed
- confirmation that independent direct-read verification succeeded
- confirmation that requested sample count and actual marked-row count were reconciled

Do not simply say the workbook was updated; state what was validated.

# Rule of thumb

If you cannot prove from a direct read of the saved workbook at the anchored workspace path that the workbook identity and required structure are correct after saving, that every user-mandated sampling criterion is represented in the selected sample or explicitly documented in-workbook as unavailable when the task instructions allow that fallback, and that the requested sample count matches the actual marked-row count or any difference has been explicitly reconciled, the task is not complete.
