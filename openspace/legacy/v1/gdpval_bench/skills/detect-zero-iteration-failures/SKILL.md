---
name: detect-zero-iteration-failures
description: Identify and handle pre-execution agent failures occurring before any iterations or tool usage
---

# Detect Zero-Iteration Failures

This skill helps identify and properly categorize agent failures that occur before any task execution begins. These failures require system-level investigation rather than agent-level debugging.

## When to Apply

Use this skill when analyzing agent task executions where:

1. **Iteration count is 0** - The agent completed zero iterations
2. **No tool usage** - No tools were invoked during the execution
3. **No artifacts created** - No files, documents, or outputs were produced
4. **Minimal or no conversation log** - Only the user instruction exists, with no agent responses

## Identification Checklist

Check the following indicators to confirm a zero-iteration failure:

```
[ ] Iterations reported: 0
[ ] Tool invocations: None
[ ] Files created: None  
[ ] Conversation turns: 1 (user instruction only)
[ ] Agent self-report: Indicates failure before execution began
```

## Failure Categories

Zero-iteration failures typically indicate one of these pre-execution issues:

| Category | Description | Investigation Focus |
|----------|-------------|---------------------|
| Initialization crash | Agent failed during setup | System logs, environment config |
| Environment issue | Missing dependencies or resources | Infrastructure, permissions |
| Prompt parsing error | Input could not be processed | Prompt format, encoding |
| Resource exhaustion | Quota or limits exceeded | System capacity, rate limits |

## Diagnostic Steps

### Step 1: Verify Zero-Iteration Indicators

```python
def is_zero_iteration_failure(execution_log):
    """Check if execution shows zero-iteration failure pattern."""
    indicators = {
        'iterations': execution_log.get('iterations', 0) == 0,
        'tools_used': len(execution_log.get('tool_calls', [])) == 0,
        'artifacts': len(execution_log.get('files_created', [])) == 0,
        'conversation_length': len(execution_log.get('messages', [])) <= 1
    }
    return all(indicators.values())
```

### Step 2: Check System-Level Indicators

Look for these error patterns in system logs:

- **Environment errors**: Missing packages, permission denied, path not found
- **Initialization errors**: Connection failures, timeout during setup
- **Resource errors**: Memory exceeded, quota limits, rate limiting
- **Parsing errors**: Invalid JSON, encoding issues, malformed input

### Step 3: Route Investigation Appropriately

```
IF zero-iteration failure detected:
    └── Do NOT debug agent logic or prompt instructions
    └── DO investigate:
        ├── System initialization logs
        ├── Environment configuration
        ├── Resource allocation and limits
        └── Input parsing and validation
```

## Response Actions

### For Analysts

1. **Flag as system-level issue** - Do not attribute to agent behavior
2. **Check infrastructure health** - Verify system components are operational
3. **Review recent changes** - Look for deployments, config updates, or quota changes
4. **Escalate appropriately** - Route to infrastructure/platform team, not agent developers

### For Automated Systems

```python
def categorize_failure(execution_log):
    """Categorize failure type for routing."""
    if is_zero_iteration_failure(execution_log):
        return {
            'category': 'PRE_EXECUTION_FAILURE',
            'severity': 'HIGH',
            'investigation_team': 'INFRASTRUCTURE',
            'agent_debug_required': False,
            'recommended_actions': [
                'Check system initialization logs',
                'Verify environment configuration',
                'Review resource quotas and limits',
                'Validate input parsing pipeline'
            ]
        }
    else:
        return {
            'category': 'EXECUTION_FAILURE',
            'severity': 'MEDIUM',
            'investigation_team': 'AGENT_DEVELOPMENT',
            'agent_debug_required': True
        }
```

## Example Analysis

**Zero-Iteration Failure Example:**
```
Task ID: 69a8ef86-phase1
Iterations: 0
Tools Used: None
Files Created: None
Messages: [User instruction only]
Agent Report: "Failed before executing any iterations"

Analysis: PRE_EXECUTION_FAILURE
- No agent logic was executed
- Failure occurred during initialization
- Action: Investigate system environment, not agent prompts
```

**Normal Execution Failure (for contrast):**
```
Task ID: abc123
Iterations: 3
Tools Used: [read_file, write_file, shell_agent]
Files Created: [output.txt]
Messages: [User instruction, Agent response x3]
Agent Report: "Could not complete task due to X"

Analysis: EXECUTION_FAILURE
- Agent logic was executed
- Failure occurred during task performance
- Action: Debug agent reasoning and tool usage
```

## Key Takeaways

1. **Zero iterations = Pre-execution failure** - The agent never got to work
2. **System-level, not agent-level** - Debug infrastructure, not prompts
3. **High severity** - Indicates potential systemic issues affecting multiple tasks
4. **Distinct failure mode** - Treat separately from normal execution failures