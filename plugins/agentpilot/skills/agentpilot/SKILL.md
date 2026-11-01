---
name: agentpilot
description: Set up AgentPilot and use its single entry to prepare missing inputs and run configured RSI experiments. Use when the user requests AgentPilot installation or work with AgentPilot.
---

# AgentPilot plugin

Load the installed AgentPilot skill for the user's task. The current agent prepares missing task/eval inputs, then hands a ready environment to the existing AgentPilot runner. This plugin selects that same skill and does not implement a separate improvement loop.

First read `~/.agents/skills/agentpilot/SKILL.md`, or the explicit skill path returned by an earlier installation. Reuse its launcher and follow its instructions. An existing installation does not need to be set up again for each task.

If the skill/runtime is missing, prefer the user's selected AgentPilot checkout and run from its root:

```bash
python install.py --host codex
```

For a published revision when no checkout is available:

```bash
uv tool install 'git+https://github.com/HKUDS/AgentPilot.git#subdirectory=agentpilot' --python 3.12
agentpilot setup --host codex
```

Use the selected source when updating. If uv is unavailable, use a dedicated Python 3.11+ environment, install the core package there, and run its `python -m agentpilot.app setup --host codex`. Read the skill at the returned `skills[].path`; reuse the exact `launcher_argv` rather than relying on PATH. `doctor` checks the installation, not the task's evaluator, model or worker.

The installer preserves locally edited skills. If an existing skill conflicts, use `--skill-dir` to install separately and load the returned skill; do not delete the user's version or alter model/authentication settings. For installation-only requests, report the entry and stop. Otherwise continue through the installed skill, including bare invocations; do not require a preparation session or JSON handoff before the agent can start investigating.
