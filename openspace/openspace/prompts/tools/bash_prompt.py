"""Prompt text for the shell ``bash`` tool.

Implementation notes: ``tools/BashTool/prompt.ts``.

Implemented here:
- ``getDefaultTimeoutMs`` / ``getMaxTimeoutMs`` equivalents
- ``getSimplePrompt`` core guidance
- background, multiple-command, sleep, and git safety branches

Not implemented in this step:
- Ant/internal undercover and skill-shortcut branches; OS has no equivalent
  build flags or attribution system.
- Monitor-tool branch; OS has background bash lifecycle work in phase 24.
"""

from __future__ import annotations

import json
import os
from typing import Iterable

from openspace.prompts.grounding_agent_prompts import prepend_bullets

BASH_TOOL_NAME = "bash"
FILE_READ_TOOL_NAME = "read"
FILE_EDIT_TOOL_NAME = "edit"
FILE_WRITE_TOOL_NAME = "write"
GLOB_TOOL_NAME = "glob"
GREP_TOOL_NAME = "grep"
TODO_WRITE_TOOL_NAME = "todo_write"
AGENT_TOOL_NAME = "agent"

_DEFAULT_BASH_TIMEOUT_MS = 2 * 60 * 1000
_MAX_BASH_TIMEOUT_MS = 10 * 60 * 1000


def get_default_timeout_ms() -> int:
    """OpenSpace ``getDefaultTimeoutMs`` equivalent."""

    return _DEFAULT_BASH_TIMEOUT_MS


def get_max_timeout_ms() -> int:
    """OpenSpace ``getMaxTimeoutMs`` equivalent."""

    return _MAX_BASH_TIMEOUT_MS


def _background_usage_note() -> str | None:
    if _is_env_truthy("OPENSPACE_DISABLE_BACKGROUND_TASKS"):
        return None
    return (
        "You can use the `run_in_background` parameter to run the command in "
        "the background. Only use this if you do not need the result "
        "immediately and are OK being notified when the command completes "
        "later. You do not need to use '&' at the end of the command when "
        "using this parameter. If you need the output before continuing, use "
        "a short follow-up bash command such as `cat <output_path>` or "
        "`tail <output_path>` on the output file path returned by the tool."
    )


def _commit_and_pr_instructions() -> str:
    return f"""# Committing changes with git

Only create commits when requested by the user. If unclear, ask first. When the user asks you to create a new git commit, follow these steps carefully:

Git Safety Protocol:
- NEVER update the git config
- NEVER run destructive git commands (push --force, reset --hard, checkout ., restore ., clean -f, branch -D) unless the user explicitly requests these actions
- NEVER skip hooks (--no-verify, --no-gpg-sign, etc) unless the user explicitly requests it
- NEVER run force push to main/master, warn the user if they request it
- When a pre-commit hook fails, fix the issue and create a NEW commit rather than amending the previous commit
- When staging files, prefer adding specific files by name rather than using `git add -A` or `git add .`

1. Run the following {BASH_TOOL_NAME} commands in parallel when possible:
   - `git status --short` to see changes and untracked files
   - `git diff` to inspect unstaged changes
   - `git diff --staged` to inspect staged changes
   - `git log --oneline -5` to follow the repository's commit style
2. Analyze all staged changes and draft a concise commit message that focuses on why the change was made.
3. Add only relevant files, commit with a HEREDOC message, then run `git status` after the commit completes.
4. If the commit fails due to a hook, fix the issue, re-stage, and create a new commit.

Important notes:
- NEVER use interactive git commands such as `git rebase -i` or `git add -i`.
- DO NOT push to the remote repository unless the user explicitly asks you to do so.
- If there are no changes to commit, do not create an empty commit.

# Creating pull requests
Use the `gh` command via the {BASH_TOOL_NAME} tool for GitHub tasks. If given a GitHub URL, use `gh` to get the information needed.

Before creating a pull request, inspect branch state, push only if needed, and create the PR with a short title and a body that includes summary and test plan.

# Other common operations
- View comments on a Github PR: `gh api repos/foo/bar/pulls/123/comments`"""


