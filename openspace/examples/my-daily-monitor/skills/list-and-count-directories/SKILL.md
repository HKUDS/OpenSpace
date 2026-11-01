---
name: list-and-count-directories
description: List and count directories in a path by filtering list_dir results for directory entries and excluding hidden/system files
---

# List and Count Directories

This skill provides a systematic workflow for listing and counting directories within a given path using the `list_dir` tool. It filters results to identify directories, excludes hidden and system entries, and presents both an enumerated list and a total count.

## Use Cases

- Project structure analysis
- Skill or module inventory
- Repository exploration
- Directory auditing and documentation

## Workflow

### Step 1: Get Directory Contents

Use `list_dir` to retrieve the contents of the target path:

```
list_dir(path="/path/to/target")
```

### Step 2: Identify Directories

Filter the results to identify directory entries. Directories are marked by permissions starting with `d` in Unix ls format (e.g., `drwxr-xr-x`).

**Recognition pattern:**
- Look for entries where the permissions field starts with `d`
- Common directory permission patterns: `drwxr-xr-x`, `drwxrwxr-x`, `drwx------`, etc.

### Step 3: Exclude Hidden and System Entries

Filter out entries that should not be counted:
- `.` (current directory)
- `..` (parent directory)
- `.DS_Store` (macOS metadata)
- Other hidden files starting with `.` (optional, depending on requirements)

### Step 4: Create Enumerated List

Present the directories as a numbered list for easy reference:

```
1. directory-name-1
2. directory-name-2
3. directory-name-3
...
```

### Step 5: Provide Total Count

Calculate and report the total number of directories found:

```
Total: N directories
```

## Implementation Example

When implementing this workflow programmatically, use the following approach:

```python
# Pseudo-code for filtering directories
directories = []
for entry in dir_listing:
    # Check if entry is a directory (starts with 'd')
    if entry['permissions'].startswith('d'):
        name = entry['name']
        # Exclude hidden/system entries
        if name not in ['.', '..', '.DS_Store']:
            directories.append(name)

# Sort for consistent ordering
directories.sort()

# Present enumerated list
for i, dirname in enumerate(directories, 1):
    print(f"{i}. {dirname}")

# Provide total count
print(f"\nTotal: {len(directories)} directories")
```

## Tips

- **Sorting**: Always sort directory names alphabetically for consistent, predictable output
- **Context**: Mention the path being analyzed for clarity
- **Validation**: If the count is zero, verify the path exists and contains directories
- **Extensions**: This pattern can be adapted to filter for files, specific extensions, or other criteria

## Common Patterns

### Basic directory listing with count:
```
Directories in /skills:
1. authentication-helpers
2. data-processing
3. workflow-automation

Total: 3 directories
```

### With additional context:
```
Found 3 skill directories in /skills:
1. authentication-helpers
2. data-processing  
3. workflow-automation
```

### Including path context:
```
Directory inventory for /path/to/modules:
- module-a
- module-b
- module-c
Count: 3 directories
```

## Related Techniques

- Use similar filtering logic to count files instead of directories (permissions starting with `-`)
- Extend to filter by modification date, size, or naming patterns
- Combine with recursive directory traversal for deep structure analysis