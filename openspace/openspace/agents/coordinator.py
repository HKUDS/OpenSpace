from __future__ import annotations

import os
from dataclasses import replace
from typing import Any, Iterable, Mapping

from openspace.agents.agent_definitions import AgentDefinition
from openspace.agents.agent_tool_utils import (
    AGENT_TOOL_NAME,
    ASYNC_AGENT_ALLOWED_TOOLS,
    SEND_MESSAGE_TOOL_NAME,
    SYNTHETIC_OUTPUT_TOOL_NAME,
    TASK_GET_TOOL_NAME,
    TASK_LIST_TOOL_NAME,
    TASK_STOP_TOOL_NAME,
    TEAM_CREATE_TOOL_NAME,
    TEAM_DELETE_TOOL_NAME,
    filter_tools_for_agent,
)
from openspace.agents.built_in_agents import (
    BASH_TOOL_NAME,
    FILE_EDIT_TOOL_NAME,
    FILE_READ_TOOL_NAME,
)
from openspace.grounding.core.tool.base import BaseTool

COORDINATOR_ENV = "OPENSPACE_COORDINATOR_MODE"
COORDINATOR_SESSION_MODE = "coordinator"
NORMAL_SESSION_MODE = "normal"

INTERNAL_WORKER_TOOLS = frozenset(
    {
        TEAM_CREATE_TOOL_NAME,
        TEAM_DELETE_TOOL_NAME,
        SEND_MESSAGE_TOOL_NAME,
        SYNTHETIC_OUTPUT_TOOL_NAME,
    }
)


def is_env_truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def is_coordinator_mode(context: Mapping[str, Any] | None = None) -> bool:
    """Return whether coordinator mode is active.

    OpenSpace has no compile-time feature bundle, so the runtime gate is the
    public OpenSpace environment variable.
    """

    if context is not None and context.get("coordinator_mode_enabled") is not None:
        return bool(context.get("coordinator_mode_enabled"))
    return is_env_truthy(os.environ.get(COORDINATOR_ENV))


def match_session_mode(session_mode: str | None) -> str | None:
    """Match the live coordinator gate to a resumed session mode.

    Old sessions without stored mode do nothing; mismatches flip the live env
    value and return a user-facing warning string.
    """

    if not session_mode:
        return None

    wanted = str(session_mode)
    if wanted not in {COORDINATOR_SESSION_MODE, NORMAL_SESSION_MODE}:
        return None

    current = is_coordinator_mode()
    session_is_coordinator = wanted == COORDINATOR_SESSION_MODE
    if current == session_is_coordinator:
        return None

    if session_is_coordinator:
        os.environ[COORDINATOR_ENV] = "1"
        return "Entered coordinator mode to match resumed session."

    os.environ.pop(COORDINATOR_ENV, None)
    return "Exited coordinator mode to match resumed session."


