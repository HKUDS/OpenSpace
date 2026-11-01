---
name: spreadsheet-source-validated
description: Execute Python scripts for spreadsheet operations with mandatory source data validation and fallback protocols
---

# Validated Python Execution for Spreadsheet Tasks

## Pre-Execution Data Validation

**CRITICAL**: Before attempting any spreadsheet operations, validate that your source data is accessible and complete.

### Step 1: Verify Data Source Availability

```python
import os
import sys
from pathlib import Path

def validate_source_data(source_paths):
    """Validate all required data sources before processing."""
    missing = []
    inaccessible = []
    
    for path in source_paths:
        p = Path(path)
        if not p.exists():
            missing.append(str(path))
        elif not os.access(path, os.R_OK):
            inaccessible.append(str(path))
    
    if missing:
        print(f"ERROR: Missing data sources: {', '.join(missing)}", file=sys.stderr)
        return False
    if inaccessible:
        print(f"ERROR: Inaccessible data sources: {', '.join(inaccessible)}", file=sys.stderr)
        return False
    
    print(f"VALIDATED: {len(source_paths)} source(s) available")
    return True

# Usage
sources = ['input.xlsx', 'reference_data.csv']
if not validate_source_data(sources):
    sys.exit(1)
```

### Step 2: Verify Data Integrity

```python
import pandas as pd
from openpyxl import load_workbook

def validate_spreadsheet_integrity(file_path, required_sheets=None, required_columns=None):
    """Check spreadsheet structure before processing."""
    try:
        # Check file is readable
        wb = load_workbook(file_path, read_only=True)
        
        # Verify required sheets exist
        if required_sheets:
            missing_sheets = [s for s in required_sheets if s not in wb.sheetnames]
            if missing_sheets:
                print(f"ERROR: Missing sheets: {missing_sheets}", file=sys.stderr)
                wb.close()
                return False
        
        # Verify required columns (sample first sheet)
        if required_columns:
            ws = wb.active
            headers = [cell.value for cell in ws[1]]
            missing_cols = [c for c in required_columns if c not in headers]
            if missing_cols:
                print(f"ERROR: Missing columns: {missing_cols}", file=sys.stderr)
                wb.close()
                return False
        
        wb.close()
        print(f"VALIDATED: Spreadsheet structure OK")
        return True
        
    except Exception as e:
        print(f"ERROR: Cannot validate spreadsheet: {str(e)}", file=sys.stderr)
        return False
```

### Step 3: External Data Source Verification

For data retrieved from APIs, websites, or external services:

```python
import requests
from urllib3.exceptions import SSLError, MaxRetryError

def validate_external_source(url, timeout=30, max_retries=2):
    """Verify external data source is accessible."""
    for attempt in range(max_retries + 1):
        try:
            response = requests.head(url, timeout=timeout, allow_redirects=True)
            if response.status_code == 200:
                print(f"VALIDATED: External source accessible ({url})")
                return True
            else:
                print(f"WARNING: External source returned {response.status_code}", file=sys.stderr)
        except (SSLError, MaxRetryError, requests.ConnectionError) as e:
            if attempt == max_retries:
                print(f"ERROR: External source inaccessible after {max_retries + 1} attempts: {url}", file=sys.stderr)
                return False
            print(f"RETRY {attempt + 1}/{max_retries}: {str(e)}", file=sys.stderr)
    
    return False
```

## When to Use This Skill

Use validated Python execution for spreadsheet operations when:

- **Source data must be verified** before processing begins
- Reading or writing complex Excel files with multiple sheets
- External data sources (APIs, websites) must be accessed first
- Applying formulas, formatting, or data transformations
- Working with `openpyxl`, `pandas`, or similar libraries
- The operation involves multiple steps that could exceed agent step limits
- You need precise control over error handling and debugging
- **Fallback data sources** should be identified if primary sources fail

## Handling Inaccessible Data Sources

### Protocol for Failed Data Access

1. **Document the failure** with specific error messages
2. **Identify alternative sources** (see Alternative Sources section below)
3. **Report the blockage** clearly before attempting workarounds
4. **Limit retry attempts** to 3 before escalating

### Alternative Data Source Identification

When primary data sources are inaccessible:

```python
ALTERNATIVE_SOURCES = {
    'epa_water_data': [
        'https://dataservices.epa.illinois.gov/swap',  # Primary
        'https://www.epa.gov/safewater/data-and-reports',  # Federal fallback
        'https://waterdata.usgs.gov/nwis',  # USGS fallback
    ],
    'financial_data': [
        'internal_database.xlsx',  # Primary
        'backup_financial_data.csv',  # Local backup
        'request_from_stakeholder',  # Manual acquisition
    ]
}

def try_alternative_sources(source_key):
    """Iterate through alternative sources until one succeeds."""
    alternatives = ALTERNATIVE_SOURCES.get(source_key, [])
    
    for i, source in enumerate(alternatives):
        print(f"Attempting alternative {i + 1}/{len(alternatives)}: {source}")
        if source.startswith('http'):
            if validate_external_source(source):
                return source
        else:
            if Path(source).exists():
                print(f"SUCCESS: Alternative source found: {source}")
                return source
    
    print(f"ERROR: All alternatives exhausted for {source_key}", file=sys.stderr)
    return None
```

### Error Reporting Protocol

