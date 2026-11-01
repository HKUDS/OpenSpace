"""
OpenSpace UI Integration

Integrates the UI system with OpenSpace core components.
Provides hooks and callbacks to update UI in real-time.
"""

import asyncio
from typing import Optional

from openspace.utils.ui import OpenSpaceUI, AgentStatus
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


class UIIntegration:
    """
    UI Integration for OpenSpace
    
    Connects OpenSpace components with the UI system to provide real-time
    visualization of agent activities and execution flow.
    """
    
    def __init__(self, ui: OpenSpaceUI):
        """
        Initialize UI integration
        
        Args:
            ui: OpenSpaceUI instance
        """
        self.ui = ui
        self._update_task: Optional[asyncio.Task] = None
        self._running = False
        
        # Tracked components
        self._llm_client = None
        self._grounding_client = None
    
    def attach_llm_client(self, llm_client):
        """
        Attach LLM client
        
        Args:
            llm_client: LLMClient instance
        """
        self._llm_client = llm_client
        logger.debug("UI attached to LLMClient")
    
    def attach_grounding_client(self, grounding_client):
        """
        Attach grounding client
        
        Args:
            grounding_client: GroundingClient instance
        """
        self._grounding_client = grounding_client
        logger.debug("UI attached to GroundingClient")
    
    async def start_monitoring(self, poll_interval: float = 0.5):
        """
        Start monitoring and updating UI
        
        Args:
            poll_interval: Update interval in seconds
        """
        if self._running:
            logger.warning("UI monitoring already running")
            return
        
        self._running = True
        
        # Immediately update UI once before starting the loop
        await self._update_ui()
        
        self._update_task = asyncio.create_task(
            self._monitor_loop(poll_interval)
        )
        logger.info("UI monitoring started")
    
    async def stop_monitoring(self):
        """Stop monitoring"""
        if not self._running:
            return
        
        self._running = False
        
        if self._update_task:
            self._update_task.cancel()
            try:
                await self._update_task
            except asyncio.CancelledError:
                pass
        
        logger.info("UI monitoring stopped")
    
    async def _monitor_loop(self, poll_interval: float):
        """
        Main monitoring loop
        
        Args:
            poll_interval: Update interval in seconds
        """
        while self._running:
            try:
                await self._update_ui()
                await asyncio.sleep(poll_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"UI update error: {e}", exc_info=True)
    
    async def _update_ui(self):
        """Update UI with current state"""
        # Update grounding backends info
        if self._grounding_client:
            backends = []
            try:
                # Get list of providers
                providers = self._grounding_client.list_providers()
                
                for backend_type, provider in providers.items():
                    backend_name = backend_type.value if hasattr(backend_type, 'value') else str(backend_type)
                    
                    backend_info = {
                        "name": backend_name,
                        "type": backend_name,  # gui, shell, mcp, system, web
                        "servers": []
                    }
                    
                    # For MCP provider, get server names
                    if backend_name == "mcp":
                        try:
                            # Try to get MCP sessions from provider
                            if hasattr(provider, '_sessions'):
                                backend_info["servers"] = list(provider._sessions.keys())
                        except Exception:
                            pass
                    
                    backends.append(backend_info)
                
                self.ui.update_grounding_backends(backends)
            except Exception as e:
                logger.debug(f"Failed to update grounding backends: {e}")
        
        # Refresh display
        self.ui.update_display()
    
    # Event handlers - to be called by agents
    
    def on_agent_start(self, agent_name: str, activity: str):
        """
        Called when agent starts an activity
        
        Args:
            agent_name: Agent name
            activity: Activity description
        """
        self.ui.update_agent_status(agent_name, AgentStatus.EXECUTING)
        self.ui.add_agent_activity(agent_name, activity)
        self.ui.add_log(f"{agent_name}: {activity}", level="info")
    
    def on_agent_thinking(self, agent_name: str):
        """
        Called when agent is thinking
        
        Args:
            agent_name: Agent name
        """
        self.ui.update_agent_status(agent_name, AgentStatus.THINKING)
    
    def on_agent_complete(self, agent_name: str, result: str = ""):
        """
        Called when agent completes an activity
        
        Args:
            agent_name: Agent name
            result: Result description
        """
        self.ui.update_agent_status(agent_name, AgentStatus.IDLE)
        if result:
            self.ui.add_log(f"{agent_name}: {result}", level="success")
    
    def on_llm_call(self, model: str, prompt_length: int):
        """
        Called when LLM is called
        
        Args:
            model: Model name
            prompt_length: Prompt length
        """
        self.ui.update_metrics(
            llm_calls=self.ui.metrics.get("llm_calls", 0) + 1
        )
        self.ui.add_log(f"LLM call: {model} (prompt: {prompt_length} chars)", level="debug")
    
    def on_grounding_call(self, backend: str, action: str):
        """
        Called when grounding backend is called
        
        Args:
            backend: Backend name
            action: Action description
        """
        self.ui.add_grounding_operation(backend, action, status="pending")
        self.ui.add_log(f"Grounding [{backend}]: {action}", level="info")
    
    def on_grounding_complete(self, backend: str, action: str, success: bool):
        """
        Called when grounding operation completes
        
        Args:
            backend: Backend name
            action: Action description
            success: Whether operation succeeded
        """
        status = "success" if success else "error"
        
        # Update last operation status
        for op in reversed(self.ui.grounding_operations):
            if op["backend"] == backend and op["action"] == action and op["status"] == "pending":
                op["status"] = status
                break
        
        level = "success" if success else "error"
        result = "✓" if success else "✗"
        self.ui.add_log(f"Grounding [{backend}]: {action} {result}", level=level)
    
    
    def on_iteration(self, iteration: int):
        """
        Called on each iteration
        
        Args:
            iteration: Iteration number
        """
        self.ui.update_metrics(iterations=iteration)
    
    def on_error(self, message: str):
        """
        Called when an error occurs
        
        Args:
            message: Error message
        """
        self.ui.add_log(f"ERROR: {message}", level="error")


class UILoggingHandler:
    """
    Logging handler that forwards logs to UI
    """
    
    def __init__(self, ui: OpenSpaceUI):
        """
        Initialize logging handler
        
        Args:
            ui: OpenSpaceUI instance
        """
        self.ui = ui
    
    def emit(self, record):
        """
        Emit a log record to UI
        
        Args:
            record: Log record
        """
        level_map = {
            "DEBUG": "debug",
            "INFO": "info",
            "WARNING": "warning",
            "ERROR": "error",
            "CRITICAL": "error",
        }
        
        level = level_map.get(record.levelname, "info")
        message = record.getMessage()
        
        # Filter out noisy logs
        if any(skip in message.lower() for skip in ["processing card", "workflow poll"]):
            return
        
        self.ui.add_log(message, level=level)


def create_integration(ui: OpenSpaceUI) -> UIIntegration:
    """
    Create UI integration instance
    
    Args:
        ui: OpenSpaceUI instance
        
    Returns:
        UIIntegration instance
    """
    return UIIntegration(ui)