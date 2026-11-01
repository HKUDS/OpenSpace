---
name: resilient-research-workflow
description: Unified workflow that delegates failed web searches to shell_agent for resilient data gathering, then applies anchored spreadsheet proof gates for verified Excel output
---

# Resilient Research-to-Spreadsheet Workflow

Use this skill when you must gather web-based information AND produce verified Excel workbooks, with particular emphasis on handling tool failures gracefully and proving final outputs against explicit criteria.

This skill combines:

- **Phase A**: Resilient web research with automatic failure detection and shell_agent delegation
- **Phase B**: Research data validation before spreadsheet integration
- **Phase C**: Anchored spreadsheet proof gate for workbook creation/editing
- **Cross-phase gates**: Explicit handoff criteria between research and spreadsheet phases

This is an end-to-end workflow skill. It requires:
- autonomous retry logic for web research (via shell_agent when needed)
- direct Python verification of research outputs
- deterministic spreadsheet proof methodology before finalizing

## When to use

Use this skill when ANY of the following are true:

### Research-phase triggers:
- You need current web-based data (prices, market info, competitor analysis, etc.)
- Prior search_web calls have failed or returned inconsistent results
- The task requires multi-step data collection from multiple sources
- You need resilient data gathering that can adapt to tool instability

### Spreadsheet-phase triggers:
- You must create or modify an Excel workbook based on research data
- The user requires exact sheet names, columns, counts, or samples
- Prior summaries are inconsistent or not trustworthy
- Exact post-edit workbook state matters
- You must prove that mandatory criteria were satisfied

### Both phases:
- A delegated agent may perform parts of the work
- Tool instability has been observed in the task context
- The task has failed previously due to tool errors

## Core rules

### Rule 1: Detect and escalate research failures

When `search_web` returns errors like:
- "unknown error"
- Empty or incomplete results
- Consistent failures across multiple queries (>2 failures)

**Immediately** delegate to shell_agent rather than retrying the same failing tool.

### Rule 2: Never finalize from narrative summary alone

A spreadsheet task is only complete when:
1. The target workbook was identified from the anchored workspace path
2. Pre-edit structure matched the intended operation
3. The edit was performed on the correct file
4. The saved workbook was directly re-read from disk
5. Every requested criterion was checked in a deterministic verification report
6. Each criterion is marked as one of: `PASS`, `FAIL`, or `UNAVAILABLE-IN-SOURCE`
7. Any non-pass result is explicitly reconciled before finalizing

### Rule 3: Workspace anchoring is absolute

All discovery, reads, writes, and verification must be anchored to the exact workspace path provided for the task.

- Treat the provided workspace path as the authoritative root
- Resolve workbooks to exact paths under that root
- Do not silently switch to similarly named files elsewhere
- Do not trust delegated tool reports without independent confirmation
- If the anchored file cannot be found, stop rather than guessing

### Rule 4: Research criteria flow into spreadsheet criteria

The data you gather in Phase A becomes part of the verification criteria in Phase C. Track:
- What data was required vs. what was obtained
- Which sources were used
- Which data points could not be obtained (and why)
- How missing data affects spreadsheet requirements

## Outcome contract

Your output should be based on proof, not inference.

For every task using this workflow, maintain these artifacts internally:

1. **Research inventory**: sources tried, data obtained, failures encountered
2. **Pre-edit audit checklist**: workbook structure before modification
3. **Post-write proof checklist**: deterministic verification of every spreadsheet criterion

The post-write proof checklist is decisive for spreadsheet claims.
The research inventory is decisive for data coverage claims.

# Workflow

## Phase A: Resilient Web Research

### Step A1: Attempt direct search

First, try `search_web` with your primary query:

```python
search_web(query="your specific query with date/context")
```

### Step A2: Detect failure patterns

Monitor for these failure signals:
- Return error messages ("unknown error", "connection failed", etc.)
- Empty results or clearly incomplete data
- Repeated failures on related queries

**Failure threshold**: If `search_web` fails 2+ times on the same or related queries, escalate to shell_agent.

### Step A3: Delegate to shell_agent on failure

When failure threshold is reached, create a comprehensive shell_agent task:

```python
shell_agent(
    task="""
    Gather [SPECIFIC DATA TYPE] for [SPECIFIC PURPOSE].
    
    Required information:
    1. [Item 1 with specificity - e.g., "Current WTI crude oil price as of today"]
    2. [Item 2]
    3. [Item 3]
    
    Success criteria:
    - Data from at least 2 independent sources
    - Timestamp for when data was collected
    - Source URLs for verification
    
    Handle errors by trying alternative approaches (different APIs, direct URL fetching, etc.)
    Provide structured output with source citations.
    """,
    timeout=300
)
```

