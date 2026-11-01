---
name: spreadsheet-direct-verification
description: Use a deterministic Python/openpyxl inspection pass to verify workbook structure, counts, marked rows, and output-file existence when delegated spreadsheet summaries seem inconsistent.
---

# Spreadsheet Direct Verification

Use this workflow when a higher-level or delegated agent has interacted with spreadsheet files, but its summaries or claims are inconsistent, incomplete, or hard to trust.

The goal is to replace uncertain narrative summaries with a direct, reproducible inspection from disk before you finalize your answer.

## When to use this

Apply this skill when any of the following happen:

- A delegated agent reports conflicting workbook facts across messages
- Sheet names, row counts, headers, or output paths are inconsistent
- The agent says a file was created or modified, but did not verify it from disk
- The task depends on exact workbook contents, especially after edits
- You need a trustworthy final summary of what actually exists on disk

## Core principle

When spreadsheet state matters, trust a deterministic script over a conversational summary.

Inspect the workbook directly with Python and `openpyxl`, and print a compact report covering:

- workbook path
- whether the file exists
- sheet names
- per-sheet headers
- per-sheet row counts
- totals for "marked" rows, if relevant to the task
- whether expected output files exist

Use the script output as the basis for your final response.

## Workflow

1. Identify the workbook(s) and expected output file(s).
2. Determine what "marked row" means for the task.
   - Examples:
     - rows with a non-empty marker column
     - rows with `x`, `yes`, `true`, or `1`
     - rows highlighted by a prior workflow that also wrote a marker column
3. Run a direct Python inspection on the files from disk.
4. Compare the script output against any delegated summary.
5. If they differ, treat the script as authoritative.
6. Only then provide the final user-facing summary.

## Minimum verification checklist

Before finalizing, verify and record:

- exact input workbook path inspected
  - exact output workbook path inspected
  - whether each file exists
  - all sheet names in each workbook
  - header row for each relevant sheet
  - number of non-empty data rows per sheet
  - number of marked rows per sheet, if applicable
  - whether modifications appear in the saved output file

## Production planning validation checklist

For manufacturing and production planning spreadsheets, extend the verification to include:

1. **Max daily minutes per press**: Verify that no press exceeds the daily labor minute limit (e.g., 450 minutes per day per press).
   - Sum labor minutes grouped by press and date
   - Flag any press-date combinations exceeding the threshold

2. **Max weekly hours per team member**: Verify that no team member exceeds weekly hour limits (e.g., 37.5 hours per week).
   - Sum assigned hours grouped by team member and week
   - Flag any team-member-week combinations exceeding the threshold

3. **Required roles per press**: Verify that each press assignment includes all required roles (e.g., 1 operator + 1 water spider per press).
   - Count distinct role assignments per press per shift or date
   - Flag any press assignments missing required roles

4. **Minimum changeover requirements per press**: Verify that required changeovers are scheduled (e.g., minimum 3 changeovers per press per week).
   - Count changeover events grouped by press and time period
   - Flag any press-period combinations below the minimum threshold

### Example production planning verification script extension

Extend the base inspection script with these production-specific checks:

