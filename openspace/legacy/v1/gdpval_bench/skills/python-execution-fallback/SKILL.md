---
name: python-execution-fallback
description: Four-step recovery workflow for code execution failures when inline Python fails
---

# Python Execution Fallback Workflow

This skill provides a systematic debugging approach when inline Python code execution fails, particularly in heredoc or shell_agent contexts.

## Pattern Overview

When Python code execution fails, follow this recovery workflow:

1. **Attempt inline execution first** - Try running Python code directly
2. **Write script to file on failure** - Persist the code to a `.py` file
3. **Execute the file** - Run the saved script via shell
4. **Validate output** - Verify results are correct and complete

## When to Use

Apply this pattern when:
- Inline Python execution fails with syntax or runtime errors
- Heredoc-based code execution encounters parsing issues
- Working with complex multi-line scripts that need debugging
- Need systematic approach to isolate execution failures
- Spreadsheet/data generation tasks fail in inline mode

## Step-by-Step Instructions

### Step 1: Attempt Inline Execution

Try executing Python code inline first (using execute_code_sandbox or similar):

```python
# Example: Attempt inline execution
import pandas as pd
import openpyxl

df = pd.read_excel("input.xlsx")
# Process data...
df.to_excel("output.xlsx", index=False)
```

If this succeeds, proceed. If it fails, move to Step 2.

### Step 2: Write Script to File (On Failure)

When inline execution fails, write the complete script to a persistent file:

```python
# Capture the script content
script_content = '''
import pandas as pd
import openpyxl
import sys

try:
    # Your original code here
    df = pd.read_excel("input.xlsx")
    
    # Processing logic
    result_df = df.groupby("category").sum()
    
    # Output
    result_df.to_excel("output.xlsx", index=False)
    print("SUCCESS: File generated")
    
except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)
'''

# Write to file
with open("script.py", "w") as f:
    f.write(script_content)

print("Script written to script.py")
```

### Step 3: Execute the File

Run the saved script using shell execution:

```bash
python script.py
```

Or with error capture:

```bash
python script.py 2>&1 | tee execution.log
```

This approach:
- Avoids heredoc parsing issues
- Provides clearer error messages
- Allows script inspection and modification
- Enables re-execution without rewriting

### Step 4: Validate Output

Verify the results are correct:

```python
# Validation script
import pandas as pd
import os

# Check file exists
if os.path.exists("output.xlsx"):
    df = pd.read_excel("output.xlsx")
    print(f"Rows: {len(df)}, Columns: {len(df.columns)}")
    print(df.head())
    print("VALIDATION: PASSED")
else:
    print("VALIDATION: FAILED - Output file missing")
```

## Code Example: Complete Workflow

```python
# Full fallback workflow example
def generate_spreadsheet_fallback(data, output_path):
    """Generate spreadsheet with fallback workflow"""
    
    # Step 1: Try inline
    inline_script = f'''
import pandas as pd
data = {data}
df = pd.DataFrame(data)
df.to_excel("{output_path}", index=False)
'''
    
    try:
        # Attempt inline execution
        result = execute_code_sandbox(code=inline_script)
        if "error" not in result.lower():
            return "SUCCESS_INLINE"
    except Exception as e:
        pass
    
    # Step 2: Write to file
    file_script = f'''
import pandas as pd
import sys

try:
    data = {data}
    df = pd.DataFrame(data)
    df.to_excel("{output_path}", index=False)
    print("SUCCESS")
except Exception as e:
    print(f"ERROR: {{e}}", file=sys.stderr)
    sys.exit(1)
'''
    
    with open("generate.py", "w") as f:
        f.write(file_script)
    
    # Step 3: Execute file
    shell_result = run_shell(command="python generate.py")
    
    # Step 4: Validate
    if os.path.exists(output_path):
        return "SUCCESS_FILE"
    else:
        return "FAILED"
```

## Best Practices

1. **Include error handling** in file-based scripts (try/except with sys.stderr)
2. **Add success indicators** (print statements) to confirm execution
3. **Preserve original logic** when converting from inline to file
4. **Check return codes** when executing scripts via shell
5. **Clean up temporary files** after successful validation
6. **Log both stdout and stderr** for debugging

## Common Failure Scenarios

This pattern helps resolve:
- Heredoc string escaping issues
- Multi-line code formatting problems
- Import path resolution failures
- Environment variable access issues
- Complex indentation in inline code

## Related Patterns

- Combine with `retry-with-modification` for iterative debugging
- Use alongside `output-validation-check` for result verification
- Pair with `error-log-analysis` for root cause identification