**Key task formulation principles:**
- Specify the **what** (data needed) not the **how** (specific tools to use)
- Include explicit success criteria and output format expectations
- Allow sufficient timeout for multi-step execution (300+ seconds typical)
- Let shell_agent decide whether to use Python, curl, requests, or other approaches

### Step A4: Verify research results

Check shell_agent output for:
- Complete data collection (not partial results)
- Multiple sources cited (indicates thorough searching)
- Structured, usable output format
- Evidence of error handling (mentions of retry attempts, alternatives tried)

**Gate A**: Do not proceed to Phase B until you have verified research results OR have explicitly documented what data could not be obtained and why.

## Phase B: Research Data Validation

Before using research data in spreadsheet work, validate it:

### Check B1: Data completeness

For each required data point:
- Was it obtained? If yes, mark `PASS`. If no, mark `UNAVAILABLE-IN-SOURCE` with reason.
- Is the data recent enough for the task requirements?
- Are the sources credible and verifiable?

### Check B2: Data consistency

If multiple sources were used:
- Do the values align within reasonable tolerance?
- If there are discrepancies, which source takes precedence?
- Document any reconciliation decisions.

### Check B3: Map to spreadsheet criteria

Translate research data into explicit spreadsheet criteria:
- Which sheets will contain this data?
- Which columns need to be populated?
- What formulas or calculations derive from this data?
- What validation rules apply?

**Gate B**: Do not proceed to Phase C until you have a clear mapping from research data to spreadsheet criteria.

## Phase C: Spreadsheet Proof Gate

### Step C1: Discover candidate workbooks

Search under the exact workspace root for plausible Excel files:
- `.xlsx`, `.xlsm`, `.xls`

Prefer files that:
- Match names mentioned by the user
- Live in likely data/output/project folders
- Have relevant modification times
- Contain expected sheet names

If several files are plausible, inspect all before choosing.

### Step C2: Build explicit criteria list

Before editing, convert requirements into checkable criteria:

**Possible criterion types:**
- `required-sheet: <name> exists`
- `required-column: Sheet <name> has column "<column>"`
- `data-populated: Column <X> in Sheet <Y> has values for all data rows`
- `formula: Column <Z> contains formulas for all populated rows`
- `row-count: Sheet <name> has exactly N data rows`
- `preservation: Source sheet <name> still exists`
- `research-derived: Data from <source> appears in <location>`

**Good examples:**
- `required-sheet: Summary exists`
- `required-column: Sheet Sample has column "Selected"`
- `row-count: exactly 25 marked rows in Sample`
- `research-derived: WTI price from shell_agent appears in Summary!B2`

**Bad examples:**
- `workbook looks right`
- `sampling seems okay`
- `most tabs present`

### Step C3: Pre-edit audit

Before any write, inspect the workbook structure:

1. Confirm exact anchored path
2. List all sheet names
3. Identify target sheets
4. Inspect headers
5. Count relevant rows
6. Note formulas, merged cells, tables, filters, protections, or macros if relevant
7. Confirm whether required columns exist already or must be created
8. Confirm whether the planned edit is structurally safe

**Identity consistency check**: If you inspect the workbook multiple times or with multiple tools, the inspections must agree on path, sheet names, row counts, and headers. If they disagree, treat workbook identity as unconfirmed.

### Step C4: Pre-edit go/no-go gate

Proceed only if ALL are true:
- The workbook identity is confirmed
- The target sheet is unambiguous
- Required columns exist or can be added safely
- Row counts are plausible for the requested operation
- No unresolved inspection conflicts remain
- The requested edit is possible from available source data (including research data)

Otherwise, stop and report the mismatch.

### Step C5: Perform the edit

Only after the workbook passes pre-edit audit:

- Edit only the intended workbook
- Preserve untouched sheets unless instructed otherwise
- Preserve names unless renaming was requested
- Preserve formatting/macros/formulas when required
- Keep a clear mapping from user requirements to written cells/rows

Record at minimum:
- Workbook path edited
- Output path written
- Sheets modified
- Columns added or populated
- Rows added or updated
- Formulas inserted
- Any assumptions made

### Step C6: Deterministic post-write proof

**This phase is mandatory.**

After saving, directly inspect the saved workbook from disk with Python and `openpyxl`. Do not finalize from memory, delegated prose, or a generic success message.

### Verification script template

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

criteria = []

