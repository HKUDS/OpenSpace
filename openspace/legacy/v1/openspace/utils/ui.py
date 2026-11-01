"""
OpenSpace Terminal UI System

Provides real-time CLI visualization for OpenSpace execution flow.
Displays agent activities, grounding backends, and detailed logs.

Uses native ANSI colors and custom box drawing for a clean, lightweight interface.
"""

from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime
from enum import Enum
import asyncio
import sys
import shutil

from openspace.utils.display import Box, BoxStyle, colorize


class AgentStatus(Enum):
    """Agent execution status"""
    IDLE = "idle"
    THINKING = "thinking"
    EXECUTING = "executing"
    WAITING = "waiting"


class OpenSpaceUI:
    """
    OpenSpace Terminal UI
    
    Provides real-time visualization of:
    - Agent activities and status
    - Grounding backend operations
    - Execution logs
    - System metrics
    
    Design Philosophy:
    - Lightweight and fast (no heavy dependencies)
    - Clean ANSI-based rendering
    - Minimal CPU overhead
    - Easy to customize
    """
    
    def __init__(self, enable_live: bool = True, compact: bool = False):
        """
        Initialize UI
        
        Args:
            enable_live: Whether to enable live display updates
            compact: Use compact layout (for smaller terminals)
        """
        self.enable_live = enable_live
        self.compact = compact
        
        # Terminal dimensions
        self.term_width, self.term_height = self._get_terminal_size()
        
        # State tracking
        self.agent_status: Dict[str, AgentStatus] = {}
        self.agent_activities: Dict[str, List[str]] = {}
        self.grounding_operations: List[Dict[str, Any]] = []
        self.grounding_backends: List[Dict[str, Any]] = []  # Backend info (type, servers, etc.)
        self.log_buffer: List[Tuple[str, str, datetime]] = []  # (message, level, timestamp)
        
        # Metrics
        self.metrics: Dict[str, Any] = {
            "start_time": None,
            "iterations": 0,
            "completed_tasks": 0,
            "llm_calls": 0,
            "grounding_calls": 0,
        }
        
        # Live display state
        self._live_running = False
        self._live_task: Optional[asyncio.Task] = None
        self._last_render: List[str] = []
    
    def _get_terminal_size(self) -> Tuple[int, int]:
        """Get terminal size"""
        try:
            size = shutil.get_terminal_size((80, 24))
            return size.columns, size.lines
        except:
            return 80, 24
    
    def _clear_screen(self):
        """Clear screen"""
        if self.enable_live:
            # Clear entire screen and move cursor to top-left
            sys.stdout.write('\033[2J\033[H')
            sys.stdout.flush()
    
    def _move_cursor_home(self):
        """Move cursor to home position"""
        sys.stdout.write('\033[H')
        sys.stdout.flush()
    
    def _hide_cursor(self):
        """Hide cursor"""
        sys.stdout.write('\033[?25l')
        sys.stdout.flush()
    
    def _show_cursor(self):
        """Show cursor"""
        sys.stdout.write('\033[?25h')
        sys.stdout.flush()
    
    # Banner and Startup
    def print_banner(self):
        """Print startup banner"""
        box = Box(width=70, style=BoxStyle.ROUNDED, color='c')
        
        print()
        print(box.top_line(indent=4))
        print(box.empty_line(indent=4))
        
        # Title
        title = colorize("OpenSpace", 'c', bold=True)
        print(box.text_line(title, align='center', indent=4, text_color=''))
        
        # Subtitle
        subtitle = "Self-Evolving Skill Worker & Community"
        print(box.text_line(subtitle, align='center', indent=4, text_color='gr'))
        
        print(box.empty_line(indent=4))
        print(box.bottom_line(indent=4))
        print()
    
    def print_initialization(self, steps: List[Tuple[str, str]]):
        """
        Print initialization steps
        
        Args:
            steps: List of (component_name, status) tuples
        """
        box = Box(width=70, style=BoxStyle.ROUNDED, color='bl')
        
        print(box.text_line("Initializing Components", align='center', indent=4, text_color='c'))
        print(box.separator_line(indent=4))
        
        for component, status in steps:
            icon = colorize("✓", 'g') if status == "ok" else colorize("✗", 'rd')
            line = f"{icon} {component}"
            print(box.text_line(line, indent=4))
        
        print(box.bottom_line(indent=4))
        print()
    
    async def start_live_display(self):
        """Start live display"""
        if not self.enable_live or self._live_running:
            return
        
        self._live_running = True
        self.metrics["start_time"] = datetime.now()
        self._clear_screen()
        self._hide_cursor()
        
        # Start update loop
        self._live_task = asyncio.create_task(self._live_update_loop())
    
    async def stop_live_display(self):
        """Stop live display"""
        if not self._live_running:
            return
        
        self._live_running = False
        
        if self._live_task:
            self._live_task.cancel()
            try:
                await self._live_task
            except asyncio.CancelledError:
                pass
        
        self._show_cursor()
        print()  # Add newline after live display
    
    async def _live_update_loop(self):
        """Live update loop"""
        while self._live_running:
            try:
                self.render()
                await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"UI render error: {e}")
    
    def render(self):
        """Render entire UI"""
        if not self.enable_live or not self._live_running:
            return
        
        # Clear and redraw
        self._clear_screen()
        
        lines = []
        
        # Header
        lines.extend(self._render_header())
        lines.append("")
        
        # Stack all panels vertically
        lines.extend(self._render_agents())
        lines.append("")
        lines.extend(self._render_grounding())
        lines.append("")
        lines.extend(self._render_logs())
        
        output = "\n".join(lines)
        sys.stdout.write(output)
        sys.stdout.flush()
    
    def update_display(self):
        """Update display (alias for render())"""
        self.render()
    
    def _render_header(self) -> List[str]:
        """Render header section"""
        lines = []
        
        # Calculate elapsed time
        elapsed = "0s"
        if self.metrics["start_time"]:
            delta = datetime.now() - self.metrics["start_time"]
            minutes = delta.seconds // 60
            seconds = delta.seconds % 60
            if minutes > 0:
                elapsed = f"{minutes}m{seconds}s"
            else:
                elapsed = f"{seconds}s"
        
        status_text = (
            f"▶ {colorize('RUNNING', 'g')} | "
            f"Time: {colorize(elapsed, 'c')} | "
            f"Iter: {colorize(str(self.metrics['iterations']), 'y')} | "
            f"Tasks: {colorize(str(self.metrics['completed_tasks']), 'g')} | "
            f"LLM: {colorize(str(self.metrics['llm_calls']), 'bl')} | "
            f"Grounding: {colorize(str(self.metrics['grounding_calls']), 'm')}"
        )
        
        lines.append("  " + status_text)
        lines.append("  " + "─" * 60)
        
        return lines
    
    def _render_agents(self) -> List[str]:
        """Render agents section"""
        lines = []
        
        lines.append("  " + colorize("§ Agents", 'c', bold=True))
        
        # Agent info
        agents = [
            ("GroundingAgent", 'c', self.agent_status.get("GroundingAgent", AgentStatus.IDLE)),
        ]
        
        for agent_name, color, status in agents:
            # Status icon
            status_icons = {
                AgentStatus.IDLE: "○",
                AgentStatus.THINKING: "◐",
                AgentStatus.EXECUTING: "◉",
                AgentStatus.WAITING: "◷",
            }
            icon = status_icons.get(status, "○")
            
            # Recent activity
            activities = self.agent_activities.get(agent_name, [])
            activity = activities[-1][:40] if activities else "idle"
            
            # Format line
            line = f"    {colorize(icon, 'y')} {colorize(agent_name, color):<20s} {activity}"
            lines.append(line)
        
        return lines
    
    
    def _render_grounding(self) -> List[str]:
        """Render grounding operations section"""
        lines = []
        
        lines.append("  " + colorize("⊕ Grounding Backends", 'c', bold=True))
        
        # Show backend types and servers
        if self.grounding_backends:
            for backend_info in self.grounding_backends:
                backend_name = backend_info.get("name", "unknown")
                backend_type = backend_info.get("type", "unknown")
                servers = backend_info.get("servers", [])
                
                # Backend type icon
                type_icons = {
                    "gui": "■",      
                    "shell": "$",   
                    "mcp": "◆",     
                    "system": "●",   
                    "web": "◉",     
                }
                icon = type_icons.get(backend_type, "○")
                
                # Format backend line
                if backend_type == "mcp" and servers:
                    servers_str = ", ".join([s[:15] for s in servers])
                    line = f"    {icon} {colorize(backend_name, 'y')} ({backend_type}): {colorize(servers_str, 'gr')}"
                else:
                    line = f"    {icon} {colorize(backend_name, 'y')} ({backend_type})"
                
                lines.append(line)
        
        # Show last 3 operations
        recent_ops = self.grounding_operations[-3:] if self.grounding_operations else []
        
        if recent_ops:
            lines.append("    " + colorize("Recent Operations:", 'gr'))
            for op in recent_ops:
                backend = op.get("backend", "unknown")
                action = op.get("action", "unknown")[:40]
                status = op.get("status", "pending")
                
                # Status icon
                if status == "success":
                    icon = colorize("✓", 'g')
                elif status == "pending":
                    icon = colorize("⏳", 'y')
                else:
                    icon = colorize("✗", 'rd')
                
                line = f"      {icon} {colorize(backend, 'bl')}: {action}"
                lines.append(line)
        
        return lines
    
    def _render_logs(self) -> List[str]:
        """Render logs section"""
        lines = []
        
        lines.append("  " + colorize("⊞ Recent Events", 'c', bold=True))
        
        # Show last 5 logs
        recent_logs = self.log_buffer[-5:] if self.log_buffer else []
        
        if recent_logs:
            for message, level, timestamp in recent_logs:
                time_str = timestamp.strftime("%H:%M:%S")
                
                # Truncate long messages
                msg_display = message[:55]
                
                log_line = f"    {colorize(time_str, 'gr')} | {msg_display}"
                lines.append(log_line)
        
        return lines
    
    
    def update_agent_status(self, agent_name: str, status: AgentStatus):
        """Update agent status"""
        self.agent_status[agent_name] = status
    
    def add_agent_activity(self, agent_name: str, activity: str):
        """Add agent activity"""
        if agent_name not in self.agent_activities:
            self.agent_activities[agent_name] = []
        
        self.agent_activities[agent_name].append(activity)
        
        # Keep only last 10 activities
        if len(self.agent_activities[agent_name]) > 10:
            self.agent_activities[agent_name] = self.agent_activities[agent_name][-10:]
    
    def update_grounding_backends(self, backends: List[Dict[str, Any]]):
        """
        Update grounding backends information
        
        Args:
            backends: List of backend info dicts with keys:
                - name: backend name
                - type: backend type (gui, shell, mcp, system, web)
                - servers: list of server names (for mcp)
        """
        self.grounding_backends = backends
    
    def add_grounding_operation(self, backend: str, action: str, status: str = "pending"):
        """Add grounding operation"""
        self.grounding_operations.append({
            "backend": backend,
            "action": action,
            "status": status,
            "timestamp": datetime.now(),
        })
        
        self.metrics["grounding_calls"] += 1
    
    def add_log(self, message: str, level: str = "info"):
        """Add log message"""
        self.log_buffer.append((message, level, datetime.now()))
        
        # Keep only last 100 logs
        if len(self.log_buffer) > 100:
            self.log_buffer = self.log_buffer[-100:]
    
    def update_metrics(self, **kwargs):
        """Update metrics"""
        self.metrics.update(kwargs)
    
    def print_summary(self, result: Dict[str, Any]):
        """Print execution summary"""
        box = Box(width=70, style=BoxStyle.ROUNDED, color='c')
        
        print()
        print(box.text_line(colorize("◈ Execution Summary", 'c', bold=True), align='center', indent=4, text_color=''))
        print(box.separator_line(indent=4))
        
        # Status
        status = result.get("status", "unknown")
        status_display = {
            "completed": colorize("COMPLETED", 'g', bold=True),
            "timeout": colorize("TIMEOUT", 'y', bold=True),
            "error": colorize("ERROR", 'rd', bold=True),
        }
        status_text = status_display.get(status, status)
        
        print(box.text_line(f"  Status:          {status_text}", indent=4, text_color=''))
        print(box.text_line(f"  Execution Time:  {colorize(f'{result.get('execution_time', 0):.2f}s', 'c')}", indent=4, text_color=''))
        print(box.text_line(f"  Iterations:      {colorize(str(result.get('iterations', 0)), 'y')}", indent=4, text_color=''))
        print(box.text_line(f"  Completed Tasks: {colorize(str(result.get('completed_tasks', 0)), 'g')}", indent=4, text_color=''))
        
        if result.get('evaluation_results'):
            print(box.text_line(f"  Evaluations:     {colorize(str(len(result['evaluation_results'])), 'bl')}", indent=4, text_color=''))
        
        print(box.bottom_line(indent=4))
        print()


def create_ui(enable_live: bool = True, compact: bool = False) -> OpenSpaceUI:
    """
    Create OpenSpace UI instance
    
    Args:
        enable_live: Whether to enable live display updates
        compact: Use compact layout for smaller terminals
    """
    return OpenSpaceUI(enable_live=enable_live, compact=compact)