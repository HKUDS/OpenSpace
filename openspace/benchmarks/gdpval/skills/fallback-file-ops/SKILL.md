---
name: fallback-file-ops
description: Use run_shell as fallback when execute_code_sandbox or shell_agent fail on filesystem operations
---

# Fallback File Operations

## Purpose

When `execute_code_sandbox` or `shell_agent` fail with 'unknown error' on filesystem operations, fall back to `run_shell` with explicit `mkdir` and file write commands. This hybrid approach is more reliable for creating complex project structures.

## When to Apply

- `execute_code_sandbox` returns 'unknown error' on file/directory creation
- `shell_agent` fails to write files or create nested directories
- You need to create complex project structures with multiple levels

## Procedure

### Step 1: Detect Tool Failure

 Monitor for these failure patterns:
- Error message contains "unknown error"
- File operations silently fail (file not created)
- Directory creation returns success but directory doesn't exist

### Step 2: Switch to run_shell

When failures occur, immediately switch to `run_shell` for filesystem operations:

```bash
# Create directory structure explicitly
run_shell: mkdir -p /path/to/nested/directory

# Write files using shell redirection or echo
run_shell: echo "content" > /path/to/file.txt

# Or use cat with heredoc for multi-line files
run_shell: cat > /path/to/file.txt << 'EOF'
line 1
line 2
EOF
```

### Step 3: Verify Creation

After each operation, verify the file/directory was created:

```bash
run_shell: ls -la /path/to/created/item
run_shell: test -f /path/to/file && echo "File exists"
run_shell: test -d /path/to/dir && echo "Directory exists"
```

### Step 4: Continue with Hybrid Approach

- Use `run_shell` for all filesystem operations (mkdir, write, copy, move)
- Continue using `execute_code_sandbox` or `shell_agent` for code execution, compilation, or other non-filesystem tasks
- Document which tool handles which operation for clarity

## Example: Creating Project Structure

```yaml
# Failed attempt with execute_code_sandbox
execute_code_sandbox: create directory src/components

# Fallback with run_shell
run_shell: mkdir -p src/components
run_shell: mkdir -p src/utils
run_shell: mkdir -p tests/unit
run_shell: echo "// Component file" > src/components/Button.tsx
run_shell: echo "// Utility file" > src/utils/helpers.ts
```

## Best Practices

1. **Be explicit**: Always use full paths or confirm working directory
2. **Create parent directories**: Use `mkdir -p` for nested structures
3. **Verify before proceeding**: Check files exist before dependent operations
4. **Log tool switches**: Note when you fall back to run_shell for debugging
5. **Batch operations**: Group related file operations in consecutive run_shell calls

## Common Pitfalls

- Don't assume filesystem state after tool failure
- Don't mix tools for the same file operation (pick one and stick with it)
- Don't skip verification steps when using fallback