print(f"WORKBOOK: {WORKBOOK}")
print(f"EXISTS: {WORKBOOK.exists()}")

if not WORKBOOK.exists():
    print("CRITERION|output-exists|FAIL|Workbook file missing")
    raise SystemExit(0)

wb = load_workbook(WORKBOOK, data_only=False)
print(f"SHEETS: {wb.sheetnames}")

# Add your criterion checks here
# Example:
# required_sheets = ["Summary", "Data"]
# for sheet in required_sheets:
#     status = "PASS" if sheet in wb.sheetnames else "FAIL"
#     print(f"CRITERION|required-sheet-{sheet}|{status}|Sheet {sheet} {'found' if status == 'PASS' else 'missing'}")

for ws in wb.worksheets:
    headers = first_nonempty_row(ws)
    print(f"SHEET|{ws.title}|HEADERS|{headers}")
    print(f"SHEET|{ws.title}|DATA_ROWS|{data_rows(ws)}")

# Print final criterion summary
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
- Marker/selected-row counts if relevant
- Formula presence if relevant
- Preservation of required source sheets
- Existence of requested output files
- Criterion checklist with status for each item

### Status vocabulary

Each criterion must end in exactly one status:
- `PASS` — requirement satisfied by direct inspection
- `FAIL` — requirement not satisfied
- `UNAVAILABLE-IN-SOURCE` — requirement could not be satisfied because required source information was absent

Do not replace these with softer wording like "looks okay", "appears complete", "probably satisfied", etc.

### Step C7: Finalization gate

You may finalize only when EVERY required criterion is either:
- `PASS`, or
- `UNAVAILABLE-IN-SOURCE` with explicit reconciliation

You must NOT finalize when:
- Any mandatory criterion remains `FAIL`
- A claimed output file was not verified from disk
- The workbook path is uncertain
- Post-write proof was not run
- You only have delegated summary evidence
- Requested sample counts and actual marked counts differ without reconciliation
- Required coverage criteria are missing and not repaired
- Required sheet names or headers are missing

**If direct proof says the workbook is incomplete, that proof is authoritative.**

# Troubleshooting

## Shell_agent also fails?

- Increase timeout (try 400-500 seconds)
- Break the task into smaller sub-tasks
- Specify more concrete data sources or APIs to try
- Consider running multiple targeted shell_agent calls for different data categories

## Research data is incomplete?

- Document what was obtained vs. what was not
- Assess whether missing data blocks spreadsheet requirements
- If missing data is critical, mark affected spreadsheet criteria as `UNAVAILABLE-IN-SOURCE`
- If user request allows, proceed with partial data and clearly document gaps

## Spreadsheet verification fails?

- Review the criterion that failed
- Check if it's a legitimate error (requires fix) vs. a misunderstanding (requires criterion adjustment)
- Re-run verification after any fix
- Do not skip verification even if it reveals problems

## Tool instability context

If the task environment shows signs of tool instability (multiple tools failing with "unknown error"):
- Set longer timeouts for shell_agent (300-500 seconds)
- Be prepared for partial data scenarios
- Document all tool failures in your research inventory
- Consider whether the task can be completed with available information

# Related patterns

- Use `execute_code_sandbox` for custom data processing after shell_agent gathers raw data
- Use `create_file` to persist collected research data for downstream tasks
- Pair `read_webpage` with shell_agent for targeted extraction when specific URLs are identified
- For complex spreadsheet tasks, consider running verification as a separate shell_agent invocation if direct Python fails

# Appendix: Research-to-Criteria Mapping Examples

## Example 1: Competitor Price Research

**Research criteria:**
- Competitor A price for Product X
- Competitor B price for Product X
- Competitor A price for Product Y

**Spreadsheet criteria derived:**
- `required-sheet: Competitor_Prices exists`
- `required-column: Sheet Competitor_Prices has columns "Competitor", "Product", "Price"`
- `data-populated: Competitor_Prices has 6 rows (3 products × 2 competitors)`
- `research-derived: Competitor A data appears in rows 2-4`

## Example 2: Market Data Dashboard

**Research criteria:**
- Current WTI crude price
- Current Brent crude price
- Natural gas price
- 10-year Treasury yield

**Spreadsheet criteria derived:**
- `required-sheet: Market_Data exists`
- `required-column: Sheet Market_Data has columns "Metric", "Value", "Source", "Timestamp"`
- `row-count: Market_Data has exactly 4 data rows`
- `research-derived: WTI price from shell_agent appears in Market_Data!B2`
- `research-derived: Source URLs are recorded in Market_Data!Column D`
