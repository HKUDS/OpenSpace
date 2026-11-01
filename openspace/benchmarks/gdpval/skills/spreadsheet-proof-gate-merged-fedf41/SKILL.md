---
name: spreadsheet-create-verify-gate
description: Create or edit Excel workbooks with openpyxl-specific pitfall handling, anchored preflight audit, and deterministic post-write proof that every criterion is satisfied
---

# Spreadsheet Create/Verify Gate

Use this skill when you must **create from scratch** or **modify** an Excel workbook and the result must be proven against a user specification rather than merely described.

This skill extends the spreadsheet-proof-gate methodology with explicit support for:

- **Workbook creation from scratch** (not just editing existing files)
- **openpyxl-specific pitfall handling** (MergedCell, worksheet creation, formula handling)
- **Tool failure resilience** with clear escalation paths
- **Pre-edit audit phases** that catch common openpyxl errors before they occur

This is a workflow skill requiring a direct Python verification pass with `openpyxl` before reporting success. **The verification script must be saved to a .py file and executed via run_shell (NOT execute_code_sandbox).**

## When to use

Use this skill when ANY of the following are true:

### Creation scenarios:
- You must create a new Excel workbook from scratch
- No existing template is provided
- You need to establish the initial workbook structure

### Editing scenarios:
- The workbook path must be discovered
- Multiple similar Excel files may exist
- The user requires exact sheet names, columns, counts, or samples
- Prior summaries are inconsistent or not trustworthy
- Exact post-edit workbook state matters

### General:
- You must prove that mandatory criteria were satisfied
- A delegated agent may perform spreadsheet work
- Tool instability has been observed in prior attempts

## Core rules

### Rule 1: Creation vs. Editing distinction

Before starting, determine which path applies:

**Creation path** (new workbook):
- No existing workbook at the target path
- You are establishing the initial structure
- Pre-edit audit focuses on workspace validation and structure planning

**Editing path** (existing workbook):
- Existing workbook at the target path
- You are modifying structure or content
- Pre-edit audit includes full workbook structure inspection

### Rule 2: Openpyxl pitfall awareness

The following openpyxl-specific issues must be handled explicitly:

**MergedCell errors:**
- Accessing `.value` on merged cells raises `MergedCell` exception
- **Solution**: Check `ws.merged_cells.ranges` and handle merged regions specially
- **Pre-edit check**: Identify all merged ranges before accessing cell values

**Worksheet creation patterns:**
- `wb.create_sheet()` is the correct method, not direct assignment
- Sheet names must be unique and ≤31 characters
- Invalid characters (`\ / ? * [ ]`) are not allowed in sheet names

**Formula handling:**
- Set `data_only=False` when loading to preserve formulas
- Use `wb.save()` after modifications, not `wb.close()`
- Formula cells need explicit formula strings, not calculated values

**Cell value type handling:**
- Dates, numbers, and strings may need explicit type handling
- Use `cell.value` not direct cell access for safety

### Rule 3: Never finalize from narrative summary alone

A workbook task is only complete when:

1. The target workbook path was identified (for editing) or chosen (for creation)
2. Pre-edit structure matched the intended operation
3. The edit/creation was performed on the correct file
4. The saved workbook was directly re-read from disk
5. Every requested criterion was checked in a deterministic verification report
6. Each criterion is marked as one of: `PASS`, `FAIL`, or `UNAVAILABLE-IN-SOURCE`
7. Any non-pass result is explicitly reconciled before finalizing

### Rule 4: Workspace anchoring is absolute

All discovery, reads, writes, and verification must be anchored to the exact workspace path provided for the task.

- Treat the provided workspace path as the authoritative root
- Resolve workbooks to exact paths under that root
- Do not silently switch to similarly named files elsewhere
- If the anchored file cannot be found for editing, stop rather than guessing
- For creation, ensure the output directory exists

## Outcome contract

Your output should be based on proof, not inference.

For every task using this workflow, maintain these artifacts internally:

