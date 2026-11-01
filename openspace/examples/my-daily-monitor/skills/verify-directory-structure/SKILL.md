---
name: verify-directory-structure
description: Diagnose and resolve file access issues by first verifying the directory structure.
---

# Verify Directory Structure

This skill provides a systematic approach to diagnosing and resolving file access issues by first verifying the directory structure. It is particularly useful when dealing with file operations, script executions, or any task that involves accessing files.

## Steps

1. **Identify the Target Path**: Determine the file or directory path that is causing the access issue.
2. **Check Directory Existence**: Use the `list_dir` tool to verify if the directory exists and inspect its contents.
3. **Verify Permissions**: Ensure that the user has the necessary permissions to access the directory and its contents.
4. **Resolve Path Issues**: If the directory does not exist, create it using appropriate commands or scripts. If permissions are insufficient, adjust them accordingly.
5. **Retry the Operation**: After resolving the directory structure issues, retry the original operation.

## Example

```bash
# Example of using the list_dir tool to verify directory structure
list_dir --path "/path/to/directory"
```

## Best Practices

- Always verify the directory structure before performing file operations.
- Use absolute paths to avoid ambiguity.
- Log the directory structure for debugging purposes if issues persist.