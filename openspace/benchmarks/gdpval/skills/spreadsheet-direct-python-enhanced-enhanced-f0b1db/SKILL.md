---
name: spreadsheet-validated-exec
description: Execute Python scripts for spreadsheets with prerequisite data validation and source accessibility checks
---

# Validated Python Execution for Spreadsheet Tasks

## Overview

This skill extends direct Python execution for spreadsheet operations by adding a **mandatory data validation phase** before any processing begins. This prevents wasted iterations on inaccessible data sources and provides clear error documentation when data cannot be accessed.

## When to Use This Skill

Use validated direct `run_shell` with Python scripts for spreadsheet operations when:

- Reading or writing complex Excel files with multiple sheets
- Data sources have already been validated as accessible
- You have fallback sources identified in case of access failures
- Applying formulas, formatting, or data transformations
- Working with `openpyxl`, `pandas`, or similar libraries
- The operation involves multiple steps that could exceed agent step limits
- You need precise control over error handling and debugging
- Complex scripts benefit from file-based execution for better reliability

## Why Validated Direct Execution?

Beyond standard shell_agent limitations, **unvalidated data access** causes:

- Wasted iterations attempting to process non-existent data
- Unclear error messages when source files are inaccessible
- No graceful degradation when primary sources fail
- Missing documentation of why data operations couldn't complete

Direct `run_shell` with Python validation is more reliable because it:

- Executes validation in a single step with no iteration limits
- Provides clearer, immediate error messages for access failures
- Handles complex operations without step constraints
- Gives full control over library imports and execution flow
- Writing scripts to `.py` files first avoids shell_agent parsing issues with heredocs
- Documents all access failures for troubleshooting and reporting

## Phase 0: Data Source Validation (REQUIRED)

**Before writing any spreadsheet processing code**, verify your data sources are accessible:

### Step 0.1: Verify Data Availability

```python
import os
import requests
from pathlib import Path

# For local files
def verify_local_file(file_path):
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Data file not found: {file_path}")
    if not path.is_file():
        raise ValueError(f"Path is not a file: {file_path}")
    if path.stat().st_size == 0:
        raise ValueError(f"Data file is empty: {file_path}")
    print(f"✓ Local file verified: {file_path} ({path.stat().st_size} bytes)")
    return True

# For remote URLs
def verify_remote_url(url, timeout=30):
    try:
        # Try HEAD request first (lighter than GET)
        response = requests.head(url, timeout=timeout, allow_redirects=True)
        if response.status_code == 405:  # HEAD not allowed, try GET
            response = requests.get(url, timeout=timeout, stream=True)
        response.raise_for_status()
        
        # Check content type if available
        content_type = response.headers.get('Content-Type', '')
        if 'text/html' in content_type and 'excel' not in url.lower():
            print(f"⚠ Warning: URL may return HTML, not data file")
        
        print(f"✓ Remote URL verified: {url} (Status: {response.status_code})")
        return True
    except requests.exceptions.SSLError as e:
        print(f"✗ SSL Error: {str(e)}")
        return False
    except requests.exceptions.ConnectionError as e:
        print(f"✗ Connection Error: {str(e)}")
        return False
    except requests.exceptions.Timeout as e:
        print(f"✗ Timeout Error: {str(e)}")
        return False
    except Exception as e:
        print(f"✗ Verification Failed: {str(e)}")
        return False
```

### Step 0.2: Handle Inaccessible Sources

**If primary data source is unavailable:**

1. **Check alternative locations:**
   - Is data available from a backup URL?
   - Is there a cached/local copy available?
   - Can data be obtained from an API instead of web scraping?
   - Is there an alternative data provider?

2. **Document the failure:**
   ```python
   def log_access_failure(source, error_type, timestamp=None):
       from datetime import datetime
       if not timestamp:
           timestamp = datetime.now().isoformat()
       
       error_log = {
           'timestamp': timestamp,
           'source': source,
           'error_type': error_type,
           'alternatives_attempted': [],
           'resolution': 'pending'
       }
       
       # Save to error log file
       import json
       with open('data_access_errors.json', 'a') as f:
           f.write(json.dumps(error_log) + '\n')
       
       return error_log
   ```