1. **Workbook inventory**: path, existence status, sheet structure (for editing) OR planned structure (for creation)
2. **Pre-edit audit checklist**: workbook structure before modification
3. **Post-write proof checklist**: deterministic verification of every criterion

The post-write proof checklist is decisive.

# Workflow

## Phase 1: Determine creation vs. editing path

### Step 1.1: Check for existing workbook

```python
from pathlib import Path
target_path = Path("path/to/workbook.xlsx")
exists = target_path.exists()
```

**If `exists == False` and user expects to create new workbook:**
- Follow the **creation path** (Phase 2A)

**If `exists == True` and user expects to edit:**
- Follow the **editing path** (Phase 2B)

**If `exists == False` but user expects to edit:**
- **STOP** and report: workbook not found at anchored path
- Do not proceed with creation unless user explicitly requests it

### Step 1.2: Validate workspace for creation

For creation path, confirm:

- Output directory exists or can be created
- No conflicting file at target path
- Sufficient permissions inferred from workspace context

## Phase 2A: Creation path pre-audit

### Step 2A.1: Plan workbook structure

Before writing any code, document:

- All sheet names to be created
- Column headers for each sheet
- Expected row counts
- Any formulas to be inserted
- Any merged cell regions
- Any special formatting requirements

### Step 2A.2: Check for openpyxl pitfalls

**Merged cell planning:**
- If merged cells are required, plan the merge ranges explicitly
- Ensure merged ranges don't overlap
- Plan to populate only the top-left cell of merged regions

**Sheet name validation:**
- All names ≤31 characters
- No invalid characters: `\ / ? * [ ]`
- All names unique (case-sensitive in openpyxl)

**Data type planning:**
- Dates: use Python `datetime` objects, not strings
- Numbers: use numeric types, not formatted strings
- Formulas: use Excel formula syntax as strings

## Phase 2B: Editing path pre-audit

### Step 2B.1: Discover and confirm workbook identity

Search under the exact workspace root for plausible Excel files:
- `.xlsx`, `.xlsm`, `.xls`

Confirm:
- Exact anchored path
- File existence
- Workbook readability (can be loaded by openpyxl)

### Step 2B.2: Openpyxl-safe structure inspection

```python
from openpyxl import load_workbook

# Always use data_only=False to preserve formulas
wb = load_workbook(target_path, data_only=False)

# Get all sheet names
sheet_names = wb.sheetnames

# For each sheet, inspect safely
for ws in wb.worksheets:
    # Check for merged cells BEFORE accessing values
    merged_ranges = list(ws.merged_cells.ranges)
    
    # Get headers safely
    headers = []
    for cell in ws[1]:  # Row 1
        # Check if cell is part of a merged range
        is_merged = any(cell.coordinate in mr for mr in merged_ranges)
        if is_merged:
            # Get value from top-left of merged range
            for mr in merged_ranges:
                if cell.coordinate in mr:
                    headers.append(ws[mr.min_row][mr.min_col - 1].value)
                    break
        else:
            headers.append(cell.value)
```

### Step 2B.3: Identity consistency check

If you inspect the workbook multiple times:
- Inspections must agree on: path, sheet names, row counts, headers
- If they disagree, treat workbook identity as unconfirmed
- Do not edit until reconciled

## Phase 3: Convert requirements into explicit criteria

Before editing/creating, extract every verifiable requirement.

**For creation tasks:**
- `output-exists: workbook created at <path>`
- `required-sheet: <name> exists`
- `required-column: Sheet <name> has column "<column>"`
- `row-count: Sheet <name> has exactly N data rows`
- `data-populated: Column <X> contains values for all data rows`
- `formula: Column <Y> contains formulas`
- `merged-region: Sheet <name> has merged range <range>`

**For editing tasks:**
- All creation criteria, plus:
- `preservation: Sheet <name> still exists unchanged`
- `unchanged-sheets: <list> were not modified`
- `macro-preservation: VBA macros preserved (if applicable)`

