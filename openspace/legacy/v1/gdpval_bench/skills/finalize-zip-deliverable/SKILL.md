---
name: finalize-zip-deliverable
description: Ensures codebase deliverables are properly archived into a ZIP file before marking task complete
---

# Finalize ZIP Deliverable

**CRITICAL:** This skill defines a MANDATORY workflow that MUST be executed before any task can be marked complete. Task completion is BLOCKED until ZIP verification passes.

Agents often assume that creating the project directory and files constitutes task completion. However, many tasks require a single downloadable artifact (ZIP file) for delivery. **You are forbidden from declaring task completion until the ZIP artifact is created and verified.**


**ITERATION-AWARE PRIORITY:** If the agent's iteration count approaches the task limit (e.g., iteration 25 of 30), ZIP creation MUST be elevated to highest priority. Do not continue adding features or refining code—execute the ZIP command immediately with whatever files exist.

## Early Warning System

**TRIGGER CONDITION:** When iteration count reaches 25/30 (or 80% of any iteration limit), activate the following protocol:

1. **Immediately halt** any non-essential code generation or refinement.
2. **Assess current state:** List all files currently in the project directory.
3. **Execute minimum viable ZIP:** Package whatever exists, even if incomplete.
4. **Document the fallback:** Note in completion summary that ZIP was created as fallback due to iteration constraints.

**RATIONALE:** A partial ZIP deliverable is ALWAYS preferable to zero deliverable. Never exhaust iterations without creating the required artifact.

## Minimum Viable ZIP Fallback

When triggered by the early warning system, execute this fallback procedure:

```bash
# Create ZIP with whatever files currently exist
zip -r project_fallback.zip ./project/ 2>/dev/null || zip -r project_fallback.zip ./*

# Verify creation succeeded
ls -lh project_fallback.zip
```

**Acceptable fallback scenarios:**
- ZIP contains partial codebase (better than nothing)
- ZIP contains only configuration files
- ZIP contains even a single source file

**Unacceptable outcome:** No ZIP file created due to iteration exhaustion.

## Workflow Steps

### 1. Verify Project Completeness

### 0. Monitor Iteration Count (ONGOING)
Throughout task execution, track your iteration count relative to the task limit.
- **Normal operation (iterations 1-24 of 30):** Follow standard workflow, building codebase systematically.
- **Warning zone (iterations 25-27 of 30):** Begin preparing ZIP command; ensure project directory exists.
- **Critical zone (iterations 28-30 of 30):** ZIP creation is the ONLY remaining task. Execute minimum viable ZIP fallback if necessary.

Before archiving, confirm that all intended source code, configuration files, and assets exist within the project directory.
- Check that the root project directory exists.
- Verify critical files (e.g., `package.json`, `README.md`, source files) are present.
- Ensure no build artifacts or temporary files unintended for delivery are included (unless specified).

### 2. Execute Archive Command
Run the compression command explicitly as a dedicated action. Do not bundle this step with code generation.

```bash
zip -r <output_filename>.zip <project_directory>/
```

**Example:**
```bash
zip -r project.zip ./project
```

### 3. Verify Artifact Creation
Confirm the ZIP file was successfully created and is not empty.
- Check file existence: `ls -lh <output_filename>.zip`
- Check file size: Ensure the size is greater than 0 bytes.
- Optionally inspect contents: `unzip -l <output_filename>.zip`

### 4. Declare Completion
## 4. Mandatory Pre-Completion Checklist (BLOCKING)

**BEFORE declaring task complete, you MUST verify all items below. Task completion is BLOCKED until this checklist passes:**

- [ ] All required source code and configuration files exist in the project directory
**ITERATION STATUS CHECK:** If approaching iteration limit (≥25/30), execute ZIP fallback immediately regardless of project completeness.

- [ ] All required source code and configuration files exist in the project directory **(OR fallback triggered due to iteration limit)**
- [ ] `zip -r <output_filename>.zip <project_directory>/` command has been executed
- [ ] If in fallback mode, ZIP filename includes `fallback` suffix for clarity (e.g., `project_fallback.zip`)
- [ ] ZIP file exists at the expected path (verify with `ls -lh <output_filename>.zip`)
- [ ] ZIP file size is greater than 0 bytes
- [ ] ZIP contents have been inspected (optional but recommended: `unzip -l <output_filename>.zip`)

**COMPLETION RULE:** The `zip` command MUST be executed as the FINAL step before any `<COMPLETE>` declaration. Never declare completion after file generation without first creating and verifying the ZIP artifact.

**ITERATION OVERRIDE RULE:** If iteration count ≥25/30, the ZIP command becomes the IMMEDIATE next action, superseding all other pending work.

**FAILURE CONSEQUENCE:** Declaring task complete without ZIP verification = task failure. Directory creation alone does NOT equal deliverable completion.
**CRITICAL FAILURE:** Exhausting all iterations without ZIP creation = complete task failure regardless of code quality.

## Common Pitfalls
- **Assuming Completion:** Stopping after writing the last source file without packaging.
- **Wrong Path:** Zipping the parent directory instead of the project directory, or vice versa.
- **Missing Files:** Hidden files (e.g., `.env`, `.gitignore`) may need explicit inclusion depending on requirements.
- **Tool Availability:** Ensure the `zip` utility is available in the execution environment. If not, install it or use an alternative (e.g., `tar`).
- **Iteration Exhaustion:** Failing to monitor iteration count and running out of budget before ZIP creation.

## Checklist
- [ ] Project directory contains all required files.
- [ ] Iteration count checked; if ≥25/30, fallback ZIP executed immediately.
- [ ] `zip` command executed successfully.
- [ ] ZIP file exists and has non-zero size.
- [ ] Task status updated to complete.
