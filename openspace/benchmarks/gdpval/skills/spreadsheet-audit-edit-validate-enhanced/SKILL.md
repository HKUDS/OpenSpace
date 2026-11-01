---
name: spreadsheet-struct-verified
description: Create and modify Excel workbooks with mandatory deterministic Python verification of workbook structure, required sheets, populated columns, and sample counts before reporting completion.
---

# Spreadsheet Structure Verified

Use this workflow whenever you need to create or modify an Excel workbook where the output must match an expected structure. The goal is to avoid delivering a workbook that does not satisfy the user's required tabs, columns, or counts by using deterministic Python verification.

**Important:** This workflow has two distinct modes:

| Task Type | Pre-edit Audit | Post-write Verification |
|-----------|----------------|------------------------|
| **Modify existing workbook** | Full audit required (Phases 1-2) | Full verification required (Phase 4) |
| **Create new workbook from source/reference data** | Skip pre-edit audit (no existing file) | **Deterministic Python verification REQUIRED** (Phase 4) |

This is a workflow skill, not a tool-specific recipe. Apply it with any spreadsheet-capable tooling, but Phase 4 verification uses a deterministic Python script.

## When to use

Use this workflow when:

- You are creating a new workbook from source/reference data
- You are modifying an existing workbook AND the workbook path is uncertain
- Multiple similar `.xlsx` or `.xlsm` files may exist
- The user specifies required sheet names, row counts, sample counts, or populated columns
- A silent structure mismatch would cause downstream errors
- **Systematic post-write verification is required before reporting completion**

**Before proceeding, determine which workflow mode applies:**

**Mode A: Modify existing workbook** - Use the full workflow (all phases) when there is an existing workbook file that must be updated in place, the user references an existing file, or the task requires preserving existing data/formulas/formatting.