```python
# Add to the INPUTS list if you have separate labor/assignment sheets
# Extend MARKER_CANDIDATES for production-specific columns
PRODUCTION_MARKER_CANDIDATES = MARKER_CANDIDATES | {"press", "team_member", "role", "changeover", "date", "week"}

# Production constraint thresholds (adjust per task)
MAX_DAILY_MINUTES_PER_PRESS = 450
MAX_WEEKLY_HOURS_PER_MEMBER = 37.5
REQUIRED_ROLES_PER_PRESS = {"operator", "water_spider"}  # or {"Operator", "Water Spider"}
MIN_CHANGEOVERS_PER_PRESS_PER_WEEK = 3

def validate_production_constraints(ws, headers):
    """Return a dict of constraint violations found in the worksheet."""
    violations = {
        "daily_minutes_exceeded": [],
        "weekly_hours_exceeded": [],
        "missing_roles": [],
        "insufficient_changeovers": [],
    }
    
    if not headers:
        return violations
    
    normalized_headers = [normalize(h).lower() for h in headers]
    
    # Find column indices
    press_idx = next((i for i, h in enumerate(normalized_headers) if "press" in h), None)
    member_idx = next((i for i, h in enumerate(normalized_headers) if "member" in h or "team" in h), None)
    role_idx = next((i for i, h in enumerate(normalized_headers) if "role" in h), None)
    minutes_idx = next((i for i, h in enumerate(normalized_headers) if "minute" in h or "labor" in h), None)
    date_idx = next((i for i, h in enumerate(normalized_headers) if "date" in h), None)
    changeover_idx = next((i for i, h in enumerate(normalized_headers) if "changeover" in h), None)
    
    # Aggregate data
    press_daily_minutes = {}  # (press, date) -> total minutes
    member_weekly_hours = {}  # (member, week) -> total hours
    press_roles = {}  # (press, date) -> set of roles
    press_changeovers = {}  # (press, week) -> count
    
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not any(v is not None and str(v).strip() != "" for v in row):
            continue
        
        press = normalize(row[press_idx]) if press_idx is not None and press_idx < len(row) else None
        member = normalize(row[member_idx]) if member_idx is not None and member_idx < len(row) else None
        role = normalize(row[role_idx]).lower() if role_idx is not None and role_idx < len(row) else None
        minutes = float(row[minutes_idx]) if minutes_idx is not None and minutes_idx < len(row) and row[minutes_idx] else 0
        date_val = row[date_idx] if date_idx is not None and date_idx < len(row) else None
        is_changeover = row[changeover_idx] if changeover_idx is not None and changeover_idx < len(row) else None
        
        # Derive week from date if available
        week = None
        date_str = ""
        if date_val:
            try:
                from datetime import datetime
                if isinstance(date_val, datetime):
                    week = date_val.isocalendar()[1]
                date_str = str(date_val)
            except:
                date_str = str(date_val) if date_val else ""
                week = None
        else:
            date_str = ""
        
        # Aggregate daily minutes per press
        if press and minutes:
            key = (press, date_str)
            press_daily_minutes[key] = press_daily_minutes.get(key, 0) + minutes
        
        # Aggregate weekly hours per member (assume minutes -> hours)
        if member and minutes and week:
            key = (member, week)
            member_weekly_hours[key] = member_weekly_hours.get(key, 0) + minutes / 60
        
        # Track roles per press per date
        if press and role and date_str:
            key = (press, date_str)
            if key not in press_roles:
                press_roles[key] = set()
            press_roles[key].add(role)
        
        # Count changeovers per press per week
        if press and is_changeover and week:
            if is_truthy(is_changeover) or str(is_changeover).lower() in {"yes", "true", "1", "y", "x"}:
                key = (press, week)
                press_changeovers[key] = press_changeovers.get(key, 0) + 1
    
    # Check violations
    for (press, date), total in press_daily_minutes.items():
        if total > MAX_DAILY_MINUTES_PER_PRESS:
            violations["daily_minutes_exceeded"].append(f"{press} on {date}: {total} min")
    
    for (member, week), total in member_weekly_hours.items():
        if total > MAX_WEEKLY_HOURS_PER_MEMBER:
            violations["weekly_hours_exceeded"].append(f"{member} week {week}: {total:.1f} hrs")
    
    for (press, date), roles in press_roles.items():
        missing = REQUIRED_ROLES_PER_PRESS - roles
        if missing:
            violations["missing_roles"].append(f"{press} on {date}: missing {missing}")
    
    for (press, week), count in press_changeovers.items():
        if count < MIN_CHANGEOVERS_PER_PRESS_PER_WEEK:
            violations["insufficient_changeovers"].append(f"{press} week {week}: {count} changeovers")
    
    return violations
```

When validating production planning workbooks, run this extended script and report any constraint violations found.

## Recommended approach

Use a standalone Python script snippet instead of relying on ad hoc tool narration. This makes the inspection:

- reproducible
- easy to rerun
- less prone to memory or interpretation errors
- suitable for attaching exact findings to the final answer

## Example inspection script

Adapt this script to the task. It is intentionally simple and explicit.