3. **Implement fallback strategy:**
   ```python
   def get_data_with_fallback(primary_source, fallback_sources):
       sources = [primary_source] + fallback_sources
       
       for i, source in enumerate(sources):
           print(f"Attempting source {i+1}/{len(sources)}: {source}")
           
           if source.startswith('http'):
               if verify_remote_url(source):
                   return download_data(source)
           else:
               if verify_local_file(source):
                   return load_local_data(source)
           
           print(f"Source {source} unavailable, trying next...")
       
       raise Exception(f"All {len(sources)} data sources unavailable")
   ```

### Step 0.3: Pre-Execution Checklist

Before proceeding to spreadsheet operations, confirm:

- [ ] Primary data source is accessible (file exists / URL responds)
- [ ] Data file is not empty (has content to process)
- [ ] Required permissions are in place (file not locked, API keys valid)
- [ ] Alternative sources identified if primary fails
- [ ] Error logging mechanism is configured

**If any check fails:** Do NOT proceed to spreadsheet processing. Report the blocking issue and either:
- Resolve the data access problem first
- Switch to an alternative data source
- Abort the task with clear error documentation

## How to Use (With Validation)

### Complete Workflow Template

**Recommended Pattern: Validate → Process → Report**

```bash
# Step 1: Write validation + processing script to file
cat > validated_spreadsheet_process.py << 'EOF'
import sys
import os
from pathlib import Path
import pandas as pd
from openpyxl import load_workbook

# === PHASE 0: VALIDATION ===
def validate_sources():
    sources_to_check = [
        ('input_data.xlsx', 'local'),
        # ('https://backup-source.com/data.xlsx', 'remote')
    ]
    
    validated_sources = []
    for source, source_type in sources_to_check:
        try:
            if source_type == 'local':
                if not Path(source).exists():
                    print(f"ERROR: Local file not found: {source}")
                    continue
                if Path(source).stat().st_size == 0:
                    print(f"ERROR: Local file is empty: {source}")
                    continue
            validated_sources.append(source)
            print(f"✓ Validated: {source}")
        except Exception as e:
            print(f"ERROR validating {source}: {e}")
    
    if not validated_sources:
        print("FATAL: No valid data sources available. Aborting.")
        sys.exit(1)
    
    return validated_sources

# === PHASE 1: PROCESSING ===
def process_data(source_file):
    # Your spreadsheet operations here
    pass

# === PHASE 2: REPORTING ===
def generate_report(success, details):
    report = {
        'status': 'success' if success else 'failed',
        'details': details
    }
    print(f"Report: {report}")
    return report

if __name__ == '__main__':
    try:
        # Validate first
        sources = validate_sources()
        
        # Process validated sources
        for source in sources:
            process_data(source)
        
        generate_report(True, f"Processed {len(sources)} sources")
        
    except Exception as e:
        generate_report(False, str(e))
        sys.exit(1)
EOF

# Step 2: Execute the validated script
python3 validated_spreadsheet_process.py
```

### Pattern 1: Write Script to File First (Complex Operations)

For complex multi-line scripts with multiple data sources:

```bash
# Step 1: Write the Python script to a file
cat > process_spreadsheet.py << 'EOF'
import openpyxl
from openpyxl import Workbook
from pathlib import Path
import sys

# Validate first
input_file = Path('file.xlsx')
if not input_file.exists():
    print(f"ERROR: Input file not found: {input_file}")
    sys.exit(1)
if input_file.stat().st_size == 0:
    print(f"ERROR: Input file is empty: {input_file}")
    sys.exit(1)

# Your spreadsheet code here
wb = openpyxl.load_workbook('file.xlsx')
# ... operations ...
wb.save('output.xlsx')
print('Success')
EOF

# Step 2: Execute the script
python3 process_spreadsheet.py
```

### Pattern 2: Inline Heredoc (Simple Scripts with Validation)

For short scripts when NOT using shell_agent, include validation inline:

```bash
# Include quick validation before processing
python3 << 'EOF'
import os
import sys

# Quick validation
input_file = 'data.xlsx'
if not os.path.exists(input_file):
    print(f"ERROR: {input_file} not found")
    sys.exit(1)
if os.path.getsize(input_file) == 0:
    print(f"ERROR: {input_file} is empty")
    sys.exit(1)

# Continue with processing...
EOF
```

## Examples

### Example 1: Validated Read and Transform

**With prerequisite validation:**

