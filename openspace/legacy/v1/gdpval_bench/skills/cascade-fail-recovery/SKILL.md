---
name: cascade-fail-recovery
description: Handle cascading data retrieval tool failures by falling back to embedded knowledge generation
---

# Cascade Failure Recovery

## Purpose

When multiple data retrieval tools (read_webpage, search_web) fail in succession, immediately fall back to generating content from embedded knowledge rather than attempting repairs or abandoning the task. This preserves task context and ensures completion.

## When to Apply

Use this pattern when you observe:
- 2+ consecutive failures from data retrieval tools
- Network/access errors preventing web content retrieval
- Risk of task abandonment due to tool errors

## Recovery Procedure

### Step 1: Recognize Cascading Failure

Identify the failure pattern:
```
Iteration N: read_webpage failed - connection error
Iteration N+1: search_web failed - timeout/unavailable
Iteration N+2: [risk] Agent may abandon task or switch objectives
```

### Step 2: Preserve Task Context

Before switching strategies, explicitly restate the original objective:
```
ORIGINAL OBJECTIVE: [Restate the core task goal]
CONTEXT PRESERVED: [Key requirements, constraints, deliverables]
```

### Step 3: Invoke Fallback Strategy

Immediately switch to embedded knowledge generation:
1. **Acknowledge the limitation**: Note that external data sources are unavailable
2. **Activate internal knowledge**: Use pre-trained knowledge relevant to the task
3. **Generate content**: Use write_file to create the deliverable from available knowledge
4. **Document the fallback**: Note in the output what information could not be verified externally

### Step 4: Execute write_file

Generate the required document:
```python
# Fallback to generating from embedded knowledge
write_file(
    path="output/document.md",
    content="[Generate content from internal knowledge base]"
)
```

## Example Application

**Scenario**: PACT Act veterans benefits document needed, but web access failing

**Wrong approach** (observed failure):
```
Iter 11: read_webpage failed - access error
Iter 12: search_web failed - unavailable
Iter 13: [ABANDONED] Switched to unrelated musician payroll task
```

**Correct approach** (with this skill):
```
Iter 11: read_webpage failed - access error
Iter 12: search_web failed - unavailable
Iter 13: CASCADE FAIL DETECTED - invoking fallback
Iter 14: write_file - generate PACT Act document from embedded knowledge
      - Note: "External verification unavailable; content based on training knowledge"
```

## Guidelines

1. **Threshold**: Trigger fallback after 2 consecutive retrieval failures
2. **No endless retries**: Do not attempt more than 1 repair/retry cycle
3. **Preserve objective**: Never switch to unrelated tasks when tools fail
4. **Transparency**: Clearly mark any content that lacks external verification
5. **Document limitations**: Note what could not be verified due to tool failures

## Related Skills

- `write-file-fallback`: Generate documents when data sources unavailable
- `task-context-preservation`: Maintain objective continuity through errors