```python
from pathlib import Path
from openpyxl import load_workbook

INPUTS = [
    "input.xlsx",
    "output.xlsx",
]

MARKER_CANDIDATES = {"mark", "marked", "flag", "selected", "include", "status"}

TRUTHY = {"x", "yes", "true", "1", "y"}

def normalize(value):
    if value is None:
        return ""
    return str(value).strip()

def is_truthy(value):
    return normalize(value).lower() in TRUTHY

def first_nonempty_row(ws, max_scan=10):
    for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, max_scan), values_only=True):
        if any(v is not None and str(v).strip() != "" for v in row):
            return list(row)
    return []

def data_row_count(ws):
    count = 0
    for row in ws.iter_rows(min_row=2, values_only=True):
        if any(v is not None and str(v).strip() != "" for v in row):
            count += 1
    return count

def marked_row_count(ws, headers):
    if not headers:
        return 0, None
    normalized_headers = [normalize(h).lower() for h in headers]
    marker_idx = None
    for i, h in enumerate(normalized_headers):
        if h in MARKER_CANDIDATES:
            marker_idx = i
            break
    if marker_idx is None:
        return 0, None

    count = 0
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not any(v is not None and str(v).strip() != "" for v in row):
            continue
        value = row[marker_idx] if marker_idx < len(row) else None
        if is_truthy(value):
            count += 1
    return count, headers[marker_idx]

for file_str in INPUTS:
    path = Path(file_str)
    print(f"FILE: {path}")
    print(f"EXISTS: {path.exists()}")

    if not path.exists():
        print()
        continue

    wb = load_workbook(path, data_only=False)
    print(f"SHEETS: {wb.sheetnames}")

    for ws in wb.worksheets:
        headers = first_nonempty_row(ws)
        rows = data_row_count(ws)
        marked_count, marker_name = marked_row_count(ws, headers)

        print(f"  SHEET: {ws.title}")
        print(f"    HEADERS: {headers}")
        print(f"    DATA_ROWS: {rows}")
        if marker_name is not None:
            print(f"    MARKER_COLUMN: {marker_name}")
            print(f"    MARKED_ROWS: {marked_count}")
        else:
            print("    MARKER_COLUMN: <not found>")
    print()
```

## How to adapt the script safely

Adjust only the task-specific parts:

- `INPUTS`: the exact workbook paths to verify
- `MARKER_CANDIDATES`: possible names of the marker column
- `TRUTHY`: accepted values that count as marked
- row counting logic, if headers do not start on row 1
- any expected output file paths to confirm

Do not weaken the script by replacing direct inspection with summary text from another agent.

## If the workbook layout is unusual

Handle these cases explicitly:

### Header row is not row 1

Search the first few rows for the first non-empty row or the row containing expected header names.

### Multiple tables in one sheet

Limit verification to the relevant region and state that assumption.

### Marked rows are based on formatting, not a marker column

If required, inspect formatting directly with `openpyxl`, but prefer a visible marker column whenever possible. If formatting-based logic is necessary, document exactly what you counted.

### Formula-driven workbooks

If you need displayed values, consider `load_workbook(..., data_only=True)` in a separate pass. If you need formulas and saved values, inspect both modes and note which one you used.

## Final response pattern

After running the verification script, summarize findings using facts from the output only. Include:

- file verified
- sheets present
- row counts
- marked-row totals
- output file existence
- any discrepancy discovered between delegated claims and actual disk state

Example final phrasing:

- "I verified the workbook directly from disk with `openpyxl`."
- "The output file exists at `...`."
- "The workbook contains sheets `...`."
- "Sheet `...` has `N` data rows and `M` marked rows."
- "A prior delegated summary was inconsistent, so I used the direct inspection results as authoritative."

## Decision rule

If delegated spreadsheet summaries and direct inspection disagree:

- trust the direct inspection
- cite the discrepancy internally
- base the final answer on the deterministic script output

## Anti-patterns to avoid

Do not:

- finalize based only on a delegated agent's prose summary
- assume a workbook was saved without checking file existence
- quote row counts without reading the workbook from disk
- report sheet names from memory when they can be enumerated directly
- mix speculative statements with verified spreadsheet facts

## Why this skill is reusable

This pattern applies broadly to spreadsheet tasks involving:

- workbook edits
- filtering or marking rows
- creating output copies
- validating agent-performed file changes
- reconciling conflicting intermediate reports

Whenever workbook facts are important and agent summaries are unreliable, a direct `openpyxl` verification pass is the safe fallback.