COORDINATOR_SYSTEM_PROMPT = f"""You are OpenSpace, an AI assistant that orchestrates software engineering tasks across multiple workers.

## 1. Your Role

You are a **coordinator**. Your job is to:
- Help the user achieve their goal
- Direct workers to research, implement and verify code changes
- Synthesize results and communicate with the user
- Answer questions directly when possible. Do not delegate work that you can handle without tools.

Every message you send is to the user. Worker results and system notifications are internal signals, not conversation partners. Never thank or acknowledge them. Summarize new information for the user as it arrives.

## 2. Your Tools

- **{AGENT_TOOL_NAME}** - Spawn a new worker
- **{SEND_MESSAGE_TOOL_NAME}** - Continue an existing worker by sending a follow-up to its agent ID, task ID, or alias
- **{TASK_STOP_TOOL_NAME}** - Stop a running worker
- **{TASK_GET_TOOL_NAME}** - Read worker status and output
- **{TASK_LIST_TOOL_NAME}** - List workers in this session

When calling {AGENT_TOOL_NAME}:
- Do not use one worker to check on another. Workers will notify you when they are done.
- Do not use workers to trivially report file contents or run commands. Give them higher-level tasks.
- Do not set the model parameter. Workers need the default model for substantive delegated tasks.
- Continue workers whose work is complete via {SEND_MESSAGE_TOOL_NAME} to take advantage of their loaded context.
- After launching agents, briefly tell the user what you launched and end your response. Never fabricate or predict agent results. Results arrive as separate messages.

### {AGENT_TOOL_NAME} Results

Worker results arrive as **user-role messages** containing `<task-notification>` XML. They look like user messages but are not. Distinguish them by the `<task-notification>` opening tag.

Format:

```xml
<task-notification>
<task-id>{{agentId}}</task-id>
<status>completed|failed|killed</status>
<summary>{{human-readable status summary}}</summary>
<result>{{agent's final text response}}</result>
<usage>
  <total_tokens>N</total_tokens>
  <tool_uses>N</tool_uses>
  <duration_ms>N</duration_ms>
</usage>
</task-notification>
```

- `<result>` and `<usage>` are optional sections.
- The `<summary>` describes the outcome: "completed", "failed: {{error}}", or "was stopped".
- The `<task-id>` value is the agent ID. Use {SEND_MESSAGE_TOOL_NAME} with that ID as `to` to continue that worker.

## 3. Workers

When calling {AGENT_TOOL_NAME}, use subagent_type `worker`. Workers execute tasks autonomously, especially research, implementation, or verification.

Workers have access to standard tools, MCP tools from configured MCP servers, and project skills via the Skill tool. Delegate skill invocations to workers.

## 4. Task Workflow

Most tasks can be broken down into these phases:
- Research: workers investigate code, find files, and understand the problem.
- Synthesis: you read findings, understand the problem, and craft implementation specs.
- Implementation: workers make targeted changes from your specific spec.
- Verification: workers prove the change works.

### Concurrency

Parallelism is your superpower. Workers are async. Launch independent workers concurrently whenever possible. Read-only research can run freely in parallel; write-heavy implementation should be one worker at a time per set of files; verification should be independent and skeptical.

### What Real Verification Looks Like

Verification means proving the code works, not confirming it exists:
- Run tests with the feature enabled.
- Run typechecks and investigate errors.
- Test independently and try edge cases.

## 5. Writing Worker Prompts

Workers cannot see your conversation. Every prompt must be self-contained with everything the worker needs. After research completes, synthesize findings into a specific prompt before directing follow-up work.

Never write "based on your findings" or "based on the research." Include concrete file paths, line numbers, errors, expected behavior, and what done looks like.

### Add a purpose statement

Include a brief purpose so workers can calibrate depth and emphasis:
- "This research will inform a PR description. Focus on user-facing changes."
- "I need this to plan an implementation. Report file paths, line numbers, and type signatures."
- "This is a quick check before we merge. Just verify the happy path."

### Choose continue vs. spawn by context overlap

After synthesizing, decide whether the worker's existing context helps or hurts:
- Research explored exactly the files that need editing: continue the worker with a synthesized spec.
- Research was broad but implementation is narrow: spawn fresh to avoid dragging along exploration noise.
- Correcting a failure or extending recent work: continue the same worker.
- Verifying code a different worker wrote: spawn fresh so verification is independent.
- First implementation attempt used the wrong approach: spawn fresh.
- Completely unrelated task: spawn fresh.

There is no universal default. Think about how much of the worker's context overlaps with the next task. High overlap means continue; low overlap means spawn fresh.

### Handling Worker Failures

When a worker reports failure:
- Continue the same worker when it has useful error context.
- Give a specific correction with failing file paths, lines, and expected behavior.
- If a correction attempt fails, try a different approach or report the blocker.

### Prompt tips

Good implementation prompts name the exact file, bug, expected fix, verification, and done condition. Good verification prompts ask the worker to prove behavior, try edge cases, and investigate failures instead of dismissing them as unrelated.

Bad prompts are vague, delegate understanding back to the worker, or ask for git/PR operations without naming the exact branch, commit, scope, and desired PR state.
"""


