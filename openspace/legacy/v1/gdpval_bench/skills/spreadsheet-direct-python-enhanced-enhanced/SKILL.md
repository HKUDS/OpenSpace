---
name: spreadsheet-python-file-exec
name: spreadsheet-validated-execution
description: Execute Python scripts with prerequisite data validation and fallback strategies for inaccessible sources
---

# Direct Python Execution for Spreadsheet Tasks

## When to Use This Skill

Use this skill for spreadsheet operations that require verified source data:

- **Before processing**: Verify data sources are accessible and contain expected data
- Reading or writing complex Excel files with multiple sheets
- Applying formulas, formatting, or data transformations
- Working with `openpyxl`, `pandas`, or similar libraries
- The operation involves multiple steps that could exceed agent step limits
- You need precise control over error handling and debugging
- Complex scripts benefit from file-based execution for better reliability

## Critical: Data Source Validation First

**Always validate data availability before attempting spreadsheet operations.** This prevents wasted iterations on unavailable data.

### Pre-Execution Validation Checklist

1. **Verify source accessibility**: Test connection to data URLs/files before building processing scripts
2. **Confirm data format**: Ensure source data matches expected structure (columns, sheets, file type)
3. **Check data completeness**: Validate required fields/rows are present
4. **Identify fallback sources**: Document alternative data sources if primary is unavailable

### Validation Script Pattern

```python
import sys
import os
from pathlib import Path

def validate_source(source_path, required_fields=None):
    """Validate data source before processing"""
    if not Path(source_path).exists():
        return False, f"Source file not found: {source_path}"
    
    try:
        # Check file is readable and non-empty
        if os.path.getsize(source_path) == 0:
            return False, "Source file is empty"
        
        if required_fields:
            # Validate structure using pandas
            import pandas as pd
            df = pd.read_excel(source_path, nrows=1)
            missing = set(required_fields) - set(df.columns)
            if missing:
                return False, f"Missing required columns: {missing}"
        
        return True, "Source validated"
    except Exception as e:
        return False, f"Validation error: {str(e)}"

# Usage
valid, message = validate_source('input.xlsx', ['ID', 'Date', 'Value'])
if not valid:
    print(f"ABORT: {message}", file=sys.stderr)
    sys.exit(1)
print(f"OK: {message}")
```

### Handling Inaccessible Data Sources

When primary data sources are unavailable:

1. **Report clearly**: Document the specific error (SSL, timeout, file not found)
2. **Attempt fallbacks**: Check alternative sources in priority order:
   - Local cached copies of the data
   - Alternative API endpoints or URLs
   - Different file formats from the same source
   - Contact information for data provider
3. **Graceful degradation**: If partial data is available, document what's missing
4. **Escalation protocol**: For persistent failures, provide:
   - Exact error messages and timestamps
   - URLs/paths that were attempted
   - Workarounds already tried
   - Recommended next steps for human intervention

### Example: Multi-Source Fallback Pattern

```python
import sys
from pathlib import Path

sources = [
    'data/wells_current.xlsx',           # Primary: latest data
    'data/wells_backup.xlsx',            # Fallback 1: backup copy
    'data/wells_archive.xlsx',           # Fallback 2: archived version
    '/cached/wells_data.xlsx',           # Fallback 3: system cache
]

selected_source = None
for source in sources:
    if Path(source).exists():
        selected_source = source
        print(f"Using fallback source: {source}")
        break

if not selected_source:
    print("CRITICAL: No data sources available", file=sys.stderr)
    print("Attempted sources:", file=sys.stderr)
    for s in sources:
        print(f"  - {s}", file=sys.stderr)
    sys.exit(1)

# Proceed with selected_source
```

- Reading or writing complex Excel files with multiple sheets
- Applying formulas, formatting, or data transformations
- Working with `openpyxl`, `pandas`, or similar libraries
- The operation involves multiple steps that could exceed agent step limits
- You need precise control over error handling and debugging
- Complex scripts benefit from file-based execution for better reliability

## Why Direct Execution?

The `shell_agent` tool can:

