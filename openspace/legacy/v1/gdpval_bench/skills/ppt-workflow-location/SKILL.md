---
name: ppt-workflow-location
description: Generate PowerPoint files using shell_agent and locate them in nested workspace directories
---

# PowerPoint Generation and Location Workflow

## When to Use This Skill

Use this workflow when:
- Direct document generation tools fail or are unavailable
- You need to create PowerPoint presentations with specific slide content and structure
- Output files may be created in unexpected nested directories within the workspace

## Step 1: Generate PowerPoint via shell_agent

Delegate the PowerPoint creation task to shell_agent with clear specifications:

```
Use shell_agent to create a PowerPoint presentation with:
- Specify the exact number of slides needed
- Define the content/topic for each slide
- Request the file be saved with a clear filename (e.g., presentation.pptx)
- Ask the agent to confirm the file path after creation
```

**Example task description:**
```
Create a PowerPoint presentation with 10 slides covering [topic]. Each slide should have a title and bullet points. Save it as presentation.pptx and tell me the full file path.
```

## Step 2: Locate the Output File

PowerPoint files created by shell_agent may be in nested workspace directories. Use the `find` command to locate them:

```bash
# Find all .pptx files in the workspace
find . -name "*.pptx" -type f

# Or search for a specific filename
find . -name "presentation.pptx" -type f
```

## Step 3: Copy to Working Directory

Once located, copy the file to your working directory:

```bash
# Copy the found file to current directory
cp /path/to/found/presentation.pptx .

# Or move it if you prefer
mv /path/to/found/presentation.pptx .
```

## Step 4: Verify the File

Confirm the file exists and is accessible:

```bash
# List the file with details
ls -la presentation.pptx

# Check file size to ensure it's not empty
file presentation.pptx
```

## Common Pitfalls

1. **Don't assume the file is in the current directory** - shell_agent may create files in subdirectories
2. **Don't skip the find step** - always verify the file location before proceeding
3. **Check file size** - ensure the PowerPoint file is not empty (0 bytes)
4. **Request confirmation** - ask shell_agent to report the file path after creation

## Example Workflow

```bash
# Step 1: shell_agent creates the presentation (done via task delegation)

# Step 2: Find the created file
find . -name "*.pptx" -type f
# Output: ./workspace/task_123/output/presentation.pptx

# Step 3: Copy to working directory
cp ./workspace/task_123/output/presentation.pptx .

# Step 4: Verify
ls -la presentation.pptx
# Output: -rw-r--r-- 1 user user 45632 Jan 15 10:30 presentation.pptx
```

## Troubleshooting

| Problem | Solution |
|---------|----------|
| File not found with find | Search broader: `find / -name "*.pptx" 2>/dev/null` |
| File is 0 bytes | Re-run shell_agent task; ensure it completed successfully |
| Permission denied | Check directory permissions with `ls -la` on parent dirs |
| Multiple .pptx files found | Use more specific search: `find . -name "*presentation*.pptx"` |