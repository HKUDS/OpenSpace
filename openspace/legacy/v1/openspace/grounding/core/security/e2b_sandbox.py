"""
E2B Sandbox implementation.

This module provides a concrete implementation of BaseSandbox using E2B.
"""

import os
from typing import Any, Dict, Optional, TYPE_CHECKING

from openspace.utils.logging import Logger
from .sandbox import BaseSandbox
from ..types import SandboxOptions

logger = Logger.get_logger(__name__)

# Import E2B SDK components (optional dependency)
if TYPE_CHECKING:
    # For type checking purposes only
    try:
        from e2b_code_interpreter import CommandHandle, Sandbox
    except ImportError:
        CommandHandle = None  # type: ignore
        Sandbox = None  # type: ignore

try:
    logger.debug("Attempting to import e2b_code_interpreter...")
    from e2b_code_interpreter import (  # type: ignore
        CommandHandle,
        Sandbox,
    )
    logger.debug("Successfully imported e2b_code_interpreter")
    E2B_AVAILABLE = True
except ImportError as e:
    logger.debug(f"Failed to import e2b_code_interpreter: {e}")
    CommandHandle = None  # type: ignore
    Sandbox = None  # type: ignore
    E2B_AVAILABLE = False


class E2BSandbox(BaseSandbox):
    """E2B sandbox implementation for secure code execution."""
    
    def __init__(self, options: SandboxOptions):
        """Initialize E2B sandbox.
        
        Args:
            options: Sandbox configuration options including:
                - api_key: E2B API key (or use E2B_API_KEY env var)
                - sandbox_template_id: Template ID for the sandbox (default: "base")
                - timeout: Command execution timeout in seconds
        """
        super().__init__(options)
        
        if not E2B_AVAILABLE:
            raise ImportError(
                "E2B SDK (e2b-code-interpreter) not found. Please install it with "
                "'pip install e2b-code-interpreter'."
            )
        
        # Get API key from options or environment
        self.api_key = options.get("api_key") or os.environ.get("E2B_API_KEY")
        if not self.api_key:
            raise ValueError(
                "E2B API key is required. Provide it via 'options.api_key'"
                " or the E2B_API_KEY environment variable."
            )
        
        # Get sandbox configuration
        self.sandbox_template_id = options.get("sandbox_template_id", "base")
        self.timeout = options.get("timeout", 600)  # Default 10 minutes
        
        # Sandbox instance (using Any to avoid import issues with optional dependency)
        self._sandbox: Any = None
        self._process: Any = None
        
    async def start(self) -> bool:
        """Start the E2B sandbox instance.
        
        Returns:
            True if sandbox started successfully, False otherwise.
        """
        if self._active:
            logger.debug("E2B sandbox already active")
            return True
        
        try:
            logger.debug(f"Creating E2B sandbox with template: {self.sandbox_template_id}")
            self._sandbox = Sandbox(
                template=self.sandbox_template_id,
                api_key=self.api_key,
            )
            self._active = True
            logger.info(f"E2B sandbox started successfully (template: {self.sandbox_template_id})")
            return True
            
        except Exception as e:
            logger.error(f"Failed to start E2B sandbox: {e}")
            self._active = False
            return False
    
    async def stop(self) -> None:
        """Stop the E2B sandbox instance."""
        if not self._active:
            logger.debug("E2B sandbox not active")
            return
        
        try:
            # Terminate any running process
            if self._process:
                try:
                    logger.debug("Terminating sandbox process")
                    self._process.kill()
                except Exception as e:
                    logger.warning(f"Error terminating sandbox process: {e}")
                finally:
                    self._process = None
            
            # Close the sandbox
            if self._sandbox:
                try:
                    logger.debug("Closing E2B sandbox instance")
                    self._sandbox.kill()
                    logger.info("E2B sandbox stopped successfully")
                except Exception as e:
                    logger.warning(f"Error closing E2B sandbox: {e}")
                finally:
                    self._sandbox = None
            
            self._active = False
            
        except Exception as e:
            logger.error(f"Error stopping E2B sandbox: {e}")
            raise
    
    async def execute_safe(self, command: str, **kwargs) -> Any:
        """Execute a command safely in the E2B sandbox.
        
        Args:
            command: The command to execute
            **kwargs: Additional options:
                - envs: Environment variables (dict)
                - timeout: Command timeout in milliseconds
                - background: Run in background (bool)
                - on_stdout: Stdout callback function
                - on_stderr: Stderr callback function
        
        Returns:
            CommandHandle object representing the running process
        """
        if not self._active or not self._sandbox:
            raise RuntimeError("E2B sandbox is not active. Call start() first.")
        
        try:
            # Extract execution options
            envs = kwargs.get("envs", {})
            timeout = kwargs.get("timeout", self.timeout * 1000)  # Convert to ms
            background = kwargs.get("background", False)
            on_stdout = kwargs.get("on_stdout")
            on_stderr = kwargs.get("on_stderr")
            
            logger.debug(f"Executing command in E2B sandbox: {command}")
            
            # Execute the command
            self._process = self._sandbox.commands.run(
                command,
                envs=envs,
                timeout=timeout,
                background=background,
                on_stdout=on_stdout,
                on_stderr=on_stderr,
            )
            
            return self._process
            
        except Exception as e:
            logger.error(f"Failed to execute command in E2B sandbox: {e}")
            raise
    
    def get_connector(self) -> Any:
        """Get the underlying E2B sandbox connector.
        
        Returns:
            The E2B Sandbox instance, or None if not active.
        """
        return self._sandbox
    
    def get_host(self, port: int) -> str:
        """Get the host URL for a specific port.
        
        Args:
            port: The port number to get the host for
            
        Returns:
            The host URL string
            
        Raises:
            RuntimeError: If sandbox is not active
        """
        if not self._active or not self._sandbox:
            raise RuntimeError("E2B sandbox is not active. Call start() first.")
        
        return self._sandbox.get_host(port)
    
    @property
    def sandbox(self) -> Any:
        """Get the underlying E2B Sandbox instance."""
        return self._sandbox
    
    @property
    def process(self) -> Any:
        """Get the current running process handle."""
        return self._process