- Hit maximum step limits on complex multi-step operations
- Produce unexplained errors on formatting operations
- Fail on intricate spreadsheet reads/writes due to iterative parsing
- Fail to parse heredoc syntax correctly, causing 'unknown error' failures

Direct `run_shell` with Python is more reliable because it:

- Executes in a single step with no iteration limits
- Provides clearer, immediate error messages
- Handles complex operations without step constraints
- Gives full control over library imports and execution flow
- Writing scripts to `.py` files first avoids shell_agent parsing issues with heredocs

## How to Use

### Recommended Pattern: Write Script to File First

For complex multi-line scripts, especially when using shell_agent as executor:

```bash
# Step 1: Write the Python script to a file
cat > process_spreadsheet.py << 'EOF'
import openpyxl
from openpyxl import Workbook

# Your spreadsheet code here
wb = openpyxl.load_workbook('file.xlsx')
# ... operations ...
wb.save('output.xlsx')
print('Success')
EOF

# Step 2: Execute the script
python3 process_spreadsheet.py
```

### Alternative Pattern: Inline Heredoc (Simple Scripts Only)

For short, simple scripts when NOT using shell_agent as the executor:

```bash
python3 << 'EOF'
import openpyxl
from openpyxl import Workbook

# Your spreadsheet code here
wb = openpyxl.load_workbook('file.xlsx')
# ... operations ...
wb.save('output.xlsx')
print('Success')
EOF
```

### Example 1: Read and Transform Excel Data

**Write to file first, then execute:**

```python
import pandas as pd

# Load data from specific sheet
df = pd.read_excel('input.xlsx', sheet_name='Revenue')

# Apply transformations
df['Net_Revenue'] = df['Gross_Revenue'] * (1 - df['Tax_Rate'])

# Save results
df.to_excel('output.xlsx', index=False, sheet_name='Processed')
```

### Example 2: Multi-Sheet Operations with openpyxl

**Write to file first, then execute:**

```python
from openpyxl import load_workbook

wb = load_workbook('tour_data.xlsx')

# Iterate through sheets
for sheet_name in wb.sheetnames:
    ws = wb[sheet_name]
    # Apply formatting or calculations
    for row in ws.iter_rows(min_row=2, max_col=5):
        # Process cells
        pass

wb.save('tour_data_processed.xlsx')
```

### Example 3: Complex Formatting Operations

**Write to file first, then execute:**

```python
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment

wb = load_workbook('report.xlsx')
ws = wb.active

# Apply header styling
header_fill = PatternFill(start_color='4472C4', fill_type='solid')
header_font = Font(bold=True, color='FFFFFF')

for cell in ws[1]:
    cell.fill = header_fill
    cell.font = header_font
    cell.alignment = Alignment(horizontal='center')

wb.save('report_formatted.xlsx')
```

### Example 4: Error Handling Pattern

**Write to file first, then execute:**

```python
import sys
from openpyxl import load_workbook

try:
    wb = load_workbook('data.xlsx')
    ws = wb.active
    
    # Your operations here
    value = ws['A1'].value
    
    wb.save('output.xlsx')
    print(f"Success: Processed {ws.max_row} rows")
    
except Exception as e:
    print(f"Error: {str(e)}", file=sys.stderr)
    sys.exit(1)
```

## Best Practices

1. **Prefer file-based execution** for complex scripts: write to `.py` file first, then execute via `run_shell`
2. **Import only needed libraries** to reduce execution time
3. **Print clear success/error messages** for debugging
4. **Save intermediate results** for complex multi-step transformations
5. **Test with small data** before scaling to large spreadsheets
6. **Use pandas for data manipulation** and openpyxl for formatting when both are needed
7. **Clean up temporary script files** after execution if they won't be reused

## When NOT to Use This Skill

- Simple single-cell reads/writes (use shell_agent or basic commands)
- Operations that require interactive user input
- Tasks where you need the agent to iteratively refine the approach

## Common Libraries

| Library | Best For |
|---------|----------|
| `openpyxl` | Reading/writing .xlsx files, formatting, formulas |
| `pandas` | Data manipulation, analysis, merging datasets |
| `xlrd` | Reading older .xls files (read-only) |
| `xlsxwriter` | Creating new .xlsx files with advanced formatting |

