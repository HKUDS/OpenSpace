---
name: zip-export-deliverable
description: Ensures codebase deliverables are properly exported as ZIP archives through a dedicated finalization step
---

# ZIP Export Deliverable Finalization

## Purpose

When a task requires delivering a codebase or project directory as a ZIP archive, this skill ensures the deliverable is actually created through an explicit finalization step, rather than assuming directory creation equals completion.

## When to Apply

Use this skill when:
- The task deliverable is a codebase, project, or directory structure
- The deliverable must be exported or packaged as a ZIP file
- You have already created the project files and directory structure

## Common Pitfall

**Do NOT assume** that creating the project directory and files means the deliverable is complete. The ZIP archive itself must be explicitly created as a final step.

## Step-by-Step Instructions

### Step 1: Verify Project Structure

Before creating the ZIP, confirm all required files and directories exist:

```bash
ls -la ./project/
# or
find ./project -type f | head -20
```

### Step 2: Create Dedicated Final Iteration

Add a final step/iteration specifically for ZIP creation. This should be a distinct action, not combined with other tasks.

### Step 3: Execute ZIP Command

Run the zip command with recursive flag:

```bash
zip -r project.zip ./project
```

Or with a custom name:

```bash
zip -r <deliverable-name>.zip ./<project-directory>
```

### Step 4: Verify ZIP Creation

Confirm the ZIP file was created successfully:

```bash
ls -lh project.zip
# Should show file size > 0
unzip -l project.zip | head -20
# Should list contents
```

### Step 5: Report Completion

Explicitly confirm the ZIP deliverable has been created in your final response to the user.

## Code Example

```python
import subprocess
import os

def finalize_deliverable(project_dir="project", zip_name="project.zip"):
    """Create ZIP archive of project deliverable."""
    
    # Step 1: Verify project exists
    if not os.path.exists(project_dir):
        raise FileNotFoundError(f"Project directory {project_dir} not found")
    
    # Step 2: Create ZIP
    result = subprocess.run(
        ["zip", "-r", zip_name, f"./{project_dir}"],
        capture_output=True,
        text=True
    )
    
    if result.returncode != 0:
        raise RuntimeError(f"ZIP creation failed: {result.stderr}")
    
    # Step 3: Verify
    zip_size = os.path.getsize(zip_name)
    print(f"Created {zip_name} ({zip_size} bytes)")
    
    return zip_name
```

## Checklist

- [ ] All project files are created in the expected directory
- [ ] A dedicated step is allocated for ZIP creation
- [ ] `zip -r <name>.zip ./<directory>` command is executed
- [ ] ZIP file existence and size are verified
- [ ] Deliverable completion is explicitly confirmed to the user

## Related Patterns

- File creation should always include verification steps
- Deliverables should be explicitly validated before marking tasks complete
- Archive/export operations need their own iteration, not implicit completion