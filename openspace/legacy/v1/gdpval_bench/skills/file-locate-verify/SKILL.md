---
name: file-locate-verify
description: Systematic workflow to resolve FileNotFoundError by locating and verifying file paths
---

# File Locate and Verify Workflow

When encountering FileNotFoundError or uncertain file paths, follow this systematic pattern to safely locate and verify files before executing operations.

## When to Use

- You receive a FileNotFoundError or similar path-related error
- You need to work with files whose exact location is unknown
- You want to verify file existence before executing operations that depend on those files

## Step-by-Step Procedure

### Step 1: Check Current Directory Contents

First, use `list_dir` to inspect what files exist in your current working directory:

```python
# Use the list_dir tool to see current directory contents
list_dir(path=".")
```

This reveals files in the immediate context and helps determine if you need to search elsewhere.

### Step 2: Search for the File Using Find

If the file is not in the current directory, use the `find` command to locate it by name:

```bash
# Search for a specific file by name (case-insensitive)
find . -iname "filename.pdf" 2>/dev/null

# Or search from root for system-wide files
find / -name "filename.pdf" 2>/dev/null
```

**Tips:**
- Use `-iname` for case-insensitive matching
- Redirect stderr with `2>/dev/null` to suppress permission errors
- Narrow the search scope when possible for better performance

### Step 3: Verify the Discovered Path

Before executing any operations on the discovered file, verify it exists and check its properties:

```bash
# Verify file existence and permissions at the full path
ls -la /full/path/to/discovered/file.pdf
```

This confirms:
- The file actually exists at the located path
- File permissions (can you read/write it?)
- File size and modification time

### Step 4: Execute Your Operation Safely

Only after verification, proceed with your intended operation:

```python
# Now safe to work with the verified path
file_path = "/full/path/to/discovered/file.pdf"
# ... your extraction or processing code here
```

## Complete Example

```python
# Task: Process a file that may be in an unknown location

# Step 1: Check current directory
dir_contents = list_dir(path=".")
print(f"Current directory contains: {dir_contents}")

# Step 2: Find the file if not present
if "target_file.xlsx" not in dir_contents:
    result = run_shell(command='find . -iname "target_file.xlsx" 2>/dev/null')
    file_path = result.stdout.strip().split('\n')[0]
    print(f"Found file at: {file_path}")

# Step 3: Verify before proceeding
verification = run_shell(command=f'ls -la {file_path}')
print(f"Verification: {verification.stdout}")

# Step 4: Proceed with operation
# ... your code to process the file
```

## Common Pitfalls to Avoid

1. **Skipping verification**: Never assume the path from `find` is correct without checking
2. **Using relative paths blindly**: Always convert to absolute paths when possible
3. **Ignoring permissions**: `ls -la` reveals if you have read/write access
4. **Hardcoding paths**: Always dynamically locate files when location is uncertain

## Related Tools

- `list_dir`: Inspect directory contents
- `run_shell`: Execute find and ls commands
- `read_file`: Read file contents after verification
- `shell_agent`: Delegate complex file operations after path is confirmed