---
name: locate-missing-artifacts
description: Recover generated files in sandboxed environments by searching from confirmed roots and copying to expected locations
---

# Locate Missing Artifacts in Sandboxed Environments

## When to Use This Skill

Use this skill when:
- A tool reports success but files are not found in their expected locations
- You receive errors like "file not found" after a tool claims to have created it
- You're working in a sandboxed/ephemeral environment where file paths may shift
- Directory structure mismatches occur between tool execution and validation

## Step-by-Step Instructions

### Step 1: Confirm the Failure

Before searching, verify the file is actually missing from the expected location:

```bash
ls -la /expected/path/filename.ext
# or
test -f /expected/path/filename.ext && echo "Found" || echo "Missing"
```

### Step 2: Identify Confirmed Root Directories

Determine which directories you know exist and where tools typically write:

```bash
# Common roots to check
ls -la /
ls -la $HOME
ls -la /tmp
ls -la /workspace
ls -la .
```

### Step 3: Search for the Artifact Using `find`

Use `find` from confirmed root directories to locate the missing file:

```bash
# Search by filename
find /workspace -name "filename.ext" 2>/dev/null

# Search by pattern if exact name varies
find /workspace -name "*.ext" 2>/dev/null

# Search broader paths if needed
find / -name "filename.ext" 2>/dev/null | head -20
```

### Step 4: Copy Artifact to Expected Location

Once located, explicitly copy the file to where it should be:

```bash
# Copy to expected location
cp /found/path/filename.ext /expected/path/filename.ext

# Or if you need to preserve the original
cp -r /found/path/filename.ext /expected/path/

# Verify the copy succeeded
ls -la /expected/path/filename.ext
```

### Step 5: Validate Before Proceeding

Confirm the file is now accessible at the expected location:

```bash
# Test file exists and is readable
test -f /expected/path/filename.ext && test -r /expected/path/filename.ext && echo "Ready"

# Check file size to ensure it's not empty
stat /expected/path/filename.ext
```

## Common Patterns

### Pattern: Output Written to Wrong Directory

```bash
# Tool wrote to current directory instead of specified output dir
find . -name "*.docx" -type f
cp ./report.docx ./output/report.docx
```

### Pattern: Temporary Directory Used

```bash
# File ended up in /tmp or similar
find /tmp -name "*.pdf" -mtime -1
cp /tmp/generated.pdf ./deliverables/
```

### Pattern: Nested Subdirectory Created

```bash
# Tool created extra directory levels
find /workspace -type f -name "*.xlsx"
# May find: /workspace/output/2024/reports/file.xlsx
cp /workspace/output/2024/reports/file.xlsx /workspace/deliverables/
```

## Prevention Tips

1. **Always specify absolute paths** when giving tools output locations
2. **Check the working directory** before and after tool execution: `pwd`
3. **Verify immediately** after a tool reports success before proceeding
4. **Use tool-specific confirmations** when available (e.g., return paths)

## Example: Complete Recovery Workflow

```bash
# 1. Tool reported creating report.docx but it's missing
ls ./deliverables/report.docx
# Output: ls: cannot access './deliverables/report.docx': No such file or directory

# 2. Search for it
find /workspace -name "report.docx" 2>/dev/null
# Output: /workspace/tmp/generated/report.docx

# 3. Copy to expected location
mkdir -p ./deliverables
cp /workspace/tmp/generated/report.docx ./deliverables/report.docx

# 4. Validate
ls -la ./deliverables/report.docx
# Output: -rw-r--r-- 1 user user 45678 Jan 15 10:30 ./deliverables/report.docx

# Success - file is now where it should be
```

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `find` returns no results | Expand search to broader paths (`/`, `/home`, `/root`) |
| Multiple matches found | Check timestamps (`-mtime -1`) or file sizes to identify the correct one |
| Permission denied on copy | Use `sudo` if available, or check if file is in a read-only mount |
| File exists but is empty | The tool may have failed silently; regenerate the artifact |