**Mode B: Create new workbook from source/reference data** - Adapt Phase 1-2 to audit SOURCE reference files instead of targeting a non-existent workbook. Phase 1 locates and inspects source data files; Phase 2 audits their structure (sheet names, columns, data types, row counts, merged cells, formulas). Skip target workbook pre-edit audit (it doesn't exist yet), but **DO audit the source/reference files thoroughly** before creating. **Phase 4 (deterministic Python verification) is MANDATORY for create mode.**

## Core principle

**For modify-existing mode:** Never write first. Verify before editing.

**For create-new mode:** You must create the file, but verify thoroughly after creation using a deterministic Python script.

Always perform these phases in order:

1. Find candidate workbook files (modify mode only)
2. Verify workbook structure before editing (modify mode only)
3. Perform the edit or create
4. **Deterministic Python verification** (ALL modes - MANDATORY)

If any check fails, stop and resolve the mismatch before continuing.

## Workspace-path anchoring rule

All spreadsheet operations must be anchored to the exact workspace path provided for the task.

- For **modify-existing mode**: Discovery, inspection, editing, and verification must use the anchored path
- For **create-new mode**: The output path must be under the anchored workspace; verify the created file from that path

- Treat the provided workspace path as the authoritative root for all file operations
- Resolve the selected workbook to an exact path under that workspace before editing
- Do not switch to a different directory, fallback path, temp copy, or similarly named workbook unless the user explicitly authorizes it
- If a delegated tool reports a workbook path, independently verify that exact path from the current workspace before trusting the result
- If the workbook cannot be found at the anchored workspace path, stop rather than guessing

# Workflow

> **Create mode:** In create mode, Phase 1 focuses on locating SOURCE reference data files rather than a target workbook. Audit these source files to understand their structure before creating the new workbook.

## Phase 1: Locate candidate Excel files (Modify mode only)

Search the filesystem broadly enough to find plausible workbooks, then narrow to the file most likely intended by the user.

### What to look for

Search for:

- `.xlsx`
- `.xlsm`
- `.xls`
- files whose names resemble the user request
- recently modified files
- files inside likely project/output/data directories
- **In create mode:** source data files, reference reports, or input spreadsheets that will populate the new workbook

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
- **In create mode:** column names, data types in source sheets, and presence of merged cells or formulas that may affect data extraction

The exact path must be recorded as a workspace-anchored path, not a vague filename.

> **Create mode:** In create mode, Phase 2 audits SOURCE reference files to understand their structure before creating the new workbook. This prevents calculation errors, missing data, or structural mismatches.

## Phase 2: Pre-edit audit (Modify mode only)

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

**Modify mode:** Verify the target workbook structure before editing.

**Create mode:** Audit source/reference data files to document their structure (sheet names, column names, data types, row counts, merged cells, formulas, data quality issues). Use this audit to plan the target workbook structure.

### Verify workbook structure

For each relevant workbook:

1. List all sheet names
2. Identify the target sheet(s)
3. Count rows in each relevant sheet
4. Inspect header row values
5. Confirm key columns exist
6. Note any merged cells, formulas, filters, tables, or protected sheets that may affect edits

**Create mode source audit checklist:**

1. List all sheet names in each source file
2. Identify which sheets contain the data needed for the new workbook
3. Count rows in each relevant source sheet
4. Inspect header row values and record exact column names (case-sensitive)
5. Confirm key columns exist and note their data types (text, number, date, etc.)
6. **Critical:** Note any merged cells, formulas, filters, tables, or hidden rows that may affect data extraction
7. Check for blank cells, inconsistent formatting, or data quality issues in source columns
8. Document the mapping from source columns to target columns in the new workbook

### Workbook identity consistency check

Before any write, confirm that repeated inspections agree on workbook identity and structure.

At minimum, compare across inspections:

- exact workbook path
- sheet-name list
- relevant row counts
- relevant sheet dimensions or used ranges when available
- header values for each target sheet

If these differ across runs or tools in any unexplained way, treat the workbook identity as unconfirmed and do not edit until the discrepancy is resolved.

### Pre-edit go/no-go decision

Proceed only if all of the following are true:

- The workbook is the correct one
- The target sheet names match or can be mapped unambiguously
- The required columns are present or can be created safely
- The row counts are plausible for the requested operation
- There is no unresolved ambiguity about where edits belong

If not, stop and report the discrepancy.

## Phase 3: Perform the edit or create

After the workbook passes the pre-edit audit (modify mode) or when creating new (create mode):

- Preserve untouched sheets and workbook structure (modify mode)
- Avoid renaming sheets unless requested
- Write only to the intended workbook and target tabs
- Preserve formulas/macros/formatting when the task requires it
- Keep a clear mapping between user requirements and the cells/rows being updated
- **In create mode:** Document the planned target structure (sheet names, column names, expected row counts, formulas) based on source data audit before writing

During the edit, record what changed:

- workbook path
- edited sheets
- rows added/updated
- columns populated
- formulas inserted
- samples marked or flags applied

**For create-new mode:** Document the intended structure (sheet names, columns, expected row counts) before writing, so Phase 4 can verify against this specification. **This documentation must be based on the source data audit from Phase 2.**

## Phase 4: Deterministic Python Verification (ALL modes - MANDATORY)

After saving, create and execute a deterministic Python verification script that independently validates the saved workbook against the original specification.

This phase is **MANDATORY for all modes** and must use a direct Python script with openpyxl (or equivalent) for deterministic verification.

### Why deterministic Python verification

- Eliminates ambiguity from tool summaries or delegated-agent claims
- Provides reproducible, structured verification output
- Catches structural errors before reporting completion
- Creates an auditable verification record

### Create verification script

Write a Python script that:

1. Opens the saved workbook from the exact anchored workspace path
2. Verifies all required sheet names exist with exact spelling
3. Verifies required columns exist in each relevant sheet
4. Counts rows and validates against expected counts
5. Checks sample-mark counts match requested counts
6. Validates that every user-mandated sampling criterion is represented
7. Spot-checks content quality (non-blank values in required columns)
8. Reports structured pass/fail results for each check

### Verification script template

```python
#!/usr/bin/env python3
"""Deterministic workbook structure verification script."""

from openpyxl import load_workbook
import sys
from pathlib import Path

def verify_workbook(workbook_path, specification):
    """
    Verify workbook structure against specification.
    
    Args:
        workbook_path: Exact path to the saved workbook
        specification: Dict with required_sheets, required_columns, 
                      expected_row_counts, sample_criteria, etc.
    
    Returns:
        dict with verification_results, passed (bool), errors (list)
    """
    results = {
        'passed': True,
        'errors': [],
        'warnings': [],
        'details': {}
    }
    
    # Load workbook
    try:
        wb = load_workbook(workbook_path, data_only=True)
        results['details']['workbook_loaded'] = True
    except Exception as e:
        results['passed'] = False
        results['errors'].append(f"Failed to load workbook: {e}")
        return results
    
    # Verify required sheets
    actual_sheets = wb.sheetnames
    required_sheets = specification.get('required_sheets', [])
    
    for sheet_name in required_sheets:
        if sheet_name in actual_sheets:
            results['details'][f'sheet_{sheet_name}'] = 'present'
        else:
            results['passed'] = False
            results['errors'].append(f"Missing required sheet: {sheet_name}")
            results['details'][f'sheet_{sheet_name}'] = 'missing'
    
    # Verify each required sheet
    for sheet_name in required_sheets:
        if sheet_name not in actual_sheets:
            continue
            
        ws = wb[sheet_name]
        
        # Get headers from first row
        headers = []
        if ws.max_row >= 1:
            for col in range(1, ws.max_column + 1):
                cell_value = ws.cell(row=1, column=col).value
                headers.append(str(cell_value).strip() if cell_value else '')
        
        results['details'][f'{sheet_name}_headers'] = headers
        
        # Verify required columns for this sheet
        sheet_required_cols = specification.get('required_columns', {}).get(sheet_name, [])
        for col_name in sheet_required_cols:
            if col_name in headers:
                results['details'][f'{sheet_name}_col_{col_name}'] = 'present'
            else:
                results['passed'] = False
                results['errors'].append(
                    f"Missing required column '{col_name}' in sheet '{sheet_name}'"
                )
                results['details'][f'{sheet_name}_col_{col_name}'] = 'missing'
        
        # Count data rows (excluding header)
        data_row_count = max(0, ws.max_row - 1) if ws.max_row > 1 else 0
        results['details'][f'{sheet_name}_row_count'] = data_row_count
        
        # Verify expected row counts
        expected_count = specification.get('expected_row_counts', {}).get(sheet_name)
        if expected_count is not None:
            if data_row_count < expected_count:
                results['passed'] = False
                results['errors'].append(
                    f"Sheet '{sheet_name}' has {data_row_count} rows, expected at least {expected_count}"
                )
        
        # Verify populated columns (spot check)
        for col_name in sheet_required_cols:
            if col_name not in headers:
                continue
            col_idx = headers.index(col_name) + 1
            
            # Check first few data rows are populated
            populated_count = 0
            for row in range(2, min(ws.max_row + 1, 11)):
                cell_value = ws.cell(row=row, column=col_idx).value
                if cell_value is not None and str(cell_value).strip() != '':
                    populated_count += 1
            
            if populated_count == 0 and data_row_count > 0:
                results['warnings'].append(
                    f"Column '{col_name}' in sheet '{sheet_name}' appears empty"
                )
    
    # Verify sample counts if applicable
    sample_mark_col = specification.get('sample_mark_column')
    sample_mark_value = specification.get('sample_mark_value', 'Yes')
    expected_sample_count = specification.get('expected_sample_count')
    
    if sample_mark_col and expected_sample_count is not None:
        for sheet_name in required_sheets:
            if sheet_name not in actual_sheets:
                continue
            ws = wb[sheet_name]
            headers = []
            if ws.max_row >= 1:
                for col in range(1, ws.max_column + 1):
                    cell_value = ws.cell(row=1, column=col).value
                    headers.append(str(cell_value).strip() if cell_value else '')
            
            if sample_mark_col not in headers:
                continue
            
            col_idx = headers.index(sample_mark_col) + 1
            marked_count = 0
            for row in range(2, ws.max_row + 1):
                cell_value = ws.cell(row=row, column=col_idx).value
                if cell_value is not None and str(cell_value).strip() == sample_mark_value:
                    marked_count += 1
            
            results['details'][f'{sheet_name}_marked_count'] = marked_count
            
            if marked_count != expected_sample_count:
                results['passed'] = False
                results['errors'].append(
                    f"Expected {expected_sample_count} marked rows in '{sheet_name}', found {marked_count}"
                )
    
    # Verify sampling criteria if applicable
    sampling_criteria = specification.get('sampling_criteria', [])
    if sampling_criteria:
        # Check that each criterion is represented in marked rows
        # This requires checking the actual marked row content
        for criterion in sampling_criteria:
            criterion_found = False
            for sheet_name in required_sheets:
                if sheet_name not in actual_sheets:
                    continue
                ws = wb[sheet_name]
                # Simplified check - look for criterion value in marked rows
                # Full implementation would check specific columns
                for row in range(2, ws.max_row + 1):
                    for col in range(1, ws.max_column + 1):
                        cell_value = ws.cell(row=row, column=col).value
                        if cell_value is not None and str(cell_value).strip() == criterion:
                            criterion_found = True
                            break
                    if criterion_found:
                        break
                if criterion_found:
                    break
            
            results['details'][f'criterion_{criterion}'] = 'found' if criterion_found else 'missing'
            if not criterion_found:
                # Check if fallback is permitted
                if not specification.get('criterion_fallback_permitted', False):
                    results['passed'] = False
                    results['errors'].append(
                        f"Sampling criterion '{criterion}' not found in marked rows"
                    )
                else:
                    results['warnings'].append(
                        f"Sampling criterion '{criterion}' not found (fallback permitted)"
                    )
    
    return results


def main():
    if len(sys.argv) < 3:
        print("Usage: verify_workbook.py <workbook_path> <spec_json>")
        sys.exit(1)
    
    workbook_path = sys.argv[1]
    spec_json = sys.argv[2]
    
    import json
    specification = json.loads(spec_json)
    
    results = verify_workbook(workbook_path, specification)
    
    # Print structured results
    print("=" * 60)
    print("WORKBOOK VERIFICATION RESULTS")
    print("=" * 60)
    print(f"Workbook: {workbook_path}")
    print(f"Status: {'PASSED' if results['passed'] else 'FAILED'}")
    print()
    
    if results['errors']:
        print("ERRORS:")
        for err in results['errors']:
            print(f"  ✗ {err}")
        print()
    
    if results['warnings']:
        print("WARNINGS:")
        for warn in results['warnings']:
            print(f"  ⚠ {warn}")
        print()
    
    print("DETAILS:")
    for key, value in results['details'].items():
        print(f"  {key}: {value}")
    
    print("=" * 60)
    
    sys.exit(0 if results['passed'] else 1)


if __name__ == '__main__':
    main()
```

### Execute verification

After creating the output workbook:

1. **Define the verification specification** as a JSON object with:
   - `required_sheets`: List of sheet names that must exist
   - `required_columns`: Dict mapping sheet names to lists of required column headers
   - `expected_row_counts`: Dict mapping sheet names to minimum expected row counts
   - `expected_sample_count`: Number of rows that should be marked/selected
   - `sample_mark_column`: Name of the column used to mark selected rows
   - `sample_mark_value`: Value indicating a row is marked (e.g., "Yes", "TRUE", 1)
   - `sampling_criteria`: List of values that must appear in marked rows
   - `criterion_fallback_permitted`: Boolean - whether missing criteria are acceptable

2. **Write the verification script** to a file in the workspace

3. **Execute the script** using execute_code_sandbox or run_shell:
   ```bash
   python3 verify_workbook.py /path/to/output.xlsx '<spec_json>'
   ```

4. **Interpret results**:
   - If `Status: PASSED` with no errors: Proceed to report completion
   - If `Status: FAILED` with errors: Review errors, fix the workbook, re-verify
   - If script execution fails: Debug the script or try alternative verification

### Verification go/no-go decision

**Only report completion if:**

- The verification script executes successfully
- The verification returns `Status: PASSED`
- All required sheets are present with exact names
- All required columns are populated (not all blank)
- Row counts meet specifications
- Sample counts match expectations
- All sampling criteria are represented (or fallback is explicitly permitted)

**If verification fails:**

1. Review the specific errors reported
2. Fix the identified issues in the workbook
3. Re-run the verification script
4. Only report completion after verification passes

### Independent verification requirement

Post-write verification must include an independent direct file read of the saved workbook from the anchored workspace path.

- Do not rely only on delegated-agent summaries, success messages, or memory of what was written
- The Python verification script performs an independent verification by directly loading the workbook from disk
- If the verification script fails to load or verify the workbook, do not declare completion until the issue is resolved
- If a delegated tool performed the edit, the verification script separately confirms the resulting file

### Minimum verification checklist

At minimum, the verification script must validate:

- [ ] The expected workbook file exists at the output path
- [ ] Required tab names are present with exact spelling
- [ ] Required columns exist in each relevant sheet
- [ ] Required columns are populated (not all blank/null)
- [ ] Relevant sheets contain expected row counts
- [ ] Sample-mark counts match the requested count (if applicable)
- [ ] Every user-mandated sampling criterion is represented (if applicable)
- [ ] No required source sheets disappeared during the write

## Error handling

### If verification script fails to execute

1. Check that openpyxl is available (`pip install openpyxl` if needed)
2. Verify the workbook path is correct and accessible
3. Try running the script with explicit error output
4. If Python execution is unavailable, use an alternative method but document the limitation

### If verification finds errors

1. Document each error clearly
2. Determine the root cause (wrong sheet name? missing column? wrong count?)
3. Fix the workbook
4. Re-run verification
5. Iterate until verification passes

### If workbook cannot be verified

If the verification script cannot load or inspect the workbook:

1. Confirm the file exists at the expected path
2. Check file permissions
3. Verify the file is a valid Excel workbook (not corrupted)
4. Try opening with alternative methods
5. If still failing, report the issue and do not claim completion

## Completion criteria

A task using this workflow is complete only when:

1. The workbook has been created or modified as requested
2. The deterministic Python verification script has been executed
3. Verification returns `Status: PASSED` with no errors
4. Any warnings have been reviewed and deemed acceptable
5. The verified workbook is at the correct anchored workspace path

**Never report completion without successful Phase 4 verification.**
