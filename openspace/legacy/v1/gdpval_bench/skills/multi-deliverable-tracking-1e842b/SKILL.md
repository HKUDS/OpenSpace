---
name: multi-deliverable-tracking-1e842b
description: Track completion status of all required deliverables and ensure ALL outputs are completed before stopping
---

# Multi-Deliverable Task Tracking

## Purpose

This skill ensures that agents working on tasks with multiple required outputs explicitly track which deliverables are complete and prioritize completing ALL required outputs before considering the task finished.

## Problem Pattern

Agents often stop after completing the first or partial set of deliverables, even when multiple outputs are explicitly required. This leads to incomplete task execution.

## Instructions

### Step 1: Identify All Required Deliverables

At the start of any task, explicitly enumerate ALL required outputs:

```
Required Deliverables Checklist:
- [ ] Deliverable 1: [description]
- [ ] Deliverable 2: [description]
- [ ] Deliverable 3: [description]
```

### Step 2: Track Progress Explicitly

Maintain a visible progress tracker throughout task execution. Update it after each deliverable is completed:

```
Progress Tracker:
- [x] Email to Juvoxa CEO (COMPLETE)
- [ ] Email to Second Company CEO (PENDING)
```

### Step 3: Verify Before Stopping

Before concluding the task, perform a final verification:

1. Review the original task requirements
2. Check each item in your deliverables checklist
3. Confirm ALL items are marked complete
4. Only stop when every required deliverable is finished

### Step 4: Prioritize Completion Over Speed

If you encounter difficulties with one deliverable:
- Do NOT abandon remaining deliverables
- Attempt alternative approaches (switch tools, methods, etc.)
- Complete what you can before reporting any blockers

## Example Workflow

```
Task: Create emails to two company CEOs

1. INITIALIZE CHECKLIST:
   - [ ] Email to Juvoxa CEO
   - [ ] Email to PartnerCo CEO

2. EXECUTE DELIVERABLE 1:
   - Create email to Juvoxa CEO
   - Save/output the file
   - UPDATE: - [x] Email to Juvoxa CEO

3. CHECK STATUS:
   - 1 of 2 complete
   - Continue to next deliverable

4. EXECUTE DELIVERABLE 2:
   - Create email to PartnerCo CEO
   - Save/output the file
   - UPDATE: - [x] Email to PartnerCo CEO

5. FINAL VERIFICATION:
   - All checkmarks complete? YES
   - Task is now complete
```

## Anti-Patterns to Avoid

- ❌ Stopping after first deliverable without checking requirements
- ❌ Assuming "good enough" without verifying all outputs
- ❌ Not maintaining visible progress tracking
- ❌ Abandoning remaining deliverables when one becomes difficult

## When to Apply

Use this skill whenever a task requires:
- Multiple files to be created
- Multiple outputs to different recipients
- Several distinct deliverables specified in requirements
- Any situation where "done" could be ambiguous