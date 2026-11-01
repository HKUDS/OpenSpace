---
name: zip-deliverable-finalization
description: Ensure codebase deliverables are properly packaged as ZIP exports in a dedicated final step
---

# ZIP Deliverable Finalization

## Purpose
When a task requires delivering a codebase or project directory, do not assume that creating the project structure equals completion. A dedicated final iteration must explicitly package the deliverable as a ZIP archive.

## When to Apply
Apply this skill when:
- The task involves creating a codebase, project, or multi-file deliverable
- The output should be exportable or downloadable
- A ZIP archive is the expected or implied deliverable format

## Steps

### 1. Recognize Deliverable Requirements
Before starting work, identify if the task requires a ZIP export. Look for phrases like:
- "export as ZIP"
- "package the project"
- "deliverable archive"
- "downloadable codebase"
- Any mention of sharing, exporting, or distributing the project

### 2. Complete All Project Work First
Finish creating all project files, directories, and content. Ensure:
- All code files are written and saved
- All configurations are in place
- All documentation is complete
- The project structure is finalized

### 3. Create Dedicated Final Iteration
Do NOT combine ZIP creation with file creation. Allocate a separate, explicit iteration for packaging:

```
Iteration N: [File creation and project setup]
Iteration N+1: [ZIP packaging and finalization]  <-- Dedicated step
```

### 4. Execute ZIP Command
Run the ZIP command explicitly in the final iteration:

```bash
zip -r project.zip ./project
```

Or for a custom project directory name:
```bash
zip -r {project-name}.zip ./{project-name}
```

### 5. Verify the Archive
Confirm successful creation:
```bash
ls -la project.zip
unzip -l project.zip  # List contents without extracting
```

### 6. Report Completion
Explicitly state that the deliverable has been packaged and is ready. Include:
- Confirmation that ZIP was created
- Location of the ZIP file
- Brief summary of contents

## Common Mistakes to Avoid

| Mistake | Correct Approach |
|---------|------------------|
| Assuming file creation = deliverable complete | Explicitly create ZIP in separate step |
| Combining ZIP with file edits | Dedicate final iteration to packaging only |
| Forgetting to verify ZIP was created | List and confirm archive contents |
| Not mentioning ZIP in final report | Explicitly confirm deliverable is packaged |

## Example

**Task:** Create a smart contract project and deliver as ZIP

**Correct Execution:**
```
Iteration 1: Create contract files, tests, and configs
Iteration 2: Create frontend components
Iteration 3: Run 'zip -r smart-contract-project.zip ./smart-contract-project'
Iteration 4: Verify and report: 'Deliverable packaged successfully'
```

**Incorrect Execution:**
```
Iteration 1: Create all files and mention "project is complete"
[No ZIP step - deliverable incomplete]
```

## Notes
- This pattern applies to any multi-file deliverable requiring archival
- The dedicated iteration approach ensures the packaging step is not overlooked
- Always verify the ZIP exists before marking task complete