### Good criterion examples:
- `required-sheet: Summary exists`
- `required-column: Sheet Sample has column "Selected"`
- `row-count: exactly 25 data rows in Sample`
- `formula: column H contains formulas for all populated rows`
- `merged-region: Summary A1:B1 is merged`

### Bad criterion examples:
- `workbook looks right`
- `sampling seems okay`
- `most tabs present`

## Phase 4: Pre-edit go/no-go gate

Proceed only if ALL are true:

**For creation:**
- Output directory is valid
- Planned sheet names are valid (length, characters, uniqueness)
- Planned structure is coherent
- No conflicting file at target path

**For editing:**
- Workbook identity is confirmed
- Target sheet is unambiguous
- Required columns exist or can be added safely
- Row counts are plausible
- No unresolved inspection conflicts remain
- Requested edit is possible from available source data

**For both:**
- Openpyxl pitfalls have been planned for (merged cells, etc.)
- All required criteria are checkable

Otherwise, stop and report the mismatch.

## Phase 5: Perform the creation/edit

### For creation:

```python
from openpyxl import Workbook

wb = Workbook()
# Remove default sheet if creating custom structure
if 'Sheet' in wb.sheetnames:
    del wb['Sheet']

# Create sheets with validated names
for sheet_name in planned_sheets:
    ws = wb.create_sheet(title=sheet_name)
    # Populate headers
    for col_idx, header in enumerate(headers, start=1):
        ws.cell(row=1, column=col_idx, value=header)
    # Populate data rows
    for row_idx, row_data in enumerate(data_rows, start=2):
        for col_idx, value in enumerate(row_data, start=1):
            ws.cell(row=row_idx, column=col_idx, value=value)
    # Apply merges if planned
    for merge_range in planned_merges:
        ws.merge_cells(merge_range)

wb.save(output_path)
```

### For editing:

```python
from openpyxl import load_workbook

wb = load_workbook(target_path, data_only=False)
ws = wb[sheet_name]

# Handle merged cells when reading
merged_ranges = list(ws.merged_cells.ranges)

# When modifying, be careful with merged regions
# Don't write to cells within merged ranges (except top-left)

# Modify content
ws.cell(row=row, column=col, value=new_value)

# Add new sheet if needed
if new_sheet_name not in wb.sheetnames:
    wb.create_sheet(title=new_sheet_name)

wb.save(output_path)
```

### Record what was done:

- Workbook path edited/created
- Output path written
- Sheets modified or created
- Columns added or populated
- Rows added or updated
- Formulas inserted
- Merged regions applied
- Any assumptions made

## Phase 6: Deterministic post-write proof

**This phase is mandatory.**

After saving, directly inspect the saved workbook from disk with Python and `openpyxl`. Do not finalize from memory, delegated prose, or a generic success message.

### Verification script procedure

```python
from pathlib import Path
from openpyxl import load_workbook

WORKBOOK = Path("TARGET.xlsx")
TRUTHY = {"x", "yes", "true", "1", "y"}

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

def safe_cell_value(ws, coord, merged_ranges=None):
    """Safely get cell value, handling merged cells"""
    cell = ws[coord]
    if merged_ranges:
        for mr in merged_ranges:
            if coord in mr:
                # Return value from top-left of merged range
                return ws[mr.min_row][mr.min_col - 1].value
    return cell.value

print(f"WORKBOOK: {WORKBOOK}")
print(f"EXISTS: {WORKBOOK.exists()}")

if not WORKBOOK.exists():
    print("CRITERION|output-exists|FAIL|Workbook file missing")
    raise SystemExit(0)

wb = load_workbook(WORKBOOK, data_only=False)
print(f"SHEETS: {wb.sheetnames}")

for ws in wb.worksheets:
    merged_ranges = list(ws.merged_cells.ranges)
    headers = first_nonempty_row(ws)
    print(f"SHEET|{ws.title}|HEADERS|{headers}")
    print(f"SHEET|{ws.title}|DATA_ROWS|{data_rows(ws)}")
    print(f"SHEET|{ws.title}|MERGED_RANGES|{[str(mr) for mr in merged_ranges]}")

# Check each criterion
# Example:
# required_sheets = ["Summary", "Data"]
# for sheet in required_sheets:
#     status = "PASS" if sheet in wb.sheetnames else "FAIL"
#     print(f"CRITERION|required-sheet-{sheet}|{status}|Sheet {sheet} {'found' if status == 'PASS' else 'missing'}")

# Print all criteria with status
for criterion_line in criteria:
    print(criterion_line)
```

