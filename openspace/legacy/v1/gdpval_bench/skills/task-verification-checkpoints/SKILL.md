---
name: task-verification-checkpoints
description: Verify output format, file coverage, and task alignment before completing
---

# Task Verification Checkpoints

Before declaring a task as `<COMPLETE>`, perform these verification checks to ensure the deliverable matches requirements and prevents wrong-task completion.

## Overview

This skill prevents common failures where agents:
- Create outputs in wrong formats (PDF vs PPTX vs DOCX)
- Skip processing some reference files
- Drift from the actual task requirements

## Three Critical Checkpoints

### Checkpoint 1: Output Format Verification

**Question:** Does the output file format match task requirements?

**Actions:**
1. Re-read the task prompt for explicit format requirements
2. Check file extensions of all output files
3. Verify the actual file type matches the extension
4. Confirm against any format specifications (e.g., "revised PDF report")

**Example validation:**
```python
def verify_output_format(task_prompt, output_files):
    """Check if output format matches task requirements."""
    required_format = extract_format_requirement(task_prompt)  # e.g., 'pdf', 'pptx'
    
    for f in output_files:
        actual_ext = f.split('.')[-1].lower()
        if actual_ext != required_format:
            return False, f"Expected .{required_format}, got .{actual_ext}"
    
    return True, "Format verified"
```

### Checkpoint 2: Reference File Coverage

**Question:** Were ALL reference/input files processed?

**Actions:**
1. List all files mentioned in the task as inputs
2. Confirm each was read/analyzed/used (check file access logs)
3. Document which content from each file contributed to output
4. Flag any unprocessed reference files

**Example validation:**
```python
def verify_reference_coverage(task_files, accessed_files):
    """Ensure all reference files were actually processed."""
    missing = set(task_files) - set(accessed_files)
    if missing:
        return False, f"Unprocessed files: {missing}"
    return True, "All references processed"
```

### Checkpoint 3: Task Alignment Verification

**Question:** Does the deliverable address the ACTUAL task prompt?

**Actions:**
1. Re-read the original task description verbatim
2. Compare output purpose against stated task goal
3. Check for scope creep or task drift
4. Verify the output solves the stated problem (not a different one)

**Example validation:**
```python
def verify_task_alignment(task_prompt, output_description):
    """Confirm output addresses the actual task."""
    task_verbs = extract_action_verbs(task_prompt)  # e.g., 'review', 'revise', 'create'
    output_verbs = extract_action_verbs(output_description)
    
    if not set(task_verbs).issubset(set(output_verbs)):
        return False, "Output actions don't match task requirements"
    
    return True, "Task alignment verified"
```

## Pre-Completion Checklist

Execute this checklist before marking ANY task complete:

```python
def pre_completion_verification(task_prompt, output_files, reference_files):
    """
    Complete verification before declaring task done.
    Returns (success, message) tuple.
    """
    checks = []
    
    # Check 1: Format
    format_ok, format_msg = verify_output_format(task_prompt, output_files)
    checks.append(('Format', format_ok, format_msg))
    
    # Check 2: Coverage
    coverage_ok, coverage_msg = verify_reference_coverage(reference_files, get_accessed_files())
    checks.append(('Coverage', coverage_ok, coverage_msg))
    
    # Check 3: Alignment
    alignment_ok, alignment_msg = verify_task_alignment(task_prompt, get_output_summary())
    checks.append(('Alignment', alignment_ok, alignment_msg))
    
    # Report results
    all_passed = all(ok for _, ok, _ in checks)
    
    if not all_passed:
        print("❌ VERIFICATION FAILED - Do not mark complete")
        for name, ok, msg in checks:
            status = "✓" if ok else "✗"
            print(f"  {status} {name}: {msg}")
        return False, "Verification failed"
    
    print("✓ All verification checkpoints passed")
    return True, "Ready for completion"
```

## When to Apply

Use this skill whenever:
- Creating deliverables from input/reference files
- Task specifies output format requirements
- Multiple files must be processed/analyzed
- Task involves transformation, revision, or synthesis
- **Always** before any `<COMPLETE>` declaration

## Common Failure Modes to Catch

| Failure Type | Example | Prevention |
|-------------|---------|------------|
| Format mismatch | Created PPTX when PDF requested | Checkpoint 1 |
| Incomplete coverage | Skipped Photographs.zip | Checkpoint 2 |
| Task drift | Created new report instead of revising existing | Checkpoint 3 |
| Assumption errors | Assumed format without verification | All checkpoints |

## Quick Reference

```
Before <COMPLETE>:
  □ Output format matches requirements?
  □ All reference files processed?
  □ Deliverable addresses actual task?
  
If ANY box unchecked → Do NOT complete, fix first.
```