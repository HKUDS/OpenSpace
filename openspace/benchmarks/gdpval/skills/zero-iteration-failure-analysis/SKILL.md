---
name: zero-iteration-failure-analysis
description: Identify and handle agent failures with 0 iterations as pre-execution system issues
---

# Zero-Iteration Failure Analysis

This skill provides a workflow for detecting and responding to agent failures that occur before any iterations are executed. These failures indicate system-level problems rather than task execution issues.

## When to Apply

Use this skill when analyzing agent execution results and you observe:
- **0 iterations** reported in the execution summary
- **No tool invocations** in the conversation log
- **No artifacts created** (no files, outputs, or intermediate results)
- **Error message** present but no agent reasoning visible

## Identification Checklist

A zero-iteration failure is confirmed when **ALL** of the following are true:

```
[ ] Iteration count = 0
[ ] Tool usage count = 0  
[ ] No conversation log beyond initial instruction
[ ] Error/self-report indicates failure before execution began
```

## Failure Mode Classification

| Indicator | Failure Type | Investigation Level |
|-----------|--------------|---------------------|
| 0 iterations, 0 tools | Pre-execution | System-level |
| 1+ iterations, error mid-task | Execution | Agent-level |
| 1+ iterations, wrong output | Reasoning | Agent-level |

## Investigation Steps

### Step 1: Confirm Zero-Iteration Status

```python
def is_zero_iteration_failure(execution_result):
    """Check if failure occurred before any agent iterations."""
    return (
        execution_result.get('iterations', 0) == 0 and
        execution_result.get('tool_calls', []) == [] and
        execution_result.get('artifacts', []) == []
    )
```

### Step 2: Extract Error Context

Examine any error message or self-report for clues:
- **Initialization errors**: Environment setup, dependency missing, config invalid
- **Prompt parsing errors**: Malformed instruction, missing required fields
- **Resource errors**: Memory limits, timeout before start, permission denied

### Step 3: Route to Appropriate Investigation

```
IF zero-iteration failure detected:
    → Escalate to SYSTEM investigation
    → Do NOT attempt agent-level debugging
    → Check: environment, dependencies, prompt format, resource limits
ELSE:
    → Proceed with standard agent failure analysis
```

### Step 4: Document the Failure Mode

Record the failure with appropriate categorization:

```yaml
failure_analysis:
  type: zero-iteration
  severity: high
  investigation_level: system
  agent_debugging_appropriate: false
  recommended_actions:
    - Check execution environment health
    - Verify prompt/input formatting
    - Review system logs for initialization errors
    - Validate resource availability
```

## Common Causes and Resolutions

| Cause | Symptoms | Resolution |
|-------|----------|------------|
| Environment crash | Immediate error, no context | Restart environment, check dependencies |
| Prompt parse failure | Error mentions instruction format | Validate input schema, fix formatting |
| Resource exhaustion | Timeout or memory error before start | Increase limits, optimize initialization |
| Permission denied | Access error on startup | Check file/system permissions |

## Escalation Criteria

Immediately flag for system-level review when:
- Zero-iteration failures occur repeatedly (>2 in same session)
- Error messages indicate infrastructure issues
- Multiple agents fail with same zero-iteration pattern

## Example Analysis

```
EXECUTION SUMMARY:
- Iterations: 0
- Tools Used: None
- Artifacts: None
- Status: Failed
- Error: "Agent initialization failed: missing required config"

ANALYSIS:
✓ Zero-iteration failure detected
✓ Pre-execution failure mode confirmed
→ Action: Escalate to system investigation
→ Do NOT debug agent reasoning (no reasoning occurred)
→ Check: config loading, environment variables, initialization sequence
```

## Anti-Patterns to Avoid

❌ **Don't** attempt to debug agent reasoning when 0 iterations occurred
❌ **Don't** assume the task instructions were unclear (agent never saw them)
❌ **Don't** retry with modified prompts before checking system health
❌ **Don't** categorize as "agent performance issue"

## Integration with Failure Tracking

When logging failures, Tag zero-iteration failures distinctly:

```json
{
  "failure_id": "xyz123",
  "failure_mode": "pre_execution",
  "iteration_count": 0,
  "requires_system_review": true,
  "agent_actionable": false
}
```