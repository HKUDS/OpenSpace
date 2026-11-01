---
name: agentpilot
description: Prepare and run AgentPilot improvements through one entry. Use when the user requests AgentPilot or iterative improvement with it. The current agent fills missing task/evaluation inputs, then hands a ready environment to the configured AgentPilot runner.
---

# AgentPilot

Use this one entry to prepare inputs and run the real AgentPilot proposer → worker → eval loop. Read the user's goal, project instructions and relevant existing configuration, preserving their edits and choices. The current agent prepares and operates the runner; the configured engine owns improvement decisions and policy updates.

## Choose the workflow

- **An existing formal run or runnable RSI configuration fits the request:** follow [references/formal.md](references/formal.md) and its existing task, evaluation, worker and budget. Resume its own records when continuing a run. Examples or templates elsewhere in a repository do not by themselves select an experiment.
- **The task, evaluation or runnable setup is missing:** prepare it in the current conversation as described below, then reassess readiness and continue through the same entry. You do not need a session or JSON handoff to begin investigating. A test command alone is not a configured Arena environment.

The installed core launcher, when needed:

```text
{{AGENTPILOT_COMMAND}}
```

## Prepare incomplete inputs

Use your normal reading, editing and command tools. Understand what the project does and inspect relevant tests or failures. Without an explicit task, infer a focused improvement from real project evidence; if the project and conversation cannot establish what to do, ask only for the missing information. A bare invocation is valid, but an empty project is not a reason to invent requirements.

Establish an acceptance criterion that matches that goal. Reuse suitable project tests and prepare missing checks or measurements; use the project's environment rather than assuming the installer contains its dependencies. Optional `project inspect --cwd /absolute/project` suggests test commands without running them. Keep the evaluator separate from the code the worker will improve, with fixed inputs and a meaningful failure condition. Exercise the actual evaluator on the starting program and verify that it detects a relevant failure, using available known cases or an isolated control when needed. An empty test run cannot count as successful evaluation; a broken evaluator or fabricated score cannot qualify a setup.

For performance goals, establish a representative workload, preserve correctness, and measure runtime repeatedly with a stated comparison rule. Passing tests alone does not measure speed. A valid failing baseline can be useful evidence; distinguish task failure from an evaluator that could not run. Subjective criteria can guide preparation, but do not invent a numerical evaluator to force a formal run.

Preparation may add acceptance checks, repair their setup or build a missing initial program when that is the user's task. Leave candidate optimization to the configured worker after handoff. Do not substitute repeated edits by the current agent for the requested RSI loop. Keep useful observations and commands with existing project artifacts or in the conversation; no separate host policy, memory schema or checkpoint system is needed.

## Hand off, or identify the missing prerequisite

Read [references/formal.md](references/formal.md) once a relevant adapter/configuration is available. Assess readiness from its actual configuration and execution evidence: it must use the intended program and evaluator, produce usable baseline/smoke measurements, and have the required worker, proposer/provider and budget settings. Arena research can reuse worker login for proposer and Memory policy calls when their API credentials are absent; task evaluation and UCB embeddings still need their configured providers. Login reuse does not make calls free. Static configuration or manifest validation is insufficient.

When ready, continue directly into the existing runner within the user's request and budget; do not stop after printing a launch command. Use its real run records to establish that proposal, worker execution, evaluation and policy feedback occurred. Resume through that runner after interruption. Once handed off, do not make parallel candidate edits outside its worker.

If the evaluator, adapter, model access or necessary user intent is still missing, report **preparation incomplete**, the checks actually performed and the specific next requirement. Ask only for information the project cannot supply. Do not silently build a generic environment adapter, substitute an unrelated benchmark, or claim RSI has run. Ordinary test discovery is supported; automatic formal integration of arbitrary projects is not yet provided. Respect commit/push restrictions, including when an adapter requires versioned candidate snapshots.
