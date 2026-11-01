---
name: detect-zero-iteration-failure
description: Identify and classify agent failures occurring before execution starts
---

# Detect Zero-Iteration Failure

This skill provides a workflow for identifying and classifying agent failures that occur prior to any task execution.

## Identification Criteria

Flag a task execution as a **Zero-Iteration Failure** if all of the following conditions are met:

1.  **Iteration Count:** The agent reports 0 completed iterations.
2.  **Tool Usage:** No tools were invoked during the session.
3.  **Artifacts:** No documents, files, or code artifacts were created.
4.  **Conversation Log:** The log ends abruptly or contains only an initial error message without agent reasoning steps.

## Diagnosis Strategy

When a Zero-Iteration Failure is detected, do **not** attempt standard agent-level debugging (e.g., analyzing reasoning chains or tool parameters). Instead, investigate system-level causes:

1.  **Environment Health:** Check if the runtime environment initialized correctly (e.g., dependencies installed, API keys loaded).
2.  **Prompt Parsing:** Verify if the initial user instruction was malformed or triggered safety filters immediately.
3.  **Initialization Crash:** Look for stack traces or panic logs occurring before the first agent step.
4.  **Resource Limits:** Check if memory or quota limits were hit before execution began.

## Action Plan

1.  **Tag the Failure:** Label the incident as `failure_mode: pre_execution` rather than `failure_mode: agent_logic`.
2.  **Escalate:** Route to infrastructure or platform engineering rather than prompt engineering.
3.  **Retry Strategy:** If automating retries, reset the environment context before retrying, as the previous context may be poisoned.

## Example Indicators

```text
Status: Failed
Iterations: 0
Tools Called: []
Error: "Session initialization failed: Missing environment variable"
```