```python
import pandas as pd
import sys
from pathlib import Path

# Validate input file exists and has content
input_path = Path('input.xlsx')
if not input_path.exists():
    print(f"ERROR: Input file not found: {input_path}")
    sys.exit(1)
if input_path.stat().st_size == 0:
    print(f"ERROR: Input file is empty: {input_path}")
    sys.exit(1)

# Now safe to process
df = pd.read_excel('input.xlsx', sheet_name='Revenue')

# Apply transformations
df['Net_Revenue'] = df['Gross_Revenue'] * (1 - df['Tax_Rate'])

# Save results
df.to_excel('output.xlsx', index=False, sheet_name='Processed')
```

### Example 2: Multi-Sheet with Source Validation

```python
from openpyxl import load_workbook
import sys
from pathlib import Path

# Validate workbook accessibility
wb_path = Path('tour_data.xlsx')
if not wb_path.exists():
    print(f"ERROR: Workbook not found: {wb_path.absolute()}")
    sys.exit(1)

try:
    wb = load_workbook('tour_data.xlsx')
except Exception as e:
    print(f"ERROR: Cannot open workbook: {e}")
    print("Possible causes: file locked, corrupted, or wrong format")
    sys.exit(1)

# Iterate through sheets
for sheet_name in wb.sheetnames:
    ws = wb[sheet_name]
    # Apply formatting or calculations
    for row in ws.iter_rows(min_row=2, max_col=5):
        # Process cells
        pass

wb.save('tour_data_processed.xlsx')
```

### Example 3: Formatting with Pre-Checks

```python
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment
from pathlib import Path
import sys

# Pre-check file
report_path = Path('report.xlsx')
if not report_path.exists():
    print(f"ERROR: Report file not found: {report_path}")
    sys.exit(1)

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

### Example 4: Comprehensive Error Handling with Logging

```python
import sys
import json
from datetime import datetime
from pathlib import Path
from openpyxl import load_workbook

def log_error(source, error_msg, error_type):
    """Log errors for later analysis and reporting"""
    error_record = {
        'timestamp': datetime.now().isoformat(),
        'source': source,
        'error_type': error_type,
        'message': error_msg
    }
    
    with open('processing_errors.log', 'a') as f:
        f.write(json.dumps(error_record) + '\n')
    
    return error_record

try:
    # Validate file before opening
    if not Path('data.xlsx').exists():
        log_error('data.xlsx', 'File not found', 'ValidationError')
        raise FileNotFoundError('data.xlsx')
    
    wb = load_workbook('data.xlsx')
    ws = wb.active
    
    # Your operations here
    value = ws['A1'].value
    
    wb.save('output.xlsx')
    print(f"Success: Processed {ws.max_row} rows")
    
except FileNotFoundError as e:
    log_error('data.xlsx', str(e), 'FileNotFoundError')
    print(f"ERROR: {str(e)}", file=sys.stderr)
    print("ACTION: Verify file path and permissions")
    sys.exit(1)
except PermissionError as e:
    log_error('data.xlsx', str(e), 'PermissionError')
    print(f"ERROR: Permission denied - file may be open in another application")
    print("ACTION: Close the file in other applications and retry")
    sys.exit(1)
except Exception as e:
    log_error('data.xlsx', str(e), type(e).__name__)
    print(f"ERROR: {str(e)}", file=sys.stderr)
    sys.exit(1)
```

## Best Practices (Enhanced)

1. **Always validate before processing**: Check data source availability before any spreadsheet operations
2. **Log all access failures**: Document when and why data sources become unavailable
3. **Identify fallbacks upfront**: Know your alternative sources before starting
4. **Fail fast on validation errors**: Don't waste iterations on impossible operations
5. **Prefer file-based execution** for complex scripts: write to `.py` file first, then execute via `run_shell`
6. **Import only needed libraries** to reduce execution time
7. **Print clear success/error messages** for debugging
8. **Save intermediate results** for complex multi-step transformations
9. **Test with small data** before scaling to large spreadsheets
10. **Use pandas for data manipulation** and openpyxl for formatting when both are needed
11. **Clean up temporary script files** after execution if they won't be reused

## Data Access Failure Protocols

### When Primary Source Fails

1. **Immediate actions:**
   - Log the failure with timestamp, source, and error type
   - Check if error is temporary (timeout) or permanent (404, file doesn't exist)
   - Attempt alternative sources if available

2. **For temporary failures (timeouts, 5xx errors):**
   ```python
   def retry_with_backoff(operation, max_retries=3, base_delay=1):
       import time
       for attempt in range(max_retries):
           try:
               return operation()
           except (TimeoutError, ConnectionError) as e:
               if attempt == max_retries - 1:
                   raise
               delay = base_delay * (2 ** attempt)
               print(f"Retry {attempt + 1}/{max_retries} after {delay}s delay")
               time.sleep(delay)
   ```

3. **For permanent failures (file not found, 404):**
   - Switch to identified fallback source immediately
   - Document the primary source failure
   - Continue with fallback if available, otherwise abort cleanly

### Error Reporting Template

```python
def report_data_access_issue(issue_type, source, details, alternatives_tried=None):
    """Standardize error reporting for data access failures"""
    from datetime import datetime
    
    report = f"""
=== DATA ACCESS FAILURE REPORT ===
Type: {issue_type}
Source: {source}
Time: {datetime.now().isoformat()}
Details: {details}
Alternatives Attempted: {alternatives_tried or 'None'}
Recommended Action: {get_recommended_action(issue_type)}
==================================
"""
    print(report)
    return report

