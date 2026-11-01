"""Built-in OpenSpace tools that are not tied to a single backend module.

Imports are intentionally lazy.  Some tools pull in heavier optional runtime
stacks (for example WebFetchTool -> LLMClient -> LiteLLM); importing this
package should not initialize those stacks when a caller only needs a different
tool module.
"""

from importlib import import_module
from typing import Any


_EXPORT_MODULES = {
    "ASK_USER_QUESTION_TOOL_NAME": ".ask_user_tool",
    "AskUserQuestionTool": ".ask_user_tool",
    "AnswerAnnotation": ".ask_user_tool",
    "BRIEF_TOOL_NAME": ".brief_tool",
    "BriefTool": ".brief_tool",
    "CONFIG_TOOL_NAME": ".config_tool",
    "ConfigTool": ".config_tool",
    "Question": ".ask_user_tool",
    "QuestionOption": ".ask_user_tool",
    "MemoryReadTool": ".memory_tools",
    "MemoryTarget": ".memory_tools",
    "MemoryWriteTool": ".memory_tools",
    "LSP_TOOL_NAME": ".lsp_tool",
    "LSPTool": ".lsp_tool",
    "SCHEDULE_CRON_CREATE_TOOL_NAME": ".schedule_cron_tool",
    "SCHEDULE_CRON_DELETE_TOOL_NAME": ".schedule_cron_tool",
    "SCHEDULE_CRON_LIST_TOOL_NAME": ".schedule_cron_tool",
    "ScheduleCronCreateTool": ".schedule_cron_tool",
    "ScheduleCronDeleteTool": ".schedule_cron_tool",
    "ScheduleCronListTool": ".schedule_cron_tool",
    "SLEEP_TOOL_NAME": ".sleep_tool",
    "SleepTool": ".sleep_tool",
    "NOTEBOOK_EDIT_TOOL_ALIAS": ".notebook_edit_tool",
    "ensure_memory_file": ".memory_tools",
    "format_memory_targets": ".memory_tools",
    "get_relative_memory_path": ".memory_tools",
    "list_memory_targets": ".memory_tools",
    "NOTEBOOK_EDIT_TOOL_NAME": ".notebook_edit_tool",
    "NotebookEditTool": ".notebook_edit_tool",
    "TaskGetTool": ".task_tools",
    "TaskListTool": ".task_tools",
    "TaskStopTool": ".task_tools",
    "SendMessageTool": ".team_tools",
    "TeamCreateTool": ".team_tools",
    "TeamDeleteTool": ".team_tools",
    "TodoItem": ".todo_tool",
    "TodoWriteTool": ".todo_tool",
    "TODO_WRITE_TOOL_NAME": ".todo_tool",
    "WebFetchTool": ".web_fetch_tool",
    "WebSearchTool": ".web_search_tool",
}

__all__ = [
    "ASK_USER_QUESTION_TOOL_NAME",
    "AskUserQuestionTool",
    "AnswerAnnotation",
    "BRIEF_TOOL_NAME",
    "BriefTool",
    "CONFIG_TOOL_NAME",
    "ConfigTool",
    "MemoryReadTool",
    "MemoryTarget",
    "MemoryWriteTool",
    "LSP_TOOL_NAME",
    "LSPTool",
    "SCHEDULE_CRON_CREATE_TOOL_NAME",
    "SCHEDULE_CRON_DELETE_TOOL_NAME",
    "SCHEDULE_CRON_LIST_TOOL_NAME",
    "ScheduleCronCreateTool",
    "ScheduleCronDeleteTool",
    "ScheduleCronListTool",
    "SLEEP_TOOL_NAME",
    "SleepTool",
    "NOTEBOOK_EDIT_TOOL_ALIAS",
    "NOTEBOOK_EDIT_TOOL_NAME",
    "NotebookEditTool",
    "Question",
    "QuestionOption",
    "SendMessageTool",
    "TaskGetTool",
    "TaskListTool",
    "TaskStopTool",
    "TeamCreateTool",
    "TeamDeleteTool",
    "TodoItem",
    "TodoWriteTool",
    "TODO_WRITE_TOOL_NAME",
    "WebFetchTool",
    "WebSearchTool",
    "ensure_memory_file",
    "format_memory_targets",
    "get_relative_memory_path",
    "list_memory_targets",
]


def __getattr__(name: str) -> Any:
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(module_name, __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value