```python
def report_data_access_failure(source, error_type, alternatives_tried=0):
    """Standardized error reporting for data access failures."""
    error_report = {
        'timestamp': datetime.now().isoformat(),
        'source': source,
        'error_type': error_type,
        'alternatives_tried': alternatives_tried,
        'action_required': 'Manual data acquisition or source configuration update'
    }
    
    print("=" * 60, file=sys.stderr)
    print("DATA ACCESS FAILURE REPORT", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    for key, value in error_report.items():
        print(f"{key}: {value}", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    
    return error_report
```

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

### Recommended Pattern: Validate Then Execute

```bash
# Step 1: Write validation script
cat > validate_sources.py << 'EOF'
import sys
from pathlib import Path

sources = ['input.xlsx', 'config.json']
missing = [s for s in sources if not Path(s).exists()]

if missing:
    print(f"BLOCKED: Missing sources: {missing}", file=sys.stderr)
    sys.exit(1)

print("VALIDATED: All sources available")
EOF

# Step 2: Run validation
python3 validate_sources.py || exit 1

# Step 3: Write processing script
cat > process_spreadsheet.py << 'EOF'
import openpyxl
# Your spreadsheet code here
EOF

# Step 4: Execute processing
python3 process_spreadsheet.py

# Step 5: Clean up (optional)
rm validate_sources.py
```

### Complete Workflow Example

```python
#!/usr/bin/env python3
"""
Complete workflow: Validate -> Process -> Report
"""
import sys
from pathlib import Path
from datetime import datetime
from openpyxl import load_workbook

def main():
    # PHASE 1: Validation
    print(f"[{datetime.now().isoformat()}] Starting validation...")
    
    source_file = 'input_data.xlsx'
    if not Path(source_file).exists():
        print(f"ERROR: Source file not found: {source_file}", file=sys.stderr)
        # Check for alternatives
        for alt in ['backup_input.xlsx', 'data_backup.csv']:
            if Path(alt).exists():
                print(f"FALLBACK: Using alternative: {alt}")
                source_file = alt
                break
        else:
            print("ERROR: No alternative sources available", file=sys.stderr)
            sys.exit(1)
    
    # PHASE 2: Processing
    print(f"[{datetime.now().isoformat()}] Processing {source_file}...")
    try:
        wb = load_workbook(source_file)
        ws = wb.active
        
        # Your operations here
        for row in ws.iter_rows(min_row=2):
            pass  # Process data
        
        wb.save('output.xlsx')
        print(f"SUCCESS: Processed {ws.max_row - 1} rows")
        
    except Exception as e:
        print(f"ERROR: Processing failed: {str(e)}", file=sys.stderr)
        sys.exit(1)
    
    # PHASE 3: Cleanup
    print(f"[{datetime.now().isoformat()}] Complete")

if __name__ == '__main__':
    main()
```

## Best Practices

1. **ALWAYS validate sources first** before any spreadsheet operations
2. **Prefer file-based execution** for complex scripts: write to `.py` file first, then execute via `run_shell`
3. **Identify 2-3 alternative sources** for critical data before starting
4. **Import only needed libraries** to reduce execution time
5. **Print clear success/error messages** for debugging
6. **Save intermediate results** for complex multi-step transformations
7. **Test with small data** before scaling to large spreadsheets
8. **Use pandas for data manipulation** and openpyxl for formatting when both are needed
9. **Clean up temporary script files** after execution if they won't be reused
10. **Document all access failures** with timestamps and error details

## When NOT to Use This Skill

- Simple single-cell reads/writes (use shell_agent or basic commands)
- Operations that require interactive user input
- Tasks where you need the agent to iteratively refine the approach
- When source data is guaranteed available (skip validation overhead)

## Common Libraries

| Library | Best For |
|---------|----------|
| `openpyxl` | Reading/writing .xlsx files, formatting, formulas |
| `pandas` | Data manipulation, analysis, merging datasets |
| `xlrd` | Reading older .xls files (read-only) |
| `xlsxwriter` | Creating new .xlsx files with advanced formatting |
| `requests` | Validating external API/data sources |
| `pathlib` | Cross-platform file path validation |

## Troubleshooting

**Issue**: Heredoc syntax fails with 'unknown error' when using shell_agent
- **Solution**: Write the Python script to a `.py` file first, then execute it with `python3 script.py`. This pattern is significantly more reliable than inline heredoc execution when shell_agent is the executor.

**Issue**: Source data validation fails
- **Solution**: 
  1. Check file paths are absolute or relative to working directory
  2. Verify file permissions with `ls -la`
  3. Try alternative sources from your fallback list
  4. Report the failure with complete error details before proceeding

**Issue**: External data source inaccessible (SSL/proxy errors)
- **Solution**:
  1. Try HTTP instead of HTTPS if appropriate
  2. Disable SSL verification temporarily: `requests.get(url, verify=False)`
  3. Check proxy settings in environment variables
  4. **After 3 failed attempts, switch to alternative source or report blockage**
  5. Do NOT exhaust 30+ iterations on a single inaccessible source

**Issue**: FileNotFoundError
- **Solution**: Verify the file path is absolute or relative to the working directory; check for alternative backup files

**Issue**: PermissionError
- **Solution**: Ensure the file is not open in another application; check file permissions

**Issue**: MemoryError on large files
- **Solution**: Process data in chunks using pandas `chunksize` parameter

**Issue**: Formatting not applying
- **Solution**: Ensure you're modifying cell styles before saving, and use `.copy()` for style objects

**Issue**: Data validation passes but processing fails
- **Solution**: Add more detailed integrity checks (column types, value ranges, row counts)