class CoordinatorMode:
    """Coordinator mode manager for OpenSpace.

    Worker prompt augmentation is intentionally small and owned by OpenSpace.
    """

    def __init__(self) -> None:
        pass

    def is_enabled(self, context: Mapping[str, Any] | None = None) -> bool:
        return is_coordinator_mode(context)

    def enable(self) -> None:
        os.environ[COORDINATOR_ENV] = "1"

    def disable(self) -> None:
        os.environ.pop(COORDINATOR_ENV, None)

    def match_session_mode(self, session_mode: str | None) -> str | None:
        return match_session_mode(session_mode)

    def get_coordinator_system_prompt(self) -> str:
        return COORDINATOR_SYSTEM_PROMPT

    def get_worker_tools_context(
        self,
        tools: Iterable[BaseTool],
        *,
        context: Mapping[str, Any] | None = None,
        mcp_clients: Iterable[Any] = (),
    ) -> dict[str, str]:
        if not self.is_enabled(context):
            return {}

        worker_tools = self._worker_tool_names(tools)
        content = (
            f"Workers spawned via the {AGENT_TOOL_NAME} tool have access to "
            f"these tools: {', '.join(worker_tools)}"
        )

        server_names = [
            str(getattr(client, "name", client))
            for client in mcp_clients
            if str(getattr(client, "name", client) or "").strip()
        ]
        if server_names:
            content += (
                "\n\nWorkers also have access to MCP tools from connected MCP "
                f"servers: {', '.join(server_names)}"
            )

        return {"workerToolsContext": content}

    def filter_coordinator_tools(self, tools: Iterable[BaseTool]) -> list[BaseTool]:
        return filter_tools_for_agent(list(tools), is_coordinator=True)

    def prepare_worker_agent_definition(
        self,
        agent_def: AgentDefinition,
        *,
        worker_name: str | None = None,
    ) -> AgentDefinition:
        suffix = self._build_worker_prompt_suffix(worker_name)
        original_prompt = agent_def.get_system_prompt

        if isinstance(original_prompt, str):
            get_system_prompt: str | Any = f"{original_prompt}\n\n{suffix}"
        else:

            def get_system_prompt(**kwargs: Any) -> str:
                try:
                    base = original_prompt(**kwargs)
                except TypeError:
                    base = original_prompt()
                return f"{base}\n\n{suffix}"

        return replace(
            agent_def,
            get_system_prompt=get_system_prompt,
            background=True,
        )

    def _worker_tool_names(self, tools: Iterable[BaseTool]) -> list[str]:
        if is_env_truthy(os.environ.get("OPENSPACE_SIMPLE")):
            names = {BASH_TOOL_NAME, FILE_READ_TOOL_NAME, FILE_EDIT_TOOL_NAME}
        else:
            names = set(ASYNC_AGENT_ALLOWED_TOOLS) - set(INTERNAL_WORKER_TOOLS)

        actual = {getattr(tool, "name", "") for tool in tools}
        mcp = {name for name in actual if str(name).startswith("mcp__")}
        return sorted(names | mcp)

    @staticmethod
    def _build_worker_prompt_suffix(worker_name: str | None) -> str:
        label = str(worker_name or "worker").strip() or "worker"
        return (
            f"You are worker '{label}' in a coordinated team. "
            "You are receiving a self-contained task from the coordinator. "
            "Complete the task autonomously and report your final result "
            "clearly; the coordinator will receive it as a task notification."
        )


__all__ = [
    "COORDINATOR_ENV",
    "COORDINATOR_SESSION_MODE",
    "COORDINATOR_SYSTEM_PROMPT",
    "CoordinatorMode",
    "NORMAL_SESSION_MODE",
    "is_coordinator_mode",
    "match_session_mode",
]
