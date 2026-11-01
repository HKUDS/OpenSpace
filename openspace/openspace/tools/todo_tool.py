"""TodoWriteTool.

The engine contract is OpenSpace-owned: the model replaces the whole todo list on
each call, state is stored per agent/session key, and a fully completed list is
cleared from runtime state while the tool output still reports the submitted
``newTodos`` payload.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from openspace.grounding.core.permissions.types import PermissionAllow
from openspace.grounding.core.tool.base import BaseTool
from openspace.grounding.core.types import BackendType, ToolResult, ToolSchema, ToolStatus


TODO_WRITE_TOOL_NAME = "todo_write"
TODO_WRITE_TOOL_ALIAS = "TodoWrite"
TODO_WRITE_MAX_RESULT_SIZE_CHARS = 100_000
TODO_STATUSES = ("pending", "in_progress", "completed")
TodoStatus = Literal["pending", "in_progress", "completed"]

DESCRIPTION = (
    "Update the todo list for the current session. To be used proactively "
    "and often to track progress and pending tasks. Make sure that at least "
    "one task is in_progress at all times. Always provide both content "
    "(imperative) and activeForm (present continuous) for each task."
)

PROMPT = """Use this tool to create and manage a structured task list for your current coding session. This helps you track progress, organize complex tasks, and demonstrate thoroughness to the user.
It also helps the user understand the progress of the task and overall progress of their requests.

## When to Use This Tool
Use this tool proactively in these scenarios:

1. Complex multi-step tasks - When a task requires 3 or more distinct steps or actions
2. Non-trivial and complex tasks - Tasks that require careful planning or multiple operations
3. User explicitly requests todo list - When the user directly asks you to use the todo list
4. User provides multiple tasks - When users provide a list of things to be done (numbered or comma-separated)
5. After receiving new instructions - Immediately capture user requirements as todos
6. When you start working on a task - Mark it as in_progress BEFORE beginning work. Ideally you should only have one todo as in_progress at a time
7. After completing a task - Mark it as completed and add any new follow-up tasks discovered during implementation

## When NOT to Use This Tool

Skip using this tool when:
1. There is only a single, straightforward task
2. The task is trivial and tracking it provides no organizational benefit
3. The task can be completed in less than 3 trivial steps
4. The task is purely conversational or informational

NOTE that you should not use this tool if there is only one trivial task to do. In this case you are better off just doing the task directly.

## Examples of When to Use the Todo List

<example>
User: I want to add a dark mode toggle to the application settings. Make sure you run the tests and build when you're done!
Assistant: *Creates todo list with the following items:*
1. Creating dark mode toggle component in Settings page
2. Adding dark mode state management (context/store)
3. Implementing CSS-in-JS styles for dark theme
4. Updating existing components to support theme switching
5. Running tests and build process, addressing any failures or errors that occur
*Begins working on the first task*

<reasoning>
The assistant used the todo list because:
1. Adding dark mode is a multi-step feature requiring UI, state management, and styling changes
2. The user explicitly requested tests and build be run afterward
3. The assistant inferred that tests and build need to pass by adding "Ensure tests and build succeed" as the final task
</reasoning>
</example>

<example>
User: Help me rename the function getCwd to getCurrentWorkingDirectory across my project
Assistant: *Uses grep or search tools to locate all instances of getCwd in the codebase*
I've found 15 instances of 'getCwd' across 8 different files.
*Creates todo list with specific items for each file that needs updating*

<reasoning>
The assistant used the todo list because:
1. First, the assistant searched to understand the scope of the task
2. Upon finding multiple occurrences across different files, it determined this was a complex task with multiple steps
3. The todo list helps ensure every instance is tracked and updated systematically
4. This approach prevents missing any occurrences and maintains code consistency
</reasoning>
</example>


<example>
User: I need to implement these features for my e-commerce site: user registration, product catalog, shopping cart, and checkout flow.
Assistant: *Creates a todo list breaking down each feature into specific tasks based on the project architecture*
Let's start with user registration. This will involve creating database models, API endpoints, and frontend forms that integrate with your existing authentication system.

<reasoning>
The assistant used the todo list because:
1. The user provided multiple complex features to implement in a comma separated list
2. The todo list helps organize these large features into manageable tasks
3. This approach allows for tracking progress across the entire implementation
</reasoning>
</example>

