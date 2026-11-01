---
name: python-spreadsheet-debug
description: Systematic Python debugging workflow for spreadsheet tasks to isolate environment issues from script logic
---

# Python Spreadsheet Debugging Workflow

When executing Python scripts for spreadsheet/data processing tasks, use this systematic debugging approach to efficiently isolate environment configuration issues from script logic errors, especially when `run_shell` returns opaque errors.

## Step 1: Verify Python Environment

First, confirm the Python interpreter path and version to ensure you're working in the expected environment:

```bash
which python python3
python --version
python3 --version
```

This identifies whether Python is available and which version is being used.

## Step 2: Test Library Imports in Isolation

Before running your full script, verify that required libraries can be imported successfully. Test each critical import individually:

```bash
python -c "import pandas; print('pandas:', pandas.__version__)"
python -c "import openpyxl; print('openpyxl:', openpyxl.__version__)"
python -c "import xlrd; print('xlrd:', xlrd.__version__)"
```

Replace library names based on your script's requirements. This identifies missing dependencies or version conflicts early.

## Step 3: Run Minimal Test Script

Create and execute a minimal script that exercises only the core functionality without full business logic:

```python
# test_minimal.py
import pandas as pd

# Test 1: Can we read a file?
try:
    df = pd.read_excel('sample.xlsx')
    print(f"✓ File read successful: {len(df)} rows")
except Exception as e:
    print(f"✗ File read failed: {e}")

# Test 2: Can we perform basic operations?
try:
    result = df.groupby('category')['amount'].sum()
    print(f"✓ GroupBy operation successful")
except Exception as e:
    print(f"✗ Operation failed: {e}")
```

Run this with: `python test_minimal.py`

**Purpose:** This isolates whether the issue is with file access, library functionality, or specific script logic.

## Step 4: Execute Full Script

Once the minimal test passes, run the complete script:

```bash
python your_script.py
```

If errors occur now, you know the environment is correctly configured and can focus on debugging the specific logic.

## Quick Diagnostic Command

For rapid diagnosis, run all checks in sequence:

```bash
echo "=== Python Version ===" && python --version && \
echo "=== Key Imports ===" && python -c "import pandas, openpyxl; print('OK')" && \
echo "=== Ready for full script ==="
```

## Common Issues and Resolutions

| Symptom | Likely Cause | Resolution |
|---------|--------------|------------|
| `ModuleNotFoundError` | Missing package | `pip install <package-name>` |
| `ImportError` with version | Version conflict | Check `pip list`, reinstall specific version |
| `FileNotFoundError` | Wrong path/context | Verify working directory with `pwd` |
| `PermissionError` | File access | Check file permissions or path |

## When to Use This Pattern

- `run_shell` returns cryptic or truncated error messages
- Script worked previously but suddenly fails
- Deploying to a new environment or container
- Debugging spreadsheet processing with pandas, openpyxl, xlrd, etc.

## Benefits

- **Fast isolation:** Identifies environment vs. logic issues within seconds
- **Incremental confidence:** Each passing step narrows the problem space
- **Reusable pattern:** Apply consistently across all Python spreadsheet tasks