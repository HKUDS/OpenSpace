---
name: sandbox-execution-fallback-717e65
description: Fallback from execute_code_sandbox to file-based run_shell execution when sandbox fails
---

# Sandbox Execution Fallback

## When to Use

Apply this pattern when `execute_code_sandbox` fails repeatedly or exhibits instability. Common triggers include:
- Sandbox returns errors after 2-3 retry attempts
- Error messages indicate provider/system issues rather than code bugs
- Execution times out or hangs inconsistently
- Complex multi-step code with file I/O is needed
- Better error visibility and debugging is required

## Procedure

### Step 1: Detect When to Switch

Recognize the failure pattern:
- `execute_code_sandbox` produces repeated errors despite code corrections
- Error output suggests infrastructure issues (not syntax/logic errors)
- The sandbox environment appears unstable or limited

### Step 2: Write Python Script to File

Use `write_file` to save your script:

```
write_file(
    path="script.py",
    content="#!/usr/bin/env python3

# Your Python code here
import sys
print('Executing via file-based approach')
# ... rest of your code
"
)
```

For multi-file projects, write each file separately:

```
write_file(path="utils.py", content="# Utility functions\ndef helper(): ...")
write_file(path="main.py", content="from utils import helper\nhelper()")
```

### Step 3: Execute via Shell

Run the script using `run_shell`:

```
run_shell(command="python3 script.py")
```

For scripts in subdirectories:
```
run_shell(command="cd mydir && python3 script.py")
```

### Step 4: Handle Output and Clean Up

- Parse stdout/stderr from `run_shell` output
- Inspect created files directly using `read_file` if needed
- Remove temporary scripts after successful execution:
  ```
  run_shell(command="rm script.py")
  ```

## Advantages Over Sandbox Execution

| Benefit | Explanation |
|---------|-------------|
| Bypasses provider limitations | No sandbox resource constraints |
| Better error visibility | Full stack traces and system errors |
| Environment control | Direct access to system Python and packages |
| Multi-file support | Easy imports and module structure |
| Persistence | Files remain for inspection and debugging |
| Reliability | More consistent execution behavior |

## Complete Example

```
# Scenario: execute_code_sandbox failing on data processing task

# Step 1: Write the script
write_file(
    path="process_data.py",
    content="#!/usr/bin/env python3
import json
import csv

# Load and process data
with open('input.json', 'r') as f:
    data = json.load(f)

# Transform data
results = []
for item in data:
    results.append({'processed': item['value'] * 2})

# Write output
with open('output.csv', 'w', newline='') as f:
    writer = csv.DictWriter(f, fieldnames=['processed'])
    writer.writeheader()
    writer.writerows(results)

print('Processing complete')
"
)

# Step 2: Execute via shell
run_shell(command="python3 process_data.py")

# Step 3: Read results
read_file(filetype="csv", file_path="output.csv")

# Step 4: Clean up (optional)
run_shell(command="rm process_data.py")
```

## Best Practices

1. **Use absolute or clear relative paths** - Avoid ambiguity in file locations
2. **Add error handling in scripts** - Catch exceptions and print meaningful messages
3. **Validate script content before writing** - Ensure proper Python syntax
4. **Log execution steps** - Track what was written and executed for debugging
5. **Clean up temporary files** - Remove scripts after use unless needed for later inspection

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `python3` not found | Try `python` or specify full path `/usr/bin/python3` |
| Import errors | Use `run_shell(command="pip3 install package")` first |
| Permission denied | Add execute permission: `run_shell(command="chmod +x script.py")` |
| Working directory issues | Use absolute paths or `cd` in the command |