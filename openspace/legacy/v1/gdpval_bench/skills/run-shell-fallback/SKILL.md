---
name: run-shell-fallback
description: Use run_shell with inline Python as a reliable fallback when execute_code_sandbox or read_file fail
---

# Run Shell Fallback Pattern

This skill provides a reliable workaround when `execute_code_sandbox` or `read_file` fail with 'unknown error' by switching to `run_shell` with inline Python code.

## When to Use

Apply this pattern when you encounter:
- `execute_code_sandbox` fails with "unknown error" or timeout
- `read_file` fails to read accessible files with "unknown error"
- Sandbox environment is unreliable for your specific task

## How to Implement

### 1. File Reading Fallback

When `read_file` fails, use `run_shell` with Python or `cat`:

**For text files:**
```bash
run_shell(command="cat /path/to/file.txt")
```

**For structured parsing (Python one-liner):**
```bash
run_shell(command="python3 -c \"import json; print(json.load(open('/path/to/file.json')))\"")
```

**For multi-line Python processing:**
```bash
run_shell(command="python3 << 'EOF'
with open('/path/to/file.txt', 'r') as f:
    content = f.read()
    # Process content here
    print(content.upper())
EOF")
```

### 2. Code Execution Fallback

When `execute_code_sandbox` fails, use `run_shell` with inline Python:

**Simple one-liners:**
```bash
run_shell(command="python3 -c \"print('Hello'); import os; print(os.getcwd())\"")
```

**Multi-line scripts (heredoc):**
```bash
run_shell(command="python3 << 'EOF'
import os
import json

def process_data(data):
    return {k: v.upper() if isinstance(v, str) else v for k, v in data.items()}

with open('input.json') as f:
    data = json.load(f)
    
result = process_data(data)
print(json.dumps(result, indent=2))
EOF")
```

**With inline file write:**
```bash
run_shell(command="python3 << 'EOF'
result = {'status': 'success', 'count': 42}
with open('output.json', 'w') as f:
    json.dump(result, f, indent=2)
print('File written successfully')
EOF")
```

### 3. Decision Flow

```
1. Attempt execute_code_sandbox or read_file
2. If failure with 'unknown error':
   a. Determine task type (code execution vs file reading)
   b. Choose inline Python approach (-c for simple, heredoc for complex)
   c. Execute via run_shell
   d. Parse output as needed
3. Continue with task using results from run_shell
```

## Best Practices

1. **Escape quotes properly** in `-c` commands: Use `\"` for double quotes inside the command string

2. **Use heredoc for complex scripts**: When code exceeds 2-3 lines or needs multiple imports, use `<< 'EOF'` syntax

3. **Quote EOF marker**: Use `<< 'EOF'` (with quotes) to prevent variable expansion in the heredoc

4. **Capture stdout**: Design your inline Python to print results to stdout for easy capture

5. **Error handling**: Add try/except blocks in your inline Python to catch and report errors clearly

6. **File paths**: Use absolute paths when possible, or ensure working directory is correct

## Examples

### Example 1: Read and parse JSON after read_file failure
```bash
run_shell(command="python3 -c \"import json; data=json.load(open('config.json')); print(data['key'])\"")
```

### Example 2: Process CSV data after execute_code_sandbox failure
```bash
run_shell(command="python3 << 'EOF'
import csv
with open('data.csv', 'r') as f:
    reader = csv.DictReader(f)
    rows = list(reader)
    print(f\"Total rows: {len(rows)}\")
    for row in rows[:5]:
        print(row)
EOF")
```

### Example 3: Transform and write output file
```bash
run_shell(command="python3 << 'EOF'
import json
with open('input.json') as f:
    data = json.load(f)
transformed = [item.upper() if isinstance(item, str) else item for item in data]
with open('output.json', 'w') as f:
    json.dump(transformed, f, indent=2)
print('Transformation complete')
EOF")
```

## Limitations

- run_shell executes in the shell environment, not the sandbox - ensure paths and dependencies are available
- Output is limited to stdout/stderr - use file writes for large data
- Complex multi-file projects may require creating temporary script files first