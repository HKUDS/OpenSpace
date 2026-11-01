---
name: robust-file-creation
description: Fallback to run_shell and write_file when execute_code_sandbox or shell_agent fail on filesystem operations
---

# Robust File Creation Strategy

When creating complex project structures, automated tools like `execute_code_sandbox` and `shell_agent` may fail with 'unknown error' on filesystem operations. This skill provides a reliable fallback strategy using explicit shell commands.

## When to Use

Apply this pattern when:
- `execute_code_sandbox` fails with unknown error on file/directory creation
- `shell_agent` fails to complete filesystem operations after retries
- You need to create nested directory structures with multiple files
- Task progress is blocked by filesystem tool failures

## Fallback Procedure

### Step 1: Detect the Failure
When either `execute_code_sandbox` or `shell_agent` fails on filesystem operations (especially after automatic retries), immediately switch to the manual hybrid approach.

### Step 2: Create Directories Explicitly
Use `run_shell` with `mkdir -p` for each required directory:

```bash
run_shell command="mkdir -p /path/to/nested/directory"
```

The `-p` flag ensures parent directories are created automatically.

### Step 3: Write Files Individually
Use `write_file` tool for each file with explicit path and content:

```
write_file path="/path/to/file.py" content="..."
```

Do not attempt to batch multiple file creations in a single operation.

### Step 4: Verify Creation (Optional)
Confirm the structure was created correctly:

```bash
run_shell command="ls -la /path/to/directory"
# or
run_shell command="tree /path/to/directory"
```

## Complete Example Flow

```
[execute_code_sandbox fails on project setup]
        ↓
[run_shell mkdir -p src/components]
[run_shell mkdir -p src/utils]
[run_shell mkdir -p tests]
        ↓
[write_file path="src/main.py" content="..."]
[write_file path="src/components/widget.py" content="..."]
[write_file path="tests/test_main.py" content="..."]
        ↓
[run_shell ls -la src/  # verify]
```

## Best Practices

1. **Create all directories first** before writing any files to avoid path errors
2. **Write files one at a time** rather than attempting batch operations
3. **Use absolute or clearly relative paths** to avoid ambiguity
4. **Verify after creation** when working on critical deliverables
5. **Document the structure** in comments or SKILL.md if it's complex

## Why This Works

- `run_shell` executes commands directly without the abstraction layer that can fail
- `write_file` is a primitive operation with fewer failure modes
- Separating directory creation from file writing isolates potential failure points
- Explicit commands are easier to debug when issues occur

## Anti-Patterns to Avoid

❌ Don't keep retrying the same failing `execute_code_sandbox` command
❌ Don't attempt to create complex nested structures in a single Python script via sandbox
❌ Don't assume directories exist before writing files to them
❌ Don't skip verification on critical project scaffolding