def _simple_sandbox_section() -> str:
    try:
        from openspace.services.sandbox import get_process_sandbox_manager

        manager = get_process_sandbox_manager()
        if not manager.is_sandboxing_enabled():
            return ""
        filesystem_config = {
            "read": manager.get_fs_read_config(),
            "write": manager.get_fs_write_config(),
        }
        network_config = manager.get_network_restriction_config()
        ignore_violations = manager.get_ignore_violations()
        allow_unsandboxed = manager.are_unsandboxed_commands_allowed()
    except Exception:
        return ""

    restrictions_lines = [
        "Filesystem: " + json.dumps(_dedupe_nested(filesystem_config), sort_keys=True),
        "Network: " + json.dumps(_dedupe_nested(network_config), sort_keys=True),
    ]
    if ignore_violations:
        restrictions_lines.append(
            "Ignored violations: "
            + json.dumps(_dedupe_nested(ignore_violations), sort_keys=True)
        )

    if allow_unsandboxed:
        sandbox_override_items: list[str | Iterable[str]] = [
            "You should always default to running commands within the sandbox. Do NOT attempt to set `dangerously_disable_sandbox: true` unless:",
            [
                "The user explicitly asks you to bypass sandbox",
                "A specific command just failed and you see evidence of sandbox restrictions causing the failure.",
            ],
            "Evidence of sandbox-caused failures includes:",
            [
                '"Operation not permitted" errors for file/network operations',
                "Access denied to specific paths outside allowed directories",
                "Network connection failures to non-whitelisted hosts",
                "Unix socket connection errors",
            ],
            "When you see evidence of sandbox-caused failure:",
            [
                "Immediately retry with `dangerously_disable_sandbox: true`",
                "Briefly explain the likely sandbox restriction and mention that `/sandbox` manages restrictions.",
                "This will prompt the user for permission",
            ],
            "Treat each command you execute with `dangerously_disable_sandbox: true` individually. Default future commands back to sandbox mode.",
            "Do not suggest adding sensitive paths like ~/.bashrc, ~/.zshrc, ~/.ssh/*, or credential files to the sandbox allowlist.",
        ]
    else:
        sandbox_override_items = [
            "All commands MUST run in sandbox mode - the `dangerously_disable_sandbox` parameter is disabled by policy.",
            "Commands cannot run outside the sandbox under any circumstances.",
            "If a command fails due to sandbox restrictions, work with the user to adjust sandbox settings instead.",
        ]

    items: list[str | Iterable[str]] = [
        *sandbox_override_items,
        "For temporary files, use the `$TMPDIR` environment variable instead of `/tmp` directly.",
    ]

    return "\n".join(
        [
            "",
            "## Command sandbox",
            "By default, your command will be run in a sandbox. This sandbox controls which directories and network hosts commands may access or modify without an explicit override.",
            "",
            "The sandbox has the following restrictions:",
            "\n".join(restrictions_lines),
            "",
            *prepend_bullets(items),
        ]
    )


