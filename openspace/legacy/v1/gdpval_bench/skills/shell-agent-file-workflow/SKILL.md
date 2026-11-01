---
name: shell-agent-file-workflow
description: Delegate file generation to shell_agent when direct tools fail, with systematic file location and retrieval
---

# Shell Agent File Generation Workflow

When direct document or file generation approaches fail, delegate the task to shell_agent which can autonomously write code, execute it, and retry on errors. Generated files may appear in nested workspace directories.

## When to Use

- Direct file generation tools or `create_file` functions fail for complex formats
- Complex file creation requiring multiple steps, libraries, or error recovery
- When you need an autonomous agent to figure out how to accomplish a goal
- File generation that requires iteration and automatic error fixing

## Workflow Steps

### Step 1: Delegate File Generation to shell_agent

Use shell_agent with a clear, specific task description:

```
shell_agent(task="Create a [file type] with [specific content/structure requirements]")
```

The agent will:
- Decide whether to use Python or Bash
- Write and execute code
- Inspect output and iterate
- Automatically retry and fix errors (up to several rounds)

### Step 2: Locate Generated Files

Files may be created in nested directories within the workspace. Use `find` to locate them:

```bash
find . -name "*.extension" -type f
```

For multiple file types:
```bash
find . -type f \( -name "*.pptx" -o -name "*.docx" -o -name "*.pdf" \)
```

For recently modified files (last 10 minutes):
```bash
find . -name "*.extension" -type f -mmin -10
```

### Step 3: Copy Files to Working Directory

Once located, copy the file to your working directory:

```bash
cp /path/to/found/file.extension ./
```

## Example

### Creating a PowerPoint Presentation

```
# Step 1: Delegate to shell_agent
shell_agent(task="Create a 10-slide PowerPoint presentation covering: topic A, topic B, topic C")

# Step 2: Find the generated file
find . -name "*.pptx" -type f

# Output might show: ./workspace/nested/path/presentation.pptx

# Step 3: Copy to working directory
cp ./workspace/nested/path/presentation.pptx ./
```

### Creating a PDF Report

```
# Step 1: Delegate generation
shell_agent(task="Generate a PDF report with charts and tables from the provided data")

# Step 2: Locate output
find . -name "*.pdf" -type f -mmin -5

# Step 3: Retrieve file
cp ./generated/reports/output.pdf ./
```

## Tips

- **Be specific in task description**: Include file format, structure, and content requirements explicitly
- **Check multiple extensions**: If unsure of output format, search for common extensions
- **Use modification time**: Filter by recent files to identify newly created outputs
- **Verify file integrity**: After copying, check that the file is valid and complete

## Troubleshooting

| Issue | Solution |
|-------|----------|
| No files found | Expand search: remove extension filter, increase time window |
| Multiple files found | Sort by modification time: `find . -name "*.ext" -type f -printf '%T@ %p\n' \| sort -n` |
| File is corrupted | Re-run shell_agent with more specific requirements |
| Permission errors | Use `sudo` or check directory permissions |