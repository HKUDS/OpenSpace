import asyncio
import sys
import shutil
from typing import Callable, Awaitable, Optional, Dict, List
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

PromptFunc = Callable[[str], Awaitable[bool]]

# Global lock to prevent concurrent user prompts
_prompt_lock = asyncio.Lock()


class MCPDependencyError(RuntimeError):
    """Base exception for MCP dependency errors."""
    pass


class MCPCommandNotFoundError(MCPDependencyError):
    """Raised when a required command is not available."""
    pass


class MCPInstallationCancelledError(MCPDependencyError):
    """Raised when user cancels installation."""
    pass


class MCPInstallationFailedError(MCPDependencyError):
    """Raised when installation fails."""
    pass


class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    GREEN = "\033[92m"
    CYAN = "\033[96m"
    GRAY = "\033[90m"
    WHITE = "\033[97m"
    BLUE = "\033[94m"


class MCPInstallerManager:
    """
    MCP dependencies package installer manager.
    
    Responsible for detecting if the MCP server dependencies are installed, and if not, asking the user whether to install them.
    """
    
    def __init__(self, prompt: PromptFunc | None = None, auto_install: bool = False, verbose: bool = False):
        """Initialize the installer manager.
        
        Args:
            prompt: Custom user prompt function, if None, the default CLI prompt is used
            auto_install: If True, automatically install dependencies without asking the user
            verbose: If True, show detailed installation logs; if False, only show progress indicator
        """
        self._prompt: PromptFunc | None = prompt or self._default_cli_prompt
        self._auto_install = auto_install
        self._verbose = verbose
        self._installed_cache: Dict[str, bool] = {}  # Cache for checked packages
        self._failed_installations: Dict[str, str] = {}  # Track failed installations to avoid retry
        
    async def _default_cli_prompt(self, message: str) -> bool:
        """Default CLI prompt function (called within lock by ensure_dependencies)."""
        from openspace.utils.display import print_separator, colorize
        
        print()
        print_separator(70, 'c', 2)
        print(f"  {colorize('MCP dependencies installation prompt', color=Colors.BLUE, bold=True)}")
        print_separator(70, 'c', 2)
        print(f"  {message}")
        print_separator(70, 'gr', 2)
        print(f"  {colorize('[y/yes]', color=Colors.GREEN)} Install  |  {colorize('[n/no]', color=Colors.RED)} Cancel")
        print_separator(70, 'gr', 2)
        print(f"  {colorize('Your choice:', bold=True)} ", end="", flush=True)
        
        answer = await asyncio.get_running_loop().run_in_executor(None, sys.stdin.readline)
        response = answer.strip().lower() in {"y", "yes"}
        
        if response:
            print(f"{Colors.GREEN}✓ Installation confirmed{Colors.RESET}\n")
        else:
            print(f"{Colors.RED}✗ Installation cancelled{Colors.RESET}\n")
        
        return response
    
    async def _ask_user(self, message: str) -> bool:
        """Ask the user whether to install."""
        if self._auto_install:
            logger.info("Automatic installation mode enabled, will automatically install dependencies")
            return True
            
        if self._prompt:
            try:
                return await self._prompt(message)
            except Exception as e:
                logger.error(f"Error asking user: {e}")
                return False
        return False
    
    def _check_command_available(self, command: str) -> bool:
        """Check if the command is available.
        
        Args:
            command: The command to check (e.g. "npx", "uvx")
            
        Returns:
            bool: Whether the command is available
        """
        return shutil.which(command) is not None
    
    async def _check_package_installed(self, command: str, args: List[str]) -> bool:
        """Check if the package is installed.
        
        Args:
            command: The command to check (e.g. "npx", "uvx")
            args: The arguments list
            
        Returns:
            bool: Whether the package is installed
        """
        # Build cache key
        cache_key = f"{command}:{':'.join(args)}"
        
        # Check cache
        if cache_key in self._installed_cache:
            return self._installed_cache[cache_key]
        
        # For different types of commands, use different check methods
        try:
            if command == "npx":
                # For npx, check if the npm package exists
                package_name = self._extract_npm_package(args)
                if package_name:
                    result = await self._check_npm_package(package_name)
                    self._installed_cache[cache_key] = result
                    return result
            elif command == "uvx":
                # For uvx, check if the Python package exists
                package_name = self._extract_python_package(args)
                if package_name:
                    result = await self._check_python_package(package_name)
                    self._installed_cache[cache_key] = result
                    return result
            elif command == "uv":
                # For "uv run --with package ...", check if the Python package exists
                package_name = self._extract_uv_package(args)
                if package_name:
                    result = await self._check_uv_pip_package(package_name)
                    self._installed_cache[cache_key] = result
                    return result
        except Exception as e:
            logger.debug(f"Error checking package installation status: {e}")
        
        # Default to assuming not installed
        return False
    
    def _extract_npm_package(self, args: List[str]) -> Optional[str]:
        """Extract package name from npx arguments.
        
        Args:
            args: npx arguments list, e.g. ["-y", "mcp-excalidraw-server"] or ["bazi-mcp"]
            
        Returns:
            Package name (without version tag) or None
        """
        for i, arg in enumerate(args):
            # Skip option parameters
            if arg.startswith("-"):
                continue
            
            # Found package name, now strip version tag
            package_name = arg
            
            # Handle scoped packages: @scope/package@version -> @scope/package
            if package_name.startswith("@"):
                # Scoped package like @rtuin/mcp-mermaid-validator@latest
                parts = package_name.split("/", 1)
                if len(parts) == 2:
                    scope = parts[0]
                    name_with_version = parts[1]
                    # Remove version tag from name part (e.g., "pkg@latest" -> "pkg")
                    name = name_with_version.split("@")[0] if "@" in name_with_version else name_with_version
                    return f"{scope}/{name}"
                return package_name
            else:
                # Regular package like mcp-deepwiki@latest -> mcp-deepwiki
                return package_name.split("@")[0] if "@" in package_name else package_name
        
        return None
    
    def _extract_python_package(self, args: List[str]) -> Optional[str]:
        """Extract package name from uvx arguments.
        
        Args:
            args: uvx arguments list, e.g. ["--from", "office-powerpoint-mcp-server", "ppt_mcp_server"]
                  or ["--with", "mcp==1.9.0", "sitemap-mcp-server"]
                  or ["arxiv-mcp-server", "--storage-path", "./path"]
            
        Returns:
            Package name or None
        """
        # Find --from parameter (this is the package to install)
        for i, arg in enumerate(args):
            if arg == "--from" and i + 1 < len(args):
                return args[i + 1]
        
        # Skip option flags and their values, find the main package (FIRST positional arg)
        # Options that take a value: --with, --python, --from, --storage-path, etc.
        options_with_value = {"--with", "--from", "--python", "-p", "--storage-path"}
        skip_next = False
        
        for arg in args:
            if skip_next:
                skip_next = False
                continue
            if arg in options_with_value:
                skip_next = True
                continue
            if arg.startswith("-"):
                # Other flags without values (or unknown options with values)
                # Also skip the next arg if it looks like an option value (doesn't start with -)
                continue
            # First non-option argument is the package name
            return arg
        
        return None
    
    def _extract_uv_package(self, args: List[str]) -> Optional[str]:
        """Extract package name from uv run arguments.
        
        Args:
            args: uv arguments list, e.g. ["run", "--with", "biomcp-python", "biomcp", "run"]
            
        Returns:
            Package name or None
        """
        # Find --with parameter (this specifies the package to install)
        for i, arg in enumerate(args):
            if arg == "--with" and i + 1 < len(args):
                package_name = args[i + 1]
                # Remove version specifier if present (e.g., "mcp==1.9.0" -> "mcp")
                if "==" in package_name:
                    return package_name.split("==")[0]
                if ">=" in package_name:
                    return package_name.split(">=")[0]
                return package_name
        
        return None
    
    async def _check_npm_package(self, package_name: str) -> bool:
        """Check if the npm package is globally installed.
        
        Args:
            package_name: npm package name
            
        Returns:
            bool: Whether the npm package is installed
        """
        try:
            process = await asyncio.create_subprocess_exec(
                "npm", "list", "-g", package_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            # npm list returns 0 if the package is installed
            return process.returncode == 0
        except Exception as e:
            logger.debug(f"Error checking npm package {package_name}: {e}")
            return False
    
    async def _check_python_package(self, package_name: str) -> bool:
        """Check if the Python package is installed as a uvx tool.
        
        uvx tools are installed in ~/.local/share/uv/tools/ directory,
        not in the current pip environment.
        
        Args:
            package_name: Python package/tool name
            
        Returns:
            bool: Whether the uvx tool is installed
        """
        import os
        from pathlib import Path
        
        # Strip version specifier if present (e.g., "mcp==1.9.0" -> "mcp")
        clean_name = package_name.split("==")[0].split(">=")[0].split("<=")[0].split(">")[0].split("<")[0]
        
        # Check if uvx tool exists in the standard uv tools directory
        uv_tools_dir = Path.home() / ".local" / "share" / "uv" / "tools"
        tool_dir = uv_tools_dir / clean_name
        
        if tool_dir.exists():
            logger.debug(f"uvx tool '{clean_name}' found at {tool_dir}")
            return True
        
        # Fallback: try running uvx with --help to check if it's available
        try:
            process = await asyncio.create_subprocess_exec(
                "uvx", clean_name, "--help",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            # Just wait briefly, don't need the full output
            try:
                await asyncio.wait_for(process.communicate(), timeout=5.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
            
            # If it didn't error immediately, the tool likely exists
            return process.returncode == 0
        except Exception as e:
            logger.debug(f"Error checking uvx tool {clean_name}: {e}")
        
        return False
    
    async def _check_uv_pip_package(self, package_name: str) -> bool:
        """Check if a Python package is installed via uv pip.
        
        Args:
            package_name: Python package name
            
        Returns:
            bool: Whether the package is installed
        """
        # Strip version specifier if present
        clean_name = package_name.split("==")[0].split(">=")[0].split("<=")[0].split(">")[0].split("<")[0]
        
        try:
            # Try using uv pip show to check if package is installed
            process = await asyncio.create_subprocess_exec(
                "uv", "pip", "show", clean_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            if process.returncode == 0:
                logger.debug(f"uv pip package '{clean_name}' found")
                return True
        except Exception as e:
            logger.debug(f"Error checking uv pip package {clean_name}: {e}")
        
        # Fallback: check with regular pip
        try:
            process = await asyncio.create_subprocess_exec(
                "pip", "show", clean_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            return process.returncode == 0
        except Exception as e:
            logger.debug(f"Error checking pip package {clean_name}: {e}")
        
        return False
    
    async def _install_package(self, command: str, args: List[str], use_sudo: bool = False) -> bool:
        """Execute the install command.
        
        Args:
            command: The command to execute (e.g. "npx", "uvx")
            args: The arguments list
            use_sudo: Whether to use sudo for installation
            
        Returns:
            bool: Whether the installation is successful
        """
        install_command = self._get_install_command(command, args)
        
        if not install_command:
            logger.error("Cannot determine install command")
            return False
        
        # Add sudo if requested
        if use_sudo:
            install_command = ["sudo"] + install_command
        
        logger.info(f"Executing install command: {' '.join(install_command)}")
        
        try:
            # For sudo commands, always show verbose output so password prompt is visible
            if self._verbose or use_sudo:
                # Verbose mode: show all installation logs
                from openspace.utils.display import print_separator, colorize
                
                print_separator(70, 'c', 2)
                if use_sudo:
                    print(f"  {colorize('Installing with administrator privileges...', color=Colors.BLUE)}")
                    print(f"  {colorize('>> You will be prompted for your password below <<', color=Colors.YELLOW)}")
                else:
                    print(f"  {colorize('Installing dependencies...', color=Colors.BLUE)}")
                print(f"  {colorize('Command: ' + ' '.join(install_command), color=Colors.GRAY)}")
                print_separator(70, 'c', 2)
                print()
                
                # For sudo, don't redirect stdin so password prompt works
                if use_sudo:
                    process = await asyncio.create_subprocess_exec(
                        *install_command,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                        stdin=None  # Let sudo use terminal for password
                    )
                else:
                    process = await asyncio.create_subprocess_exec(
                        *install_command,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT
                    )
                
                # Real-time output of installation logs
                output_lines = []
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        break
                    line_str = line.decode().rstrip()
                    output_lines.append(line_str)
                    print(f"{Colors.GRAY}{line_str}{Colors.RESET}")
                
                await process.wait()
                full_output = '\n'.join(output_lines)
            else:
                # Quiet mode: only show progress indicator
                print(f"\n{Colors.BLUE}Installing dependencies...{Colors.RESET} ", end="", flush=True)
                
                process = await asyncio.create_subprocess_exec(
                    *install_command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                
                # Show spinner animation while installing
                spinner = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
                spinner_idx = 0
                
                while True:
                    try:
                        await asyncio.wait_for(process.wait(), timeout=0.1)
                        break
                    except asyncio.TimeoutError:
                        print(f"\r{Colors.BLUE}Installing dependencies...{Colors.RESET} {Colors.CYAN}{spinner[spinner_idx]}{Colors.RESET}", end="", flush=True)
                        spinner_idx = (spinner_idx + 1) % len(spinner)
                
                # Clear the spinner line
                print(f"\r{' ' * 100}\r", end="", flush=True)
                
                # Collect output
                stdout, stderr = await process.communicate()
                full_output = (stdout or stderr).decode() if (stdout or stderr) else ""
            
            if process.returncode == 0:
                print(f"{Colors.GREEN}✓ Dependencies installed successfully{Colors.RESET}")
                if not use_sudo:
                    print(f"{Colors.GRAY}(Note: First connection may take a moment to initialize){Colors.RESET}")
                # Update cache
                cache_key = f"{command}:{':'.join(args)}"
                self._installed_cache[cache_key] = True
                return True
            else:
                # Check if it's a permission error
                is_permission_error = "EACCES" in full_output or "permission denied" in full_output.lower()
                
                if is_permission_error and not use_sudo:
                    print(f"\n{Colors.YELLOW}Permission denied{Colors.RESET}")
                    print(f"{Colors.GRAY}The installation requires administrator privileges.{Colors.RESET}\n")
                    
                    # Ask user if they want to use sudo
                    message = (
                        f"\n{Colors.WHITE}Administrator privileges required{Colors.RESET}\n\n"
                        f"Command: {Colors.GRAY}{' '.join(install_command)}{Colors.RESET}\n\n"
                        f"{Colors.YELLOW}Do you want to retry with sudo (requires password)?{Colors.RESET}"
                    )
                    
                    if await self._ask_user(message):
                        # No extra print needed, the verbose mode will show clear instructions
                        return await self._install_package(command, args, use_sudo=True)
                    else:
                        print(f"\n{Colors.RED}✗ Installation cancelled{Colors.RESET}")
                        return False
                else:
                    print(f"{Colors.RED}✗ Dependencies installation failed (return code: {process.returncode}){Colors.RESET}")
                    # Show error output if not already shown
                    if not self._verbose and full_output:
                        # Limit error output to last 20 lines
                        error_lines = full_output.split('\n')
                        if len(error_lines) > 20:
                            error_lines = ['...(truncated)...'] + error_lines[-20:]
                        print(f"{Colors.GRAY}Error output:\n{chr(10).join(error_lines)}{Colors.RESET}")
                    
                    # Add general guidance for manual installation
                    print(f"\n{Colors.YELLOW}Tip:{Colors.RESET} {Colors.GRAY}If automatic installation fails, please refer to the")
                    print(f"official documentation of the MCP server for manual installation instructions.{Colors.RESET}\n")
                    
                    return False
                
        except Exception as e:
            logger.error(f"Error installing dependencies: {e}")
            print(f"{Colors.RED}✗ Error occurred during installation: {e}{Colors.RESET}")
            return False
    
    def _get_install_command(self, command: str, args: List[str]) -> Optional[List[str]]:
        """Generate install command based on command type.
        
        Args:
            command: The command to execute (e.g. "npx", "uvx", "uv")
            args: The original arguments list
            
        Returns:
            Install command list or None
        """
        if command == "npx":
            package_name = self._extract_npm_package(args)
            if package_name:
                return ["npm", "install", "-g", package_name]
        elif command == "uvx":
            package_name = self._extract_python_package(args)
            if package_name:
                return ["pip", "install", package_name]
        elif command == "uv":
            # Handle "uv run --with package_name ..." format
            package_name = self._extract_uv_package(args)
            if package_name:
                return ["uv", "pip", "install", package_name]
        
        return None
    
    async def ensure_dependencies(
        self, 
        server_name: str,
        command: str, 
        args: List[str]
    ) -> bool:
        """Ensure the dependencies of the MCP server are installed.
        
        This method checks if the dependencies are installed, and if not, asks the user whether to install them.
        
        Args:
            server_name: MCP server name (for display purposes)
            command: The command to execute (e.g. "npx", "uvx")
            args: The arguments list
            
        Returns:
            bool: Whether the dependencies are installed (installed or successfully installed)
            
        Raises:
            RuntimeError: When the command is not available or the user refuses to install
        """
        # Use lock to ensure entire installation process is atomic
        async with _prompt_lock:
            return await self._ensure_dependencies_impl(server_name, command, args)
    
    async def _ensure_dependencies_impl(
        self, 
        server_name: str,
        command: str, 
        args: List[str]
    ) -> bool:
        """Internal implementation of ensure_dependencies (called within lock)."""
        # Skip dependency checking for direct script execution commands
        # These commands run scripts directly and don't need package installation
        SKIP_COMMANDS = {"node", "python", "python3", "bash", "sh", "deno", "bun"}
        
        if command.lower() in SKIP_COMMANDS:
            logger.debug(f"Skipping dependency check for direct script execution command: {command}")
            return True
        
        # Skip dependency checking for GitHub-based npx packages
        # These packages are handled directly by npx which downloads, builds, and runs them
        # npm install -g doesn't work properly for GitHub packages that require building
        if command == "npx":
            package_name = self._extract_npm_package(args)
            if package_name and package_name.startswith("github:"):
                logger.debug(f"Skipping dependency check for GitHub-based npx package: {package_name}")
                return True
        
        # Check if this server has already failed installation
        cache_key = f"{server_name}:{command}:{':'.join(args)}"
        if cache_key in self._failed_installations:
            error_msg = self._failed_installations[cache_key]
            logger.debug(f"Skipping installation for '{server_name}' - previously failed")
            raise MCPDependencyError(error_msg)
        
        # Special handling for uvx - check if uv is installed
        if command == "uvx":
            if not self._check_command_available("uv"):
                # Only show once to user, no verbose logging
                print(f"\n{Colors.RED}✗ Server '{server_name}' requires 'uv' to be installed{Colors.RESET}")
                print(f"{Colors.YELLOW}Please install uv first:")
                print(f"  • macOS/Linux: curl -LsSf https://astral.sh/uv/install.sh | sh")
                print(f"  • Or with pip: pip install uv")
                print(f"  • Or with brew: brew install uv{Colors.RESET}\n")
                
                error_msg = f"uvx requires 'uv' to be installed (server: {server_name})"
                self._failed_installations[cache_key] = error_msg
                raise MCPCommandNotFoundError(error_msg)
        
        # Check if the command is available
        if not self._check_command_available(command):
            error_msg = (
                f"Command '{command}' is not available.\n"
                f"Please install the necessary tools first."
            )
            logger.error(error_msg)
            self._failed_installations[cache_key] = error_msg
            raise MCPCommandNotFoundError(error_msg)
        
        # Check if the package is installed
        if await self._check_package_installed(command, args):
            logger.debug(f"The dependencies of the MCP server '{server_name}' are installed")
            return True
        
        # Extract package name for display
        if command == "npx":
            package_name = self._extract_npm_package(args)
            package_type = "npm"
        elif command == "uvx":
            package_name = self._extract_python_package(args)
            package_type = "Python"
        elif command == "uv":
            package_name = self._extract_uv_package(args)
            package_type = "Python"
        else:
            package_name = f"{command} {' '.join(args)}"
            package_type = "package"
        
        # Build the message for displaying the install command
        install_cmd = self._get_install_command(command, args)
        
        # If we can't determine an install command, show helpful message
        if not install_cmd:
            print(f"\n{Colors.YELLOW}Cannot automatically install dependencies for '{server_name}'{Colors.RESET}")
            print(f"{Colors.GRAY}Command: {command} {' '.join(args)}{Colors.RESET}")
            print(f"\n{Colors.WHITE}This MCP server may require manual installation or configuration.{Colors.RESET}")
            print(f"{Colors.GRAY}Please refer to the MCP server's official documentation for installation instructions.{Colors.RESET}\n")
            
            error_msg = f"Manual installation required for '{server_name}' (command: {command})"
            self._failed_installations[cache_key] = error_msg
            raise MCPDependencyError(error_msg)
        
        install_cmd_str = ' '.join(install_cmd)
        
        # Build the message
        message = (
            f"\n{Colors.WHITE}The MCP server needs to install dependencies{Colors.RESET}\n\n"
            f"Server name: {Colors.CYAN}{server_name}{Colors.RESET}\n"
            f"Package type: {Colors.YELLOW}{package_type}{Colors.RESET}\n"
            f"Package name: {Colors.YELLOW}{package_name or 'Unknown'}{Colors.RESET}\n"
            f"Install command: {Colors.GRAY}{install_cmd_str}{Colors.RESET}\n\n"
            f"{Colors.YELLOW}Whether to install this dependency package?{Colors.RESET}"
        )
        
        # Ask the user
        if not await self._ask_user(message):
            error_msg = f"User cancelled the dependency installation for '{server_name}'"
            logger.warning(error_msg)
            self._failed_installations[cache_key] = error_msg
            raise MCPInstallationCancelledError(error_msg)
        
        # Execute installation
        success = await self._install_package(command, args)
        
        if not success:
            error_msg = f"Dependency installation failed for '{server_name}'"
            logger.error(error_msg)
            self._failed_installations[cache_key] = error_msg
            raise MCPInstallationFailedError(error_msg)
        
        return True


# Global singleton instance
_global_installer: Optional[MCPInstallerManager] = None


def get_global_installer() -> MCPInstallerManager:
    """Get the global installer manager instance."""
    global _global_installer
    if _global_installer is None:
        _global_installer = MCPInstallerManager()
    return _global_installer

def set_global_installer(installer: MCPInstallerManager) -> None:
    """Set the global installer manager instance."""
    global _global_installer
    _global_installer = installer