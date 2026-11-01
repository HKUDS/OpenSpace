---
name: run-shell-python-file-io
description: Use run_shell with inline Python for reliable file I/O when execute_code_sandbox cannot access working directory files
---

# Run Shell Python File I/O

## When to Use This Skill

Use this technique when `execute_code_sandbox` cannot access files in your working directory, but you need to perform file operations (read, write, transform) as part of your task execution.

## The Pattern

Instead of using `execute_code_sandbox`, run Python code directly through `run_shell` with explicit path handling. This provides reliable file I/O capabilities within the task workspace.

## How to Apply

### Step 1: Identify File Access Needs
Determine which files you need to read from or write to in the working directory.

### Step 2: Write Inline Python via run_shell
Construct a Python script that handles your file operations and execute it through `run_shell`:

```bash
python3 -c "
import os

# Get the working directory
work_dir = os.getcwd()
print(f'Working directory: {work_dir}')

# Read a file
with open('input.txt', 'r') as f:
    content = f.read()

# Process the content
processed = content.upper()

# Write to output file
with open('output.txt', 'w') as f:
    f.write(processed)

print('File operation complete')
"
```

### Step 3: Handle Complex Logic
For more complex operations, write a temporary Python script file first:

```bash
cat > /tmp/process.py << 'EOF'
import os
import json

work_dir = os.getcwd()

# Read input
with open('data.json', 'r') as f:
    data = json.load(f)

# Transform
result = {k: v * 2 for k, v in data.items()}

# Write output
with open('result.json', 'w') as f:
    json.dump(result, f, indent=2)

print(f'Processed {len(data)} items')
EOF

python3 /tmp/process.py
```

### Step 4: Verify File Operations
After execution, verify the files were created/modified correctly:

```bash
ls -la
cat output.txt
```

## Best Practices

1. **Use Absolute or Relative Paths Explicitly**: Always specify file paths clearly; `os.getcwd()` helps confirm your working directory.

2. **Handle Errors Gracefully**: Add try/except blocks to catch file I/O errors:
   ```python
   try:
       with open('file.txt', 'r') as f:
           content = f.read()
   except FileNotFoundError:
       print('File not found')
   ```

3. **Use Here-Docs for Complex Scripts**: For scripts longer than a few lines, use bash here-documents to avoid escaping issues.

4. **Clean Up Temporary Files**: If you create temporary scripts, consider removing them after execution.

5. **Check Working Directory First**: Print `os.getcwd()` to confirm you're operating in the expected directory.

## Common Use Cases

- Reading configuration files from the workspace
- Writing generated reports or outputs
- Transforming data files (CSV, JSON, XML)
- Batch processing multiple files
- Generating structured documents (PDFs, spreadsheets)

## Example: Processing Multiple Files

```bash
python3 -c "
import os
import glob

work_dir = os.getcwd()
files = glob.glob('*.txt')

for f in files:
    with open(f, 'r') as file:
        content = file.read()
    # Process each file
    with open(f'processed_{f}', 'w') as out:
        out.write(content.strip().upper())

print(f'Processed {len(files)} files')
"
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| File not found | Check `os.getcwd()` output; use absolute paths if needed |
| Permission denied | Ensure the directory is writable; avoid protected system paths |
| Encoding errors | Specify encoding explicitly: `open('file.txt', 'r', encoding='utf-8')` |
| Multi-line script issues | Use here-doc (`<< 'EOF'`) instead of `-c` for complex scripts |