<example>
User: Can you help optimize my React application? It's rendering slowly and has performance issues.
Assistant: *Reviews component structure, render patterns, state management, and data fetching*
After analyzing your codebase, I've identified several performance issues.
*Creates todo list with items like: 1) Implementing memoization for expensive calculations in ProductList, 2) Adding virtualization for long lists in Dashboard, 3) Optimizing image loading in Gallery component, 4) Fixing state update loops in ShoppingCart, 5) Reviewing bundle size and implementing code splitting*
Let's start by implementing memoization for the expensive calculations in your ProductList component.

<reasoning>
The assistant used the todo list because:
1. First, the assistant examined the codebase to identify specific performance issues
2. Based on this analysis, it identified multiple optimization opportunities
3. Performance optimization is a non-trivial task requiring multiple steps
4. The todo list helps methodically track improvements across different components
5. This systematic approach ensures all performance bottlenecks are addressed
</reasoning>
</example>

## Examples of When NOT to Use the Todo List

<example>
User: How do I print 'Hello World' in Python?
Assistant: In Python, you can print "Hello World" with this simple code:

python
print("Hello World")

This will output the text "Hello World" to the console when executed.

<reasoning>
The assistant did not use the todo list because this is a single, trivial task that can be completed in one step. There's no need to track multiple tasks or steps for such a straightforward request.
</reasoning>
</example>

<example>
User: What does the git status command do?
Assistant: The git status command shows the current state of your working directory and staging area. It displays which changes have been staged, which haven't, and which files aren't being tracked by Git. This is a fundamental command for understanding the current state of your repository before making commits.

<reasoning>
The assistant did not use the todo list because this is an informational request with no actual coding task to complete. The user is simply asking for an explanation, not for the assistant to perform multiple steps or tasks.
</reasoning>
</example>

<example>
User: Can you add a comment to the calculateTotal function to explain what it does?
Assistant: Sure, let me add a comment to the calculateTotal function to explain what it does.
* Uses the edit tool to add a comment to the calculateTotal function *

<reasoning>
The assistant did not use the todo list because this is a single, straightforward task confined to one location in the code. Adding a comment doesn't require tracking multiple steps or systematic organization.
</reasoning>
</example>

<example>
User: Run npm install for me and tell me what happens.
Assistant: I'll run the npm install command for you.

*Executes: npm install*

The command completed successfully. Here's the output:
[Output of npm install command]

All dependencies have been installed according to your package.json file.

<reasoning>
The assistant did not use the todo list because this is a single command execution with immediate results. There are no multiple steps to track or organize, making the todo list unnecessary for this straightforward task.
</reasoning>
</example>

## Task States and Management

1. **Task States**: Use these states to track progress:
   - pending: Task not yet started
   - in_progress: Currently working on (limit to ONE task at a time)
   - completed: Task finished successfully

   **IMPORTANT**: Task descriptions must have two forms:
   - content: The imperative form describing what needs to be done (e.g., "Run tests", "Build the project")
   - activeForm: The present continuous form shown during execution (e.g., "Running tests", "Building the project")

