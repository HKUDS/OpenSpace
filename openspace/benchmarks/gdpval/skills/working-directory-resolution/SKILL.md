---
name: working-directory-resolution
description: Resolve file access failures by changing to the correct working directory before command execution
---

# Working Directory Resolution Pattern

Many file operation failures occur because the tool's current working directory doesn't match where the target files are located. This skill provides a reliable pattern for resolving such issues.

## Problem Symptoms

File operations fail with errors like:
- "File not found" or "No such file or directory"
- "Permission denied" (when path is actually relative)
- Tools returning empty results for files that should exist

These failures often occur even when files are present in the workspace—the working directory context is simply wrong.

## Solution

**Always explicitly set the working directory before file operations** by prepending `cd` to your shell commands.

## Implementation Pattern

### Basic Syntax
```bash
cd /path/to/target/directory && your-command-here
```

### With `run_shell`
```bash
run_shell(command="cd /workspace/project && cat config.json")
```

### Multiple Operations in Same Directory
```bash
cd /workspace/project && ls -la && cat README.md && python script.py
```

### Conditional Directory Change
```bash
cd /workspace/project 2>/dev/null && cat file.txt || echo "Directory not found"
```

## When to Apply

Use this pattern when:
- `read_file` fails to locate an existing file
- `execute_code_sandbox` can't find referenced files
- Shell commands report missing files that should exist
- File paths work in some contexts but not others
- You're unsure of the tool's current working directory

## Best Practices

1. **Use absolute paths when possible**
   ```bash
   cd /workspace/project/src && python main.py
   ```

2. **Combine related operations** to avoid repeated `cd` calls
   ```bash
   cd /workspace/project && ./build.sh && ./test.sh
   ```

3. **Verify directory exists before operations**
   ```bash
   [ -d /workspace/project ] && cd /workspace/project && ls
   ```

4. **For scripts, set working directory at the start**
   ```bash
   #!/bin/bash
   cd "$(dirname "$0")" || exit 1
   # Rest of script runs from script's directory
   ```

## Common Pitfalls

| Issue | Wrong Approach | Correct Approach |
|-------|---------------|------------------|
| Relative paths | `cat config.json` | `cd /workspace && cat config.json` |
| Assumed cwd | `python script.py` | `cd /workspace && python script.py` |
| Multiple dirs | `cd dir1; cd dir2; cmd` | `cd dir1/dir2 && cmd` |

## Fallback Strategy

If `cd` also fails:
1. Use `pwd` to check current directory
2. Use `find` or `ls -R` to locate files
3. Verify the path structure with `ls -la /path/to/check`

```bash
pwd && find /workspace -name "target_file.txt" 2>/dev/null
```

---

This pattern is especially valuable when tools like `execute_code_sandbox` or `read_file` fail but `run_shell` with explicit directory context succeeds.