def get_simple_prompt() -> str:
    """OpenSpace ``getSimplePrompt`` translated for OS tool names."""

    tool_preference_items: list[str | Iterable[str]] = [
        f"File search: Use {GLOB_TOOL_NAME} (NOT find or ls)",
        f"Content search: Use {GREP_TOOL_NAME} (NOT grep or rg)",
        f"Read files: Use {FILE_READ_TOOL_NAME} (NOT cat/head/tail)",
        f"Edit files: Use {FILE_EDIT_TOOL_NAME} (NOT sed/awk)",
        f"Write files: Use {FILE_WRITE_TOOL_NAME} (NOT echo >/cat <<EOF)",
        "Communication: Output text directly (NOT echo/printf)",
    ]

    avoid_commands = "`find`, `grep`, `cat`, `head`, `tail`, `sed`, `awk`, or `echo`"

    multiple_commands_subitems = [
        f"If the commands are independent and can run in parallel, make multiple {BASH_TOOL_NAME} tool calls in a single message. Example: if you need to run `git status` and `git diff`, send a single message with two {BASH_TOOL_NAME} tool calls in parallel.",
        f"If the commands depend on each other and must run sequentially, use a single {BASH_TOOL_NAME} call with `&&` to chain them together.",
        "Use `;` only when you need to run commands sequentially but do not care if earlier commands fail.",
        "DO NOT use newlines to separate commands. Newlines are ok inside quoted strings.",
    ]

    git_subitems = [
        "Prefer to create a new commit rather than amending an existing commit.",
        "Before running destructive operations such as `git reset --hard`, `git push --force`, or `git checkout --`, consider whether there is a safer alternative. Only use destructive operations when the user explicitly asked for them or they are truly the best approach.",
        "Never skip hooks (`--no-verify`) or bypass signing unless the user explicitly asked for it. If a hook fails, investigate and fix the underlying issue.",
    ]

    sleep_subitems = [
        "Do not sleep between commands that can run immediately. Just run them.",
        "If your command is long running and you would like to be notified when it finishes, use `run_in_background`. No sleep needed.",
        "Do not retry failing commands in a sleep loop. Diagnose the root cause.",
        "If a background task's result is needed for the next step, inspect the returned output file path with a short `cat` or `tail` command; do not use the read tool for session task output paths outside the workspace.",
        "If you must poll an external process, use a check command such as `gh run view` rather than sleeping first.",
        "If you must sleep, keep the duration short (1-5 seconds) to avoid blocking the user.",
    ]

    instruction_items: list[str | Iterable[str]] = [
        "If your command will create new directories or files, first use this tool to run `ls` to verify the parent directory exists and is the correct location.",
        'Always quote file paths that contain spaces with double quotes in your command, e.g. `cd "path with spaces/file.txt"`.',
        "Try to maintain your current working directory throughout the session by using absolute paths and avoiding `cd`. You may use `cd` if the user explicitly requests it.",
        f"You may specify an optional timeout in milliseconds, up to {get_max_timeout_ms()}ms / {get_max_timeout_ms() // 60000} minutes. By default, your command will timeout after {get_default_timeout_ms()}ms / {get_default_timeout_ms() // 60000} minutes.",
    ]

    background_note = _background_usage_note()
    if background_note is not None:
        instruction_items.append(background_note)

    instruction_items.extend(
        [
            "When issuing multiple commands:",
            multiple_commands_subitems,
            "For git commands:",
            git_subitems,
            "Avoid unnecessary `sleep` commands:",
            sleep_subitems,
        ]
    )

    return "\n".join(
        [
            "Executes a given bash command and returns its output.",
            "",
            "The working directory persists between commands, but shell state does not. The shell environment is initialized from the user's profile (bash or zsh).",
            "",
            f"IMPORTANT: Avoid using this tool to run {avoid_commands} commands, unless explicitly instructed or after you have verified that a dedicated tool cannot accomplish your task. Instead, use the appropriate dedicated tool as this will provide a much better experience for the user:",
            "",
            *prepend_bullets(tool_preference_items),
            f"While the {BASH_TOOL_NAME} tool can do similar things, it is better to use the built-in tools as they provide a better user experience and make it easier to review tool calls and give permission.",
            "",
            "# Instructions",
            *prepend_bullets(instruction_items),
            _simple_sandbox_section(),
            "",
            _commit_and_pr_instructions(),
        ]
    )


def _is_env_truthy(name: str) -> bool:
    value = os.environ.get(name, "")
    return value.lower() in {"1", "true", "yes", "on"}


def _dedupe_nested(value):
    if isinstance(value, dict):
        return {key: _dedupe_nested(item) for key, item in value.items()}
    if isinstance(value, list):
        seen = set()
        out = []
        for item in value:
            key = json.dumps(item, sort_keys=True) if isinstance(item, (dict, list)) else item
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out
    return value