2. **Task Management**:
   - Update task status in real-time as you work
   - Mark tasks complete IMMEDIATELY after finishing (don't batch completions)
   - Exactly ONE task must be in_progress at any time (not less, not more)
   - Complete current tasks before starting new ones
   - Remove tasks that are no longer relevant from the list entirely

3. **Task Completion Requirements**:
   - ONLY mark a task as completed when you have FULLY accomplished it
   - If you encounter errors, blockers, or cannot finish, keep the task as in_progress
   - When blocked, create a new task describing what needs to be resolved
   - Never mark a task as completed if:
     - Tests are failing
     - Implementation is partial
     - You encountered unresolved errors
     - You couldn't find necessary files or dependencies

4. **Task Breakdown**:
   - Create specific, actionable items
   - Break complex tasks into smaller, manageable steps
   - Use clear, descriptive task names
   - Always provide both forms:
     - content: "Fix authentication bug"
     - activeForm: "Fixing authentication bug"

When in doubt, use this tool. Being proactive with task management demonstrates attentiveness and ensures you complete all requirements successfully.
"""


@dataclass(frozen=True)
class TodoItem:
    content: str
    status: TodoStatus
    activeForm: str

    @property
    def active_form(self) -> str:
        return self.activeForm

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "TodoItem":
        status = str(raw.get("status", ""))
        return cls(
            content=str(raw.get("content", "")),
            status=status,  # type: ignore[arg-type]
            activeForm=str(raw.get("activeForm", raw.get("active_form", ""))),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "content": self.content,
            "status": self.status,
            "activeForm": self.activeForm,
        }


def is_todo_feature_enabled() -> bool:
    try:
        from openspace.services.runtime_support.settings import get_setting

        return bool(get_setting("todoFeatureEnabled", True))
    except Exception:
        return True


def normalize_todos(raw_todos: Sequence[Any]) -> list[TodoItem]:
    return [
        TodoItem.from_mapping(item)
        for item in raw_todos
        if isinstance(item, Mapping)
    ]


def validate_todo_payload(input_data: Mapping[str, Any]) -> str | None:
    raw_todos = input_data.get("todos")
    if not isinstance(raw_todos, list):
        return "todos must be an array"
    todos = normalize_todos(raw_todos)
    if len(todos) != len(raw_todos):
        return "each todo must be an object"
    for index, todo in enumerate(todos):
        if not todo.content.strip():
            return f"todos[{index}].content cannot be empty"
        if todo.status not in TODO_STATUSES:
            return (
                f"todos[{index}].status must be one of: "
                "pending, in_progress, completed"
            )
        if not todo.activeForm.strip():
            return f"todos[{index}].activeForm cannot be empty"
    return None


def make_todo_item_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "The imperative form describing what needs to be done "
                    '(for example, "Run tests").'
                ),
            },
            "status": {
                "type": "string",
                "enum": list(TODO_STATUSES),
                "description": "The current task state.",
            },
            "activeForm": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "The present continuous form shown while this task is in "
                    'progress (for example, "Running tests").'
                ),
            },
        },
        "required": ["content", "status", "activeForm"],
        "additionalProperties": False,
    }


def make_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": make_todo_item_schema(),
                "description": "The updated todo list",
            },
        },
        "required": ["todos"],
        "additionalProperties": False,
    }


def make_output_schema() -> dict[str, Any]:
    todo_list_schema = {
        "type": "array",
        "items": make_todo_item_schema(),
    }
    return {
        "type": "object",
        "properties": {
            "oldTodos": {
                **todo_list_schema,
                "description": "The todo list before the update",
            },
            "newTodos": {
                **todo_list_schema,
                "description": "The todo list after the update",
            },
            "verificationNudgeNeeded": {
                "type": "boolean",
                "description": (
                    "Whether the tool result included a verification-agent "
                    "reminder."
                ),
            },
        },
        "required": ["oldTodos", "newTodos"],
    }


def get_todo_key(context: Any | None) -> str:
    if context is None:
        return "primary"
    agent_id = getattr(context, "agent_id", None)
    if agent_id:
        return str(agent_id)
    session_id = getattr(context, "session_id", None)
    if session_id:
        return str(session_id)
    return "primary"


def todo_list_to_dicts(todos: Sequence[TodoItem | Mapping[str, Any]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for item in todos:
        if isinstance(item, TodoItem):
            result.append(item.to_dict())
        elif isinstance(item, Mapping):
            result.append(TodoItem.from_mapping(item).to_dict())
    return result


class TodoWriteTool(BaseTool):
    _name = TODO_WRITE_TOOL_NAME
    _description = DESCRIPTION
    backend_type = BackendType.META
    aliases = [TODO_WRITE_TOOL_ALIAS]
    should_defer = True
    search_hint = "manage the session task checklist"
    max_result_size_chars = TODO_WRITE_MAX_RESULT_SIZE_CHARS

    def __init__(self) -> None:
        self._current_context: Any | None = None
        super().__init__(
            schema=ToolSchema(
                name=self._name,
                description=DESCRIPTION,
                parameters=make_input_schema(),
                return_schema=make_output_schema(),
                backend_type=self.backend_type,
            )
        )

    def set_context(self, context: Any) -> None:
        self._current_context = context

    def is_enabled(self) -> bool:
        return is_todo_feature_enabled()

    def get_prompt(self, context: Any = None) -> str:
        return PROMPT

    def user_facing_name(self) -> str:
        return ""

    def to_auto_classifier_input(self, input_data: Mapping[str, Any]) -> str:
        raw_todos = input_data.get("todos") if isinstance(input_data, Mapping) else []
        return f"{len(raw_todos)} items" if isinstance(raw_todos, list) else "0 items"

    async def validate_input(
        self,
        input: dict[str, Any],
        context: Any = None,
    ) -> str | None:
        return validate_todo_payload(input)

    async def check_permissions(
        self,
        input: dict[str, Any],
        context: Any = None,
    ) -> PermissionAllow:
        return PermissionAllow(updated_input=dict(input))

    async def _arun(self, todos: list[dict[str, Any]]) -> ToolResult:
        context = self._current_context
        todo_key = get_todo_key(context)
        todo_state = _get_context_todo_state(context)
        old_todos = todo_list_to_dicts(todo_state.get(todo_key, []))
        parsed_todos = normalize_todos(todos)
        submitted_todos = [todo.to_dict() for todo in parsed_todos]
        all_done = all(todo.status == "completed" for todo in parsed_todos)
        stored_todos = [] if all_done else submitted_todos
        todo_state[todo_key] = stored_todos

        verification_nudge_needed = _verification_nudge_needed(context, parsed_todos, all_done)
        data: dict[str, Any] = {
            "oldTodos": old_todos,
            "newTodos": submitted_todos,
        }
        if verification_nudge_needed:
            data["verificationNudgeNeeded"] = True
        else:
            data["verificationNudgeNeeded"] = False

        emit_event = getattr(context, "emit_event", None)
        if emit_event is not None:
            await emit_event(
                "todo_update",
                {
                    "todo_key": todo_key,
                    "agent_id": getattr(context, "agent_id", None),
                    "session_id": getattr(context, "session_id", None),
                    "oldTodos": old_todos,
                    "newTodos": submitted_todos,
                    "storedTodos": stored_todos,
                    "todos": stored_todos,
                    "all_done": all_done,
                    "verificationNudgeNeeded": verification_nudge_needed,
                },
            )

        content = (
            "Todos have been modified successfully. Ensure that you continue "
            "to use the todo list to track your progress. Please proceed with "
            "the current tasks if applicable"
        )
        if verification_nudge_needed:
            content += (
                "\n\nNOTE: You just closed out 3+ tasks and none of them was "
                "a verification step. Before writing your final summary, "
                'spawn the verification agent (subagent_type="verification"). '
                "You cannot self-assign PARTIAL by listing caveats in your "
                "summary - only the verifier issues a verdict."
            )

        return ToolResult(
            status=ToolStatus.SUCCESS,
            content=content,
            metadata={
                "tool": self.name,
                "todo_key": todo_key,
                "oldTodos": old_todos,
                "newTodos": submitted_todos,
                "storedTodos": stored_todos,
                "verificationNudgeNeeded": verification_nudge_needed,
                "data": data,
            },
        )


def _get_context_todo_state(context: Any | None) -> dict[str, list[dict[str, str]]]:
    if context is None:
        return {}
    state = getattr(context, "todo_state", None)
    if isinstance(state, dict):
        return state
    state = {}
    try:
        setattr(context, "todo_state", state)
    except Exception:
        pass
    return state


def _verification_nudge_needed(
    context: Any | None,
    todos: Sequence[TodoItem],
    all_done: bool,
) -> bool:
    if not all_done or len(todos) < 3:
        return False
    if getattr(context, "agent_id", None) not in (None, "", "primary"):
        return False
    if not _is_env_truthy(os.environ.get("OPENSPACE_VERIFICATION_AGENT_NUDGE")):
        return False
    return not any("verif" in todo.content.lower() for todo in todos)


def _is_env_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


__all__ = [
    "TODO_WRITE_TOOL_ALIAS",
    "DESCRIPTION",
    "PROMPT",
    "TODO_STATUSES",
    "TODO_WRITE_MAX_RESULT_SIZE_CHARS",
    "TODO_WRITE_TOOL_NAME",
    "TodoItem",
    "TodoStatus",
    "TodoWriteTool",
    "get_todo_key",
    "is_todo_feature_enabled",
    "make_input_schema",
    "make_output_schema",
    "make_todo_item_schema",
    "normalize_todos",
    "todo_list_to_dicts",
    "validate_todo_payload",
]
