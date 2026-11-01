---
name: shell-error-debug-workflow
description: Systematic workflow for diagnosing and resolving unknown errors from run_shell commands
---

# Shell Error Debug Workflow

When `run_shell` returns an 'unknown error' or ambiguous failure, apply this systematic troubleshooting workflow to diagnose and resolve the issue.

## When to Use This Skill

- `run_shell` returns an 'unknown error' message
- Shell command fails without clear error output
- Command works locally but fails in the agent environment
- Intermittent or unexplained shell failures

## Step-by-Step Troubleshooting Workflow

### Step 1: Capture Explicit Stderr

Redirect stderr to stdout to capture all error output:

```bash
# Instead of:
run_shell command="some-command"

# Use:
run_shell command="some-command 2>&1"
```

This ensures error messages are captured in the output rather than being lost.

### Step 2: Verify Working Directory

Confirm the current working directory and its contents:

```bash
run_shell command="pwd && ls -la"
```

Check that:
- You're in the expected directory
- Required files/directories exist
- Permissions are correct

### Step 3: Check Tool/Command Availability

Verify the required command or tool exists and is accessible:

```bash
run_shell command="which some-command || command -v some-command || type some-command"
```

If the tool is not found, check PATH or install the required package.

### Step 4: Use Absolute Paths

Replace relative paths with absolute paths to avoid directory-related issues:

```bash
# Instead of:
run_shell command="python script.py"

# Use:
run_shell command="$(pwd)/script.py"
# or
run_shell command="/absolute/path/to/script.py"
```

### Step 5: Test with Minimal Command

Isolate the issue by running a minimal version of the command:

```bash
# Test basic functionality first
run_shell command="echo 'test'"

# Then gradually add complexity
run_shell command="some-command --version"
run_shell command="some-command --help"
```

### Step 6: Check Environment Variables

Some commands depend on specific environment variables:

```bash
run_shell command="env | grep -i relevant_var"
```

## Complete Diagnostic Sequence

For persistent unknown errors, run this complete diagnostic:

```bash
# 1. Capture full environment
run_shell command="pwd && echo '---' && ls -la && echo '---' && env"

# 2. Test command with full error capture
run_shell command="your-command 2>&1 | head -50"

# 3. Check command availability
run_shell command="which your-command || echo 'Command not found in PATH'"

# 4. Try with absolute path
run_shell command="/full/path/to/your-command 2>&1"
```

## Common Causes and Solutions

| Symptom | Likely Cause | Solution |
|---------|-------------|----------|
| No output, unknown error | stderr not captured | Add `2>&1` to command |
| File not found | Wrong working directory | Use `pwd` to verify, use absolute paths |
| Permission denied | File/directory permissions | Check with `ls -la`, adjust permissions |
| Command not found | Tool not installed or not in PATH | Use `which` to check, install or specify full path |
| Intermittent failures | Race conditions or timing | Add delays, check for file locks |

## Example: Troubleshooting a Python Script Failure

```bash
# Initial failing command
run_shell command="python process.py"
# Returns: unknown error

# Apply diagnostic workflow:
# Step 1: Capture stderr
run_shell command="python process.py 2>&1"

# Step 2: Verify directory
run_shell command="pwd && ls -la"

# Step 3: Check Python availability
run_shell command="which python && python --version"

# Step 4: Use absolute path
run_shell command="$(pwd)/process.py 2>&1"
# or
run_shell command="/usr/bin/python3 $(pwd)/process.py 2>&1"
```

## Best Practices

1. **Always capture stderr** when debugging: `2>&1`
2. **Verify assumptions** about working directory and file locations
3. **Start simple** before running complex commands
4. **Use absolute paths** in production/repeatable scripts
5. **Check tool versions** when behavior seems inconsistent
6. **Document environment** when errors are hard to reproduce

## When to Escalate

If this workflow does not resolve the issue:
- The error may be environmental (container/runtime specific)
- Consider using `shell_agent` for complex shell tasks that require autonomous error handling
- Check for resource constraints (disk space, memory, timeouts)