def get_recommended_action(issue_type):
    actions = {
        'FileNotFound': 'Verify file path, check if file was moved or deleted',
        'ConnectionError': 'Check network connectivity, try alternative source',
        'Timeout': 'Increase timeout, check source server status',
        'PermissionError': 'Check file permissions, ensure file not open elsewhere',
        'SSLError': 'Verify SSL certificate, try HTTP if appropriate',
        'default': 'Review error details, check source availability'
    }
    return actions.get(issue_type, actions['default'])
```

## When NOT to Use This Skill

- Simple single-cell reads/writes (validation overhead not warranted)
- Operations that require interactive user input
- Tasks where you need the agent to iteratively refine the approach
- When you have no way to validate data sources (skip to error handling only)

## Common Libraries

| Library | Best For | Also Use For |
|---------|----------|--------------|
| `requests` | Web requests | Source URL validation, health checks |
| `openpyxl` | Reading/writing .xlsx files, formatting, formulas | - |
| `pandas` | Data manipulation, analysis, merging datasets | Chunked reading for large files |
| `xlrd` | Reading older .xls files (read-only) | - |
| `xlsxwriter` | Creating new .xlsx files with advanced formatting | - |

## Troubleshooting (Enhanced)

**Issue**: Data source unavailable / file not found
- **Solution**: Run validation check first before processing. If validation fails:
  1. Verify the path/URL is correct
  2. Check if file was moved, renamed, or deleted
  3. Try alternative sources if available
  4. Document the failure and abort cleanly rather than proceeding

**Issue**: Source was accessible yesterday but not today
- **Solution**: Implement fallback sources and log the change. Consider:
  - Setting up monitoring for critical data sources
  - Caching important data locally when possible
  - Having contact information for data source maintainers

**Issue**: Validation passes but processing fails
- **Solution**: Validation checks accessibility, not content validity. Add:
  - Schema validation (check expected columns exist)
  - Content validation (check data types, ranges)
  - Sample verification (read first few rows to confirm structure)

**Issue**: Heredoc syntax fails with 'unknown error' when using shell_agent
- **Solution**: Write the Python script to a `.py` file first with full validation logic, then execute with `python3 script.py`. This is significantly more reliable than inline heredoc when shell_agent is the executor.

**Issue**: FileNotFoundError (after validation passed)
- **Solution**: File may have been moved/deleted between validation and processing, or path is relative and working directory changed. Use absolute paths.

**Issue**: PermissionError
- **Solution**: Ensure the file is not open in another application. On Linux/Mac, check with `lsof | grep filename`

**Issue**: ConnectionError / Timeout on remote sources
- **Solution**: Check network connectivity, try with increased timeout, use retry logic with exponential backoff, or switch to cached/local copy if available

**Issue**: SSL Certificate errors
- **Solution**: Verify the certificate is valid. As last resort for trusted internal sources only: `requests.get(url, verify=False)` - but log this as a security concern

**Issue**: All alternative sources fail
- **Solution**: Abort with comprehensive error report documenting:
  - All sources attempted
  - Error types for each
  - Timestamp of failures
  - Recommended next steps for human operator

**Issue**: MemoryError on large files
- **Solution**: Process data in chunks using pandas `chunksize` parameter. Validate chunk size during initial validation phase.

**Issue**: Formatting not applying
- **Solution**: Ensure you're modifying cell styles before saving, and use `.copy()` for style objects. Verify workbook is not in read-only mode.
