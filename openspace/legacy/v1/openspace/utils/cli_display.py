"""CLI Display utilities for OpenSpace startup and interaction"""

from openspace.tool_layer import OpenSpaceConfig
from openspace.utils.display import Box, BoxStyle, colorize


class CLIDisplay:   
    @staticmethod
    def print_banner():
        box = Box(width=70, style=BoxStyle.ROUNDED, color='c')
        
        print()
        print(box.top_line(indent=4))
        print(box.empty_line(indent=4))
        
        title = colorize("OpenSpace", 'c', bold=True)
        print(box.text_line(title, align='center', indent=4, text_color=''))
        
        subtitle = "Self-Evolving Skill Worker & Community"
        print(box.text_line(subtitle, align='center', indent=4, text_color='gr'))
        
        print(box.empty_line(indent=4))
        print(box.bottom_line(indent=4))
        print()
    
    @staticmethod
    def print_configuration(config: OpenSpaceConfig):
        box = Box(width=70, style=BoxStyle.ROUNDED, color='bl')
        
        print(box.text_line(colorize("◉ System Configuration", 'c', bold=True), align='center', indent=4, text_color=''))
        print(box.separator_line(indent=4))
        
        configs = [
            ("AI Model", config.llm_model, 'bl'),
            ("Max Iterations", str(config.grounding_max_iterations), 'c'),
            ("LLM Timeout", f"{config.llm_timeout}s", 'c'),
        ]
        
        for label, value, color in configs:
            line = f"  {label:20s} {colorize(value, color)}"
            print(box.text_line(line, indent=4, text_color=''))
        
        print(box.bottom_line(indent=4))
        print()
    
    @staticmethod
    def print_initialization_progress(steps: list, show_header: bool = True):
        box = Box(width=70, style=BoxStyle.ROUNDED, color='g')
        
        if show_header:
            print(box.text_line(colorize("► Initializing Components", 'g', bold=True), 
                              align='center', indent=4, text_color=''))
            print(box.separator_line(indent=4))
        
        for step, status in steps:
            if status == "ok":
                icon = colorize("✓", 'g')
            elif status == "error":
                icon = colorize("✗", 'rd')
            else:
                icon = colorize("[...]", 'y')
            
            line = f"  {icon}  {step}"
            print(box.text_line(line, indent=4, text_color=''))
        
        print(box.bottom_line(indent=4))
        print()
    
    @staticmethod
    def print_result_summary(result: dict):
        box = Box(width=70, style=BoxStyle.ROUNDED, color='c')
        
        print()
        print(box.text_line(colorize("◈ Execution Summary", 'c', bold=True), 
                          align='center', indent=4, text_color=''))
        print(box.separator_line(indent=4))
        
        status = result.get("status", "unknown")
        status_colors = {
            "completed": 'g',
            "timeout": 'y',
            "error": 'rd',
            "max_iterations_reached": 'y',
        }
        status_color = status_colors.get(status, 'gr')
        status_display = colorize(status.upper(), status_color, bold=True)
        
        exec_time = result.get('execution_time', 0)
        result_lines = [
            f"  Status:          {status_display}",
            f"  Execution Time:  {colorize(f'{exec_time:.2f}s', 'c')}",
            f"  Iterations:      {colorize(str(result.get('iterations', 0)), 'y')}",
            f"  Completed Tasks: {colorize(str(result.get('completed_tasks', 0)), 'g')}",
        ]
        
        if result.get('evaluation_results'):
            result_lines.append(f"  Evaluations:     {colorize(str(len(result['evaluation_results'])), 'bl')}")
        
        for line in result_lines:
            print(box.text_line(line, indent=4, text_color=''))
        
        print(box.bottom_line(indent=4))
        print()
        
        # Print user response (the actual answer/result)
        if result.get('user_response'):
            response_box = Box(width=70, style=BoxStyle.ROUNDED, color='g')
            print(response_box.text_line(colorize("◈ Result", 'g', bold=True), 
                                       align='center', indent=4, text_color=''))
            print(response_box.separator_line(indent=4))
            
            user_response = result['user_response']
            for line in user_response.split('\n'):
                if line.strip():
                    display_line = f"  {line.strip()}"
                    print(response_box.text_line(display_line, indent=4, text_color=''))
            
            print(response_box.bottom_line(indent=4))
            print()
    
    @staticmethod
    def print_interactive_header():
        box = Box(width=70, style=BoxStyle.ROUNDED, color='c')
        
        print(box.text_line(colorize("⌨ Interactive Mode", 'c', bold=True), 
                          align='center', indent=4, text_color=''))
        print(box.separator_line(indent=4))
        
        help_lines = [
            "",
            colorize("  Ready to execute your tasks!", 'g'),
            "",
            colorize("  Available Commands:", 'c', bold=True),
            "    " + colorize("status", 'bl') + "  →  View system status",
            "    " + colorize("help", 'bl') + "    →  Show available commands",
            "    " + colorize("quit", 'bl') + "    →  Exit interactive mode",
            "",
            colorize("  ▸ Enter your task description below:", 'gr'),
            "",
        ]
        
        for line in help_lines:
            print(box.text_line(line, indent=4, text_color=''))
        
        print(box.bottom_line(indent=4))
        print()
    
    @staticmethod
    def print_task_header(query: str, title: str = "▶ Executing Task"):
        box = Box(width=70, style=BoxStyle.ROUNDED, color='g')
        print()
        print(box.text_line(colorize(title, 'g', bold=True), align='center', indent=4, text_color=''))
        print(box.separator_line(indent=4))
        print(box.text_line("", indent=4, text_color=''))
        print(box.text_line(f"  {query}", indent=4, text_color=''))
        print(box.text_line("", indent=4, text_color=''))
        print(box.bottom_line(indent=4))
    
    @staticmethod
    def print_system_ready():
        box = Box(width=70, style=BoxStyle.ROUNDED, color='g')
        print(box.text_line(colorize("◈ System Ready", 'g', bold=True), 
                          align='center', indent=4, text_color=''))
        print(box.separator_line(indent=4))
        print(box.text_line("", indent=4, text_color=''))
        print(box.text_line(colorize("  Real-time UI will display:", 'c'), indent=4, text_color=''))
        print(box.text_line("    § Agent activities and status", indent=4, text_color=''))
        print(box.text_line("    ⊕ Grounding backend operations", indent=4, text_color=''))
        print(box.text_line("    ⊞ Execution logs", indent=4, text_color=''))
        print(box.text_line("", indent=4, text_color=''))
        print(box.bottom_line(indent=4))
        print()
    
    @staticmethod
    def print_status(agent):
        box = Box(width=70, style=BoxStyle.ROUNDED, color='bl')
        print()
        print(box.text_line(colorize("System Status", 'bl', bold=True), 
                          align='center', indent=4, text_color=''))
        print(box.separator_line(indent=4))
        
        status = agent.get_status()
        status_lines = [
            f"Initialized: {colorize('Yes' if status['initialized'] else 'No', 'g' if status['initialized'] else 'rd')}",
            f"Running: {colorize('Yes' if status['running'] else 'No', 'y' if status['running'] else 'g')}",
        ]
        
        if "agents" in status:
            status_lines.append(f"Agents: {colorize(', '.join(status['agents']), 'c')}")
        
        for line in status_lines:
            print(box.text_line(line, indent=4, text_color=''))
        
        print(box.bottom_line(indent=4))
        print()
    
    @staticmethod
    def print_help():
        box = Box(width=70, style=BoxStyle.ROUNDED, color='y')
        print()
        print(box.text_line(colorize("Available Commands", 'y', bold=True), 
                          align='center', indent=4, text_color=''))
        print(box.separator_line(indent=4))
        
        help_items = [
            (colorize("status", 'c'), "Show system status"),
            (colorize("help", 'c'), "Show this help message"),
            (colorize("quit/exit", 'c'), "Exit interactive mode"),
            ("", ""),
            (colorize("Other input", 'gr'), "Execute as task"),
        ]
        
        for cmd, desc in help_items:
            if cmd:
                print(box.text_line(f"  {cmd:20s} {desc}", indent=4, text_color=''))
            else:
                print(box.separator_line(indent=4))
        
        print(box.bottom_line(indent=4))
        print()