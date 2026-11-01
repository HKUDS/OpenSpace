---
name: shell-agent-file-ops
description: Leverage shell_agent for autonomous file creation and modification with automatic tool selection and error recovery
---

# Shell Agent File Operations

## Purpose

Use `shell_agent` for file operations when you want the tool to autonomously decide how to accomplish the task (Python vs Bash, which commands to use, error recovery) rather than manually selecting specific tools like `write_file` or `run_shell`.

## When to Use

Use this pattern when:
- You need to create, modify, or organize files
- The optimal approach (Python script, Bash commands, direct file write) is unclear
- You want automatic error handling and retry logic
- You prefer to describe **what** to accomplish rather than **how**

## How to Use

### Basic Pattern

```
Delegate to shell_agent with a clear natural-language task description.
```

### Example: Create a Document File

**Task description to shell_agent:**
```
Create a comprehensive SOAP note file containing four sections:
Subjective, Objective, Assessment, and Plan. Include all relevant
patient visit details in a professional medical format.
```

The shell_agent will:
1. Decide whether to use Python or Bash
2. Write and execute the appropriate code
3. Handle any errors automatically (up to several retry rounds)
4. Return the result

### Example: Modify Existing Files

**Task description to shell_agent:**
```
Update all configuration files in the config/ directory to add
a new setting "timeout: 300" under the general section.
```

### Example: Complex File Operations

**Task description to shell_agent:**
```
Create a project structure with README.md, src/main.py, and
tests/test_main.py. Include appropriate content in each file
based on a Python data processing project template.
```

## Best Practices

1. **Be specific about content requirements** - Describe what should be in the file(s), not just that files should be created.

2. **Include format specifications** - Mention desired formats (markdown, JSON, CSV, etc.) when relevant.

3. **Specify file locations** - Include paths when files should be in specific directories.

4. **Let shell_agent handle complexity** - Don't pre-decide the implementation approach; trust the autonomous selection.

5. **Check the output** - Review what shell_agent created to ensure it meets requirements.

## Comparison: When NOT to Use This Pattern

Use direct tools instead when:
- You already know the exact command/script to run → use `run_shell`
- You have the exact file content ready → use `write_file` directly
- The operation is trivial (single-line file write) → use `write_file`
- You need fine-grained control over the implementation

## Anti-Patterns to Avoid

❌ Don't over-specify the implementation:
```
Use Python to write a file called notes.md with this content...
```

✅ Do describe the desired outcome:
```
Create a notes.md file with comprehensive documentation about...
```

❌ Don't use shell_agent for simple direct operations:
```
Write "hello" to hello.txt
```

✅ Do use `write_file` directly for simple cases:
```
write_file(path="hello.txt", content="hello")
```

## Summary

This pattern shifts focus from **implementation mechanics** to **task intent**, leveraging shell_agent's autonomous decision-making for file operations that benefit from intelligent tool selection and automatic error recovery.