### Required proof outputs

The verification must report:
- Inspected file path
- Whether the file exists
- Workbook sheet names
- Per-sheet headers for relevant sheets
- Per-sheet non-empty row counts
- Merged cell ranges (if applicable)
- Formula presence (if relevant)
- Preservation of required source sheets (for editing)
- Criterion checklist with status for each item

### Status vocabulary

Each criterion must end in exactly one status:

- `PASS` — requirement satisfied by direct inspection
- `FAIL` — requirement not satisfied
- `UNAVAILABLE-IN-SOURCE` — requirement could not be satisfied because required source information was absent

Do not use softer wording like:
- looks okay
- appears complete
- probably satisfied
- seems unavailable

## Finalization gate

You may finalize only when every required criterion is either:

- `PASS`, or
- `UNAVAILABLE-IN-SOURCE` with explicit reconciliation

You must not finalize when:

- Any mandatory criterion remains `FAIL`
- A claimed output file was not verified from disk
- The workbook path is uncertain
- Post-write proof was not run
- You only have delegated summary evidence
- Requested counts differ from actual counts without reconciliation
- Required sheet names or headers are missing
- Openpyxl errors occurred during verification (indicates file corruption or structural issues)

If direct proof says the workbook is incomplete, that proof is authoritative.

## Error handling and resilience

### Tool failure patterns

If `execute_code_sandbox` or `run_shell` returns opaque errors:

1. **Retry once** with the same command (may be transient)
2. **If still failing**, try alternative approach:
   - If `execute_code_sandbox` fails, try `run_shell` with python script
   - If `run_shell` fails, try `execute_code_sandbox`
   - If both fail, use `shell_agent` for autonomous execution

### openpyxl-specific errors

**MergedCell AttributeError:**
- **Symptom**: Accessing `.value` on merged cells raises exception
- **Fix**: Check `ws.merged_cells.ranges` before accessing; get value from top-left cell

**Invalid sheet name:**
- **Symptom**: ValueError on sheet creation
- **Fix**: Validate names before creation (≤31 chars, no `\ / ? * [ ]`)

**Formula parsing errors:**
- **Symptom**: Formula not calculating correctly
- **Fix**: Use `data_only=False` when loading; verify formula syntax is Excel-compatible

**File lock/permission errors:**
- **Symptom**: Cannot save or load workbook
- **Fix**: Ensure file is not open in another process; verify directory permissions

### Escalation to shell_agent

If spreadsheet operations fail repeatedly (>2 attempts):

```python
shell_agent(
    task="""
    Create/modify Excel workbook at <exact_path>.
    
    Required structure:
    - Sheets: <list>
    - Columns per sheet: <specification>
    - Data to populate: <details>
    - Formulas: <specification>
    - Merged regions: <if any>
    
    Handle openpyxl pitfalls:
    - Check merged_cells.ranges before accessing cell values
    - Validate sheet names (31 chars max, no special chars)
    - Use data_only=False when loading to preserve formulas
    
    Provide: created file path, sheets created, row counts, any errors encountered.
    """,
    timeout=300
)
```

Then verify shell_agent output with the Phase 6 proof procedure.
**Tool requirement**: Write verification script to a .py file and execute via `run_shell`, NOT `execute_code_sandbox`. This ensures reliability when sandbox providers are unavailable.
**Step 6.1: Write script to disk first**

Create a Python file (e.g., `verify_workbook.py`) with the verification logic, then execute it via shell.

**Step 6.2: Execute via run_shell**

```bash
python verify_workbook.py
```

**Step 6.3: Parse verification output**

The script must output explicit PASS/FAIL markers for each criterion.
