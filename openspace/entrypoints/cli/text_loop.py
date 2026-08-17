from __future__ import annotations

import asyncio
import logging
import sys
from typing import Optional

from openspace.runtime import ExecutionRequest, ExecutionResult
from openspace import OpenSpace
from openspace.utils.cli_display import CLIDisplay
from openspace.utils.display import colorize
from openspace.utils.logging import Logger
from openspace.utils.ui import OpenSpaceUI
from openspace.utils.ui_integration import UIIntegration

logger = Logger.get_logger(__name__)


def _summary_payload(result: ExecutionResult) -> dict:
    return {
        "status": result.status,
        "response": result.text,
        "user_response": result.text,
        "error": result.error,
        "task_id": result.task_id,
        "session_id": result.session_id,
        "execution_time": result.execution_time,
        "iterations": result.iterations,
        "tool_executions": list(result.tool_executions),
        "completed_tasks": len(result.tool_executions),
        "skills_used": list(result.skills_used),
        "evolved_skills": list(result.evolved_skills),
        "active_skills": list(result.active_skills),
    }


class UIManager:
    def __init__(self, ui: Optional[OpenSpaceUI], ui_integration: Optional[UIIntegration]):
        self.ui = ui
        self.ui_integration = ui_integration
        self._original_log_levels = {}
    
    async def start_live_display(self):
        if not self.ui or not self.ui_integration:
            return
        
        print()
        print(colorize("  ▣ Starting real-time visualization...", 'c'))
        print()
        await asyncio.sleep(1)
        
        self._suppress_logs()
        
        await self.ui.start_live_display()
        await self.ui_integration.start_monitoring(poll_interval=2.0)
    
    async def stop_live_display(self):
        if not self.ui or not self.ui_integration:
            return
        
        await self.ui_integration.stop_monitoring()
        await self.ui.stop_live_display()
        
        self._restore_logs()
    
    def print_summary(self, result: dict):
        if self.ui:
            self.ui.print_summary(result)
        else:
            CLIDisplay.print_result_summary(result)
    
    def _suppress_logs(self):
        log_names = ["openspace", "openspace.grounding", "openspace.agents"]
        for name in log_names:
            log = logging.getLogger(name)
            self._original_log_levels[name] = log.level
            log.setLevel(logging.CRITICAL)
    
    def _restore_logs(self):
        for name, level in self._original_log_levels.items():
            logging.getLogger(name).setLevel(level)
        self._original_log_levels.clear()


async def _execute_task(openspace: OpenSpace, query: str, ui_manager: UIManager):
    await ui_manager.start_live_display()
    result = await openspace.execute(
        ExecutionRequest(
            prompt=query,
            capture_skill_dir=getattr(openspace.config, "capture_skill_dir", None),
        )
    )
    await ui_manager.stop_live_display()
    ui_manager.print_summary(_summary_payload(result))
    return result


async def interactive_mode(openspace: OpenSpace, ui_manager: UIManager) -> bool:
    CLIDisplay.print_interactive_header()
    
    while True:
        try:
            prompt = colorize(">>> ", 'c', bold=True)
            query = input(f"\n{prompt}").strip()
            
            if not query:
                continue
            
            if query.lower() in ['exit', 'quit', 'q']:
                print("\nExiting...")
                break

            if query.lower() == 'status':
                _print_status(openspace)
                continue
            
            if query.lower() == 'help':
                CLIDisplay.print_help()
                continue

            CLIDisplay.print_task_header(query)
            await _execute_task(openspace, query, ui_manager)
            
        except KeyboardInterrupt:
            print("\n\nInterrupt signal detected, exiting...")
            break
        except EOFError:
            print(
                "\nInput stream closed; exiting interactive mode. "
                "Use --query for non-interactive execution.",
                file=sys.stderr,
            )
            return False
        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)
            print(f"\nError: {e}")

    return True


async def single_query_mode(openspace: OpenSpace, query: str, ui_manager: UIManager):
    CLIDisplay.print_task_header(query, title="▶ Single Query Execution")
    await _execute_task(openspace, query, ui_manager)


def _print_status(openspace: OpenSpace):
    """Print system status"""
    from openspace.utils.display import Box, BoxStyle
    
    box = Box(width=70, style=BoxStyle.ROUNDED, color='bl')
    print()
    print(box.text_line(colorize("System Status", 'bl', bold=True), 
                      align='center', indent=4, text_color=''))
    print(box.separator_line(indent=4))
    
    status_lines = [
        f"Initialized: {colorize('Yes' if openspace.is_initialized() else 'No', 'g' if openspace.is_initialized() else 'rd')}",
        f"Running: {colorize('Yes' if openspace.is_running() else 'No', 'y' if openspace.is_running() else 'g')}",
        f"Model: {colorize(openspace.config.llm_model, 'c')}",
    ]
    
    if openspace.is_initialized():
        backends = openspace.list_backends()
        status_lines.append(f"Backends: {colorize(', '.join(backends), 'c')}")
        
        sessions = openspace.list_sessions()
        status_lines.append(f"Active Sessions: {colorize(str(len(sessions)), 'y')}")
    
    for line in status_lines:
        print(box.text_line(f"  {line}", indent=4, text_color=''))
    
    print(box.bottom_line(indent=4))
    print()
