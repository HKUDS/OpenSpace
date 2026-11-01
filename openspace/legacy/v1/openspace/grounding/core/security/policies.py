import asyncio
import sys
from typing import Callable, Awaitable, Dict, Optional
from ..types import SecurityPolicy, BackendType

PromptFunc = Callable[[str], Awaitable[bool]] 


# ANSI color codes
class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    GREEN = "\033[92m"
    CYAN = "\033[96m"
    GRAY = "\033[90m"
    WHITE = "\033[97m"


class SecurityPolicyManager:
    def __init__(self, prompt: PromptFunc | None = None):
        self._policies: Dict[BackendType, SecurityPolicy] = {}
        self._global_policy: Optional[SecurityPolicy] = None
        self._prompt: PromptFunc | None = prompt or self._default_cli_prompt
    
    async def _default_cli_prompt(self, message: str) -> bool:
        # Clean and professional prompt using unified display
        from openspace.utils.display import Box, BoxStyle, colorize, print_separator
        
        print()
        print_separator(70, 'y', 2)
        print(f"  {colorize('⚠️  Security Policy Warning', color=Colors.RED, bold=True)}")
        print_separator(70, 'y', 2)
        print(f"  {message}")
        print_separator(70, 'gr', 2)
        print(f"  {colorize('[y/yes]', color=Colors.GREEN)} Allow  |  {colorize('[n/no]', color=Colors.RED)} Deny")
        print_separator(70, 'gr', 2)
        print(f"  {colorize('Your choice:', bold=True)} ", end="", flush=True)
        
        answer = await asyncio.get_running_loop().run_in_executor(None, sys.stdin.readline)
        response = answer.strip().lower() in {"y", "yes"}
        
        if response:
            print(f"  {colorize('✓ Allowed', color=Colors.GREEN)}\n")
        else:
            print(f"  {colorize('✗ Denied', color=Colors.RED)}\n")
        
        return response
    
    def set_global_policy(self, policy: SecurityPolicy) -> None:
        self._global_policy = policy
    
    def set_backend_policy(self, backend_type: BackendType, policy: SecurityPolicy) -> None:
        self._policies[backend_type] = policy
    
    def get_policy(self, backend_type: BackendType) -> SecurityPolicy:
        policy = self._policies.get(backend_type) 
        if policy:
            return policy
        
        if self._global_policy:
            return self._global_policy
        
        return SecurityPolicy()
    
    async def _ask_user(self, message: str) -> bool:
        """If prompt is provided, ask user for confirmation, otherwise default to deny"""
        if self._prompt:
            try:
                return await self._prompt(message)
            except Exception:
                return False
        return False

    async def check_command_allowed(self, backend_type: BackendType, command: str) -> bool:
        policy = self.get_policy(backend_type)

        if policy.check(command=command):
            return True

        # Find dangerous tokens
        dangerous_tokens = policy.find_dangerous_tokens(command)
        
        # Extract only lines containing dangerous commands
        lines = command.split('\n')
        dangerous_lines = []
        for i, line in enumerate(lines):
            line_lower = line.lower()
            if any(token in line_lower for token in dangerous_tokens):
                # Add line number and the line itself
                dangerous_lines.append((i + 1, line.strip()))
        
        # If no specific dangerous lines found but policy failed, show first few lines
        if not dangerous_lines:
            dangerous_lines = [(i + 1, line.strip()) for i, line in enumerate(lines[:5])]
        
        # Format dangerous lines for display (limit to 10 lines)
        max_display_lines = 10
        if len(dangerous_lines) > max_display_lines:
            display_lines = dangerous_lines[:max_display_lines]
            truncated = True
        else:
            display_lines = dangerous_lines
            truncated = False
        
        # Build formatted command display
        formatted_cmd_lines = []
        for line_num, line in display_lines:
            # Truncate very long lines
            if len(line) > 80:
                line = line[:77] + "..."
            formatted_cmd_lines.append(f"  L{line_num}: {line}")
        
        if truncated:
            formatted_cmd_lines.append("  ... (more lines)")
        
        formatted_command = '\n'.join(formatted_cmd_lines)
        
        # Show which dangerous commands were detected
        dangerous_list = ', '.join([f"{Colors.RED}{tok}{Colors.RESET}" for tok in dangerous_tokens[:5]])
        
        from openspace.utils.display import Box, BoxStyle, colorize
        
        # Build command box
        box = Box(width=66, style=BoxStyle.SQUARE, color='gr')
        cmd_box = [
            box.top_line(2),
            box.empty_line(2),
        ]
        for line in formatted_cmd_lines:
            cmd_box.append(box.text_line(line, indent=2))
        cmd_box.extend([
            box.empty_line(2),
            box.bottom_line(2)
        ])
        
        message = (
            f"\n{colorize('Potentially dangerous command detected', color=Colors.WHITE)}\n\n"
            f"Backend:  {colorize(backend_type.value, color=Colors.CYAN)}\n"
            f"Dangerous commands: {dangerous_list}\n\n"
            f"Affected lines:\n"
            + "\n".join(cmd_box) + "\n\n"
            f"{colorize('This command may contain risky operations. Continue?', color=Colors.YELLOW)}"
        )

        return await self._ask_user(message)
    
    async def check_domain_allowed(self, backend_type: BackendType, domain: str) -> bool:
        policy = self.get_policy(backend_type)

        if policy.check(domain=domain):
            return True

        message = (
            f"\n{Colors.WHITE}Unauthorized domain access detected{Colors.RESET}\n\n"
            f"Backend: {Colors.CYAN}{backend_type.value}{Colors.RESET}\n"
            f"Domain:  {Colors.YELLOW}{domain}{Colors.RESET}\n\n"
            f"{Colors.YELLOW}This domain is not in the allowed list. Continue?{Colors.RESET}"
        )

        return await self._ask_user(message)