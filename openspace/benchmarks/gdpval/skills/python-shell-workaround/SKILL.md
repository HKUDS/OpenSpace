---
name: python-shell-workaround
description: Workaround for executing Python code with external packages when sandbox fails
---

# Python Shell Workaround

## When to Use This Skill

Use this pattern when `execute_code_sandbox` fails with Python code that requires external packages (like `openpyxl`, `python-docx`, `pandas`, etc.) that may not be available in the sandbox environment.

## The Pattern

Instead of relying solely on `execute_code_sandbox`, use `run_shell` with inline Python commands as a fallback:

```bash
python3 -c "your python code here"
```

## Step-by-Step Instructions

### Step 1: Detect Sandbox Failure

Watch for errors like:
- ModuleNotFoundError for common packages
- Import errors for external libraries
- Sandbox environment limitations

### Step 2: Convert Python Code to Shell Command

Transform your Python script into a one-liner or use a heredoc for longer scripts:

**For simple code:**
```bash
python3 -c "import openpyxl; wb = openpyxl.Workbook(); wb.save('file.xlsx')"
```

**For complex code (using heredoc):**
```bash
python3 << 'EOF'
import openpyxl
from openpyxl.utils import get_column_letter

wb = openpyxl.Workbook()
ws = wb.active
ws['A1'] = 'Header'
wb.save('output.xlsx')
print('File created successfully')
EOF
```

### Step 3: Execute with run_shell

Use `run_shell` to execute the command:

```
tool: run_shell
command: python3 -c "import openpyxl; print('Package available')"
```

### Step 4: Verify Output

Check the stdout/stderr from run_shell to confirm success, then proceed with subsequent steps.

## Best Practices

1. **Escape quotes properly**: Use single quotes inside double-quoted strings or vice versa
2. **Use heredocs for multiline code**: Cleaner and easier to debug
3. **Install packages if needed**: Add `pip install package` before your Python command if packages are missing
4. **Chain commands safely**: Use `&&` to ensure prerequisites succeed before running Python code

## Example Scenarios

### Creating Excel Files
```bash
python3 << 'EOF'
import openpyxl
from openpyxl.worksheet.datavalidation import DataValidation

wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Data"

# Add headers
headers = ['Name', 'Email', 'Status']
for col, header in enumerate(headers, 1):
    ws.cell(row=1, column=col, value=header)

wb.save('output.xlsx')
EOF
```

### Creating Word Documents
```bash
python3 -c "
from docx import Document
doc = Document()
doc.add_heading('Report', 0)
doc.save('report.docx')
print('Document created')
"
```

## Limitations

- Complex multiline scripts are harder to maintain as one-liners
- Debugging is less convenient than in sandbox
- Some packages may still not be available in the shell environment

## Related Patterns

- If packages are truly unavailable, consider installing them first: `pip install package_name && python3 -c "..."`
- For persistent scripts, write to a `.py` file first, then execute with `python3 script.py`