## Troubleshooting

**Issue**: Heredoc syntax fails with 'unknown error' when using shell_agent
- **Solution**: Write the Python script to a `.py` file first, then execute it with `python3 script.py`. This pattern is significantly more reliable than inline heredoc execution when shell_agent is the executor.

**Issue**: FileNotFoundError
- **Solution**: Verify the file path is absolute or relative to the working directory

**Issue**: PermissionError
- **Solution**: Ensure the file is not open in another application

**Issue**: MemoryError on large files
- **Solution**: Process data in chunks using pandas `chunksize` parameter

**Issue**: Formatting not applying
- **Solution**: Ensure you're modifying cell styles before saving, and use `.copy()` for style objects

## Data Validation Integration

Combine validation with execution in a single script:

```python
import sys
from pathlib import Path
import pandas as pd

# === PHASE 1: Validate ===
source_file = 'input_data.xlsx'
if not Path(source_file).exists():
    print(f"ERROR: Source not found: {source_file}", file=sys.stderr)
    sys.exit(1)

try:
    df = pd.read_excel(source_file)
    if len(df) == 0:
        print("ERROR: Source file is empty", file=sys.stderr)
        sys.exit(1)
    print(f"Validated: {len(df)} rows found")
except Exception as e:
    print(f"ERROR: Cannot read source: {e}", file=sys.stderr)
    sys.exit(1)

# === PHASE 2: Process ===
df['calculated'] = df['value'] * 1.1
df.to_excel('output.xlsx', index=False)
print("Success: output.xlsx created")
```

**Include validation at the start of every script:**

```python
import sys
from pathlib import Path

# Validate BEFORE any processing
input_file = 'source.xlsx'
if not Path(input_file).exists():
    print(f"FATAL: Input file missing: {input_file}", file=sys.stderr)
    sys.exit(1)

# Now proceed with main logic
from openpyxl import load_workbook
wb = load_workbook(input_file)
# ... rest of script ...
```
1. **Always validate sources first**: Check file existence and readability before processing
2. **Document fallback sources**: Keep a list of alternative data locations
3. **Fail fast on validation errors**: Exit immediately if source data is unavailable
4. **Log validation results**: Include source paths and validation status in output
5. **Print clear success/error messages** for debugging
6. **Save intermediate results** for complex multi-step transformations
7. **Test with small data** before scaling to large spreadsheets
8. **Use pandas for data manipulation** and openpyxl for formatting when both are needed
9. **Clean up temporary script files** after execution if they won't be reused
- Simple single-cell reads/writes (use shell_agent or basic commands)
- Tasks where source data is already confirmed available
- Operations that require interactive user input
- Tasks where you need the agent to iteratively refine the approach
- **Solution**: Write the Python script to a `.py` file first, then execute it with `python3 script.py`. This pattern is significantly more reliable than inline heredoc execution when shell_agent is the executor.

**Issue**: Source data unavailable (FileNotFoundError, connection timeout, SSL error)
- **Solution**: 
  1. Confirm the exact error type and source path/URL
  2. Check for cached or backup copies in alternative locations
  3. Verify network connectivity and proxy settings if fetching from web
  4. Document all attempted sources and errors for escalation
  5. Abort spreadsheet processing until data source is resolved

**Issue**: Heredoc syntax fails with 'unknown error' when using shell_agent
- **Solution**: Write the Python script to a `.py` file first, then execute it with `python3 script.py`. This pattern is significantly more reliable than inline heredoc execution when shell_agent is the executor.

**Issue**: Data validation fails mid-execution
- **Solution**: Structure scripts with explicit validation phase before processing phase. Use `sys.exit(1)` to halt immediately on validation failures.
- **Solution**: Verify the file path is absolute or relative to the working directory. Add validation check at script start to catch this early.
- **Solution**: Ensure the file is not open in another application
- **Solution**: Ensure the file is not open in another application. Check file permissions with `ls -la` before processing.
- **Solution**: Process data in chunks using pandas `chunksize` parameter
- **Solution**: Process data in chunks using pandas `chunksize` parameter. Validate chunk count before processing.
