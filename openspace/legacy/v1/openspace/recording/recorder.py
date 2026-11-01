import datetime
import json
from typing import Any, Dict, List, Optional
from pathlib import Path

from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


class TrajectoryRecorder:
    def __init__(
        self,
        task_name: str = "",
        log_dir: str = "./logs/trajectories",
        enable_screenshot: bool = True,
        enable_video: bool = False,
        server_url: Optional[str] = None,
    ):
        """
        Initialize trajectory recorder
        
        Args:
            task_name: task name (optional, will be saved in metadata)
            log_dir: log directory
            enable_screenshot: whether to save screenshots (through platforms.ScreenshotClient)
            enable_video: whether to enable video recording (through platform.RecordingClient)
            server_url: local_server address (None = read from config/environment variables)
        """
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # Simplify naming rule: add prefix if task_name is provided, otherwise use timestamp only
        if task_name:
            folder_name = f"{task_name}_{timestamp}"
        else:
            folder_name = timestamp
        
        self.trajectory_dir = Path(log_dir) / folder_name
        self.trajectory_dir.mkdir(parents=True, exist_ok=True)
        
        # Create screenshots directory
        if enable_screenshot:
            self.screenshots_dir = self.trajectory_dir / "screenshots"
            self.screenshots_dir.mkdir(exist_ok=True)
        else:
            self.screenshots_dir = None
        
        # Config
        self.task_name = task_name
        self.enable_screenshot = enable_screenshot
        self.enable_video = enable_video
        self.server_url = server_url
        
        # Trajectory data
        self.steps: List[Dict] = []
        self.step_counter = 0
        
        # Metadata
        self.metadata = {
            "task_name": task_name,
            "start_time": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "enable_screenshot": enable_screenshot,
            "enable_video": enable_video,
        }
        
        # Video recorder (lazy initialization)
        self._video_recorder = None
        
        # Save initial metadata
        self._save_metadata()
    
    async def record_step(
        self,
        backend: str,
        tool: str,
        command: str,
        result: Optional[Dict[str, Any]] = None,
        parameters: Optional[Dict[str, Any]] = None,
        screenshot: Optional[bytes] = None,
        extra: Optional[Dict[str, Any]] = None,
        auto_screenshot: bool = False,
    ) -> Dict[str, Any]:
        """
        Record one step operation
        
        Args:
            backend: backend type (gui/shell/mcp/web/system)
            tool: tool name (name of BaseTool)
            command: human-readable core command
            result: execution result
            parameters: tool parameters
            screenshot: screenshot bytes (if provided)
            extra: extra information (e.g. server field for MCP)
            auto_screenshot: whether to automatically capture screenshot (through platforms.ScreenshotClient)
        """
        self.step_counter += 1
        step_num = self.step_counter
        timestamp = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        step_info = {
            "step": step_num,
            "timestamp": timestamp,
            "backend": backend,
        }

        # MCP needs to record server (between backend and tool)
        if extra and "server" in extra:
            step_info["server"] = extra.pop("server")

        # General fields
        step_info["tool"] = tool  # BaseTool name
        step_info["command"] = command  # human-readable core command

        # parameters unified write to top level
        if parameters:
            step_info["parameters"] = parameters
        elif extra and "parameters" in extra:
            step_info["parameters"] = extra.pop("parameters")

        # Execution result remains original
        step_info["result"] = result or {}

        # Other extra information (e.g. coordinates/url) only added when needed
        if extra:
            step_info.update(extra)
        
        # Automatic screenshot (if enabled and no screenshot provided)
        if auto_screenshot and screenshot is None and self.enable_screenshot:
            screenshot = await self._capture_screenshot()
        
        # Save screenshot
        if screenshot and self.enable_screenshot and self.screenshots_dir:
            screenshot_filename = f"step_{step_num:03d}.png"
            screenshot_path = self.screenshots_dir / screenshot_filename
            with open(screenshot_path, "wb") as f:
                f.write(screenshot)
            step_info["screenshot"] = f"screenshots/{screenshot_filename}"
        
        # Add to trajectory
        self.steps.append(step_info)
        
        # Save to traj.jsonl in real time
        await self._append_to_traj_file(step_info)
        
        return step_info
    
    async def _capture_screenshot(self) -> Optional[bytes]:
        """Capture screenshot automatically through platforms.ScreenshotClient"""
        try:
            from openspace.platforms import ScreenshotClient
            
            # Lazy initialization screenshot client
            if not hasattr(self, '_screenshot_client'):
                try:
                    self._screenshot_client = ScreenshotClient(base_url=self.server_url)
                except Exception:
                    self._screenshot_client = None
                    return None
            
            if self._screenshot_client is None:
                return None
            
            return await self._screenshot_client.capture()
        
        except Exception:
            return None
    
    async def save_init_screenshot(self, screenshot: bytes, filename: str = "init.png"):
        """Save initial screenshot to screenshots dir and update metadata."""
        if not (self.enable_screenshot and self.screenshots_dir and screenshot):
            return
        try:
            filepath = self.screenshots_dir / filename
            with open(filepath, "wb") as f:
                f.write(screenshot)
            # Update metadata
            self.metadata["init_screenshot"] = f"screenshots/{filename}"
            self._save_metadata()
        except Exception as e:
            logger.debug(f"Failed to save initial screenshot: {e}")
    
    async def _append_to_traj_file(self, step_info: Dict[str, Any]):
        """Add step to traj.jsonl file"""
        traj_file = self.trajectory_dir / "traj.jsonl"
        try:
            line = json.dumps(step_info, ensure_ascii=False, default=str)
            with open(traj_file, "a", encoding="utf-8") as f:
                f.write(line)
                f.write("\n")
        except Exception as e:
            logger.warning(f"Failed to append step {step_info.get('step', '?')} to traj.jsonl: {e}")
    
    def _save_metadata(self):
        """Save metadata to metadata.json"""
        metadata_file = self.trajectory_dir / "metadata.json"
        with open(metadata_file, "w", encoding="utf-8") as f:
            json.dump(self.metadata, f, indent=2, ensure_ascii=False)
    
    async def start_video_recording(self):
        """Start video recording (through platform.RecordingClient)"""
        if not self.enable_video:
            return
        
        try:
            from openspace.recording.video import VideoRecorder
            
            video_path = self.trajectory_dir / "recording.mp4"
            self._video_recorder = VideoRecorder(str(video_path), base_url=self.server_url)
            
            success = await self._video_recorder.start()
            if not success:
                self._video_recorder = None
        
        except Exception as e:
            logger.warning(f"Video recording failed to start: {e}")
            self._video_recorder = None
    
    async def stop_video_recording(self):
        """Stop video recording"""
        if self._video_recorder:
            try:
                await self._video_recorder.stop()
            except Exception:
                pass
            finally:
                self._video_recorder = None
    
    async def add_metadata(self, key: str, value: Any):
        """Add metadata"""
        self.metadata[key] = value
        self._save_metadata()
    
    async def finalize(self):
        """Finalize recording, save final information"""
        self.metadata["end_time"] = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        self.metadata["total_steps"] = self.step_counter
        
        # Backend statistics
        backend_counts = {}
        for step in self.steps:
            backend = step.get("backend", "unknown")
            backend_counts[backend] = backend_counts.get(backend, 0) + 1
        self.metadata["backend_counts"] = backend_counts
        
        self._save_metadata()

        # Close internal ScreenshotClient, avoid unclosed session warning
        await self._cleanup_screenshot_client()

        # Stop video recording
        await self.stop_video_recording()
        
        logger.info(f"Recording completed: {self.trajectory_dir} (steps: {self.step_counter})")
    
    async def _cleanup_screenshot_client(self):
        """Cleanup screenshot client resources"""
        if hasattr(self, '_screenshot_client') and self._screenshot_client:
            try:
                await self._screenshot_client.close()
            except Exception as e:
                logger.debug(f"Failed to close screenshot client: {e}")
            finally:
                self._screenshot_client = None
    
    def __del__(self):
        """Ensure resources are cleaned up even if finalize() is not called"""
        # Note: This is a safety net. Best practice is to call finalize() explicitly.
        if hasattr(self, '_video_recorder') and self._video_recorder:
            logger.warning(
                f"TrajectoryRecorder for {self.trajectory_dir} was not finalized properly. "
                "Consider calling finalize() or using async context manager."
            )
    
    def get_trajectory_dir(self) -> str:
        """Get trajectory directory path"""
        return str(self.trajectory_dir)
    
    async def __aenter__(self):
        """Async context manager entry"""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit - ensures finalize() is called"""
        await self.finalize()
        return False

async def record_gui_step(
    recorder: TrajectoryRecorder,
    command: str,
    task_description: str,
    result: Dict[str, Any] = None,
    screenshot: Optional[bytes] = None,
    max_steps: int = 10,
    tool: str = "gui_agent",
) -> Dict[str, Any]:
    """
    Record GUI step
    
    Args:
        recorder: recorder instance
        command: actual executed pyautogui command (e.g. "pyautogui.moveTo(960, 540)")
        task_description: task description
        result: execution result
        screenshot: screenshot
        max_steps: maximum number of steps
        tool: tool name
    """
    parameters = {
        "task_description": task_description,
        "max_steps": max_steps,
    }
    
    return await recorder.record_step(
        backend="gui",
        tool=tool,
        command=command,
        result=result,
        parameters=parameters,
        screenshot=screenshot,
    )


async def record_shell_step(
    recorder: TrajectoryRecorder,
    command: str,
    exit_code: int,
    stdout: Optional[str] = None,
    stderr: Optional[str] = None,
    screenshot: Optional[bytes] = None,
    tool: str = "shell_agent",
) -> Dict[str, Any]:
    """
    Record Shell step
    
    Args:
        recorder: recorder instance
        command: command executed
        exit_code: exit code
        stdout: standard output (simplified version, not saved completely)
        stderr: standard error (simplified version)
        screenshot: screenshot
        tool: tool name
    """
    stdout_brief = stdout[:200] + "..." if stdout and len(stdout) > 200 else stdout
    stderr_brief = stderr[:200] + "..." if stderr and len(stderr) > 200 else stderr
    
    result = {
        "status": "success" if exit_code == 0 else "error",
        "exit_code": exit_code,
        "stdout": stdout_brief,
        "stderr": stderr_brief,
    }
    
    return await recorder.record_step(
        backend="shell",
        tool=tool,
        command=command,
        result=result,
        screenshot=screenshot,
    )

async def record_mcp_step(
    recorder: TrajectoryRecorder,
    server: str,
    tool_name: str,
    parameters: Dict[str, Any],
    result: Any,
    screenshot: Optional[bytes] = None,
) -> Dict[str, Any]:
    """
    Record MCP step
    
    Args:
        recorder: recorder instance
        server: MCP server name
        tool_name: tool name
        parameters: tool parameters
        result: execution result
        screenshot: screenshot
    """
    command = f"{server}.{tool_name}"
    
    result_str = str(result)
    result_brief = result_str[:200] + "..." if len(result_str) > 200 else result_str
    
    return await recorder.record_step(
        backend="mcp",
        tool=tool_name,
        command=command,
        result={"status": "success", "output": result_brief},
        parameters=parameters,
        screenshot=screenshot,
        extra={
            "server": server,
        }
    )


async def record_web_step(
    recorder: TrajectoryRecorder,
    query: str,
    result: Dict[str, Any],
    screenshot: Optional[bytes] = None,
    tool: str = "deep_research_agent",
) -> Dict[str, Any]:
    """
    Record Web step (deep research)
    
    Args:
        recorder: recorder instance
        query: search query
        result: execution result
        screenshot: screenshot
        tool: tool name
    """
    command = query  # directly use query as command
    
    return await recorder.record_step(
        backend="web",
        tool=tool,
        command=command,
        result=result,
        screenshot=screenshot,
    )