"""
ClawWork-compatible productivity tools for fair benchmark comparison.

When use_clawwork_productivity is enabled and the livebench package is installed,
these tools are added to the Shell backend so OpenSpace agents have the same
capabilities as ClawWork: search_web, read_webpage, create_file, read_file,
execute_code_sandbox, create_video.
"""

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from openspace.grounding.core.types import BackendType, ToolResult, ToolStatus
from openspace.grounding.core.tool import BaseTool
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

# Lazy import to avoid hard dependency on ClawWork
_LIVEBENCH_AVAILABLE = False
_direct_tools = None
_productivity = None


def _ensure_livebench():
    """Import livebench modules if available."""
    global _LIVEBENCH_AVAILABLE, _direct_tools, _productivity
    if _direct_tools is not None:
        return _LIVEBENCH_AVAILABLE
    try:
        import livebench.tools.direct_tools as dt
        import livebench.tools.productivity as prod
        _direct_tools = dt
        _productivity = prod
        _LIVEBENCH_AVAILABLE = True
    except ImportError as e:
        logger.debug("ClawWork productivity tools not available: %s", e)
        _LIVEBENCH_AVAILABLE = False
    return _LIVEBENCH_AVAILABLE


def _set_global_state_for_productivity(data_path: str, current_date: str) -> None:
    """Set ClawWork global state so productivity tools have data_path/date."""
    if not _direct_tools:
        return
    _direct_tools.set_global_state(
        signature="openspace",
        economic_tracker=None,
        task_manager=None,
        evaluator=None,
        current_date=current_date,
        current_task=None,
        data_path=data_path,
        supports_multimodal=True,
    )


def _dict_to_tool_result(out: Dict[str, Any]) -> ToolResult:
    """Convert ClawWork tool dict to OpenSpace ToolResult."""
    if not isinstance(out, dict):
        return ToolResult(
            status=ToolStatus.ERROR,
            content=str(out),
        )
    err = out.get("error")
    if err:
        return ToolResult(
            status=ToolStatus.ERROR,
            content=err if isinstance(err, str) else json.dumps(err, ensure_ascii=False),
        )
    return ToolResult(
        status=ToolStatus.SUCCESS,
        content=json.dumps(out, ensure_ascii=False, default=str),
    )


def _sync_invoke(tool_any: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    """Invoke a LangChain-style tool (sync) from async context."""
    if hasattr(tool_any, "invoke"):
        return tool_any.invoke(args)
    return tool_any(**args)


class _ProductivityToolBase(BaseTool):
    """Base for productivity tools that delegate to ClawWork."""

    backend_type = BackendType.SHELL

    def __init__(self, session: Any, data_path: str, current_date: str):
        self._session = session
        self._data_path = data_path or "."
        self._current_date = current_date or "default"
        super().__init__()

    async def _arun(self, **kwargs) -> ToolResult:
        raise NotImplementedError("Subclasses must override _arun")

    async def _run_sync_tool(self, tool_obj: Any, args: Dict[str, Any]) -> ToolResult:
        data_path = getattr(self._session, "default_working_dir", None) or self._data_path
        _set_global_state_for_productivity(data_path, self._current_date)
        try:
            result = await asyncio.to_thread(_sync_invoke, tool_obj, args)
            return _dict_to_tool_result(result)
        except Exception as e:
            logger.exception("Productivity tool %s failed", self.name)
            return ToolResult(status=ToolStatus.ERROR, content=str(e))


class SearchWebTool(_ProductivityToolBase):
    _name = "search_web"
    _description = (
        "Search the internet using Tavily or Jina. Returns structured results with "
        "AI-generated answers. Use for up-to-date information."
    )

    async def _arun(self, query: str, max_results: int = 5) -> ToolResult:
        return await self._run_sync_tool(
            _productivity.search_web,
            {"query": query, "max_results": max_results},
        )


class ReadWebpageTool(_ProductivityToolBase):
    _name = "read_webpage"
    _description = (
        "Extract and read web page content from URLs using Tavily Extract. "
        "Returns cleaned text in markdown format."
    )

    async def _arun(self, urls: str, query: Optional[str] = None) -> ToolResult:
        return await self._run_sync_tool(
            _productivity.read_webpage,
            {"urls": urls, "query": query},
        )


class CreateFileProductivityTool(_ProductivityToolBase):
    _name = "create_file"
    _description = (
        "Create a file in the current working directory. "
        "Supports: txt, md, csv, json, xlsx, docx, pdf. "
        "The file is created directly in your workspace."
    )

    async def _arun(
        self,
        filename: str,
        content: str,
        file_type: str = "txt",
    ) -> ToolResult:
        """Create a file via Shell connector so it lands in the task workspace."""
        file_type = file_type.lower().strip()
        valid_types = ["txt", "md", "csv", "json", "xlsx", "docx", "pdf"]
        if file_type not in valid_types:
            return ToolResult(
                status=ToolStatus.ERROR,
                content=f"Invalid file type: {file_type}. Valid: {valid_types}",
            )
        if not filename or not content:
            return ToolResult(status=ToolStatus.ERROR, content="filename and content are required")

        import os
        safe_name = os.path.basename(filename).replace("/", "_").replace("\\", "_")
        # Strip extension from filename if it matches file_type to avoid .docx.docx
        name_root, name_ext = os.path.splitext(safe_name)
        if name_ext.lstrip(".").lower() == file_type:
            safe_name = name_root
        final_name = f"{safe_name}.{file_type}"

        escaped_content = json.dumps(content)
        escaped_name = json.dumps(final_name)

        if file_type in ("txt", "md", "csv"):
            code = (
                "import os\n"
                f"name = {escaped_name}\n"
                f"content = {escaped_content}\n"
                "with open(name, 'w', encoding='utf-8') as f:\n"
                "    f.write(content)\n"
                "sz = os.path.getsize(name)\n"
                "print(f'Created {name} ({sz} bytes)')\n"
            )
        elif file_type == "json":
            code = (
                "import os, json\n"
                f"name = {escaped_name}\n"
                f"content = {escaped_content}\n"
                "data = json.loads(content)\n"
                "with open(name, 'w', encoding='utf-8') as f:\n"
                "    json.dump(data, f, indent=2, ensure_ascii=False)\n"
                "sz = os.path.getsize(name)\n"
                "print(f'Created {name} ({sz} bytes)')\n"
            )
        elif file_type == "xlsx":
            code = (
                "import os, json, io\n"
                "import pandas as pd\n"
                f"name = {escaped_name}\n"
                f"content = {escaped_content}\n"
                "try:\n"
                "    data = json.loads(content)\n"
                "    df = pd.DataFrame(data)\n"
                "except:\n"
                "    df = pd.read_csv(io.StringIO(content))\n"
                "df.to_excel(name, index=False, engine='openpyxl')\n"
                "sz = os.path.getsize(name)\n"
                "print(f'Created {name} ({sz} bytes)')\n"
            )
        elif file_type == "docx":
            code = (
                "import os\n"
                "from docx import Document\n"
                f"name = {escaped_name}\n"
                f"content = {escaped_content}\n"
                "doc = Document()\n"
                "for para in content.split('\\n\\n'):\n"
                "    if para.strip():\n"
                "        doc.add_paragraph(para.strip())\n"
                "doc.save(name)\n"
                "sz = os.path.getsize(name)\n"
                "print(f'Created {name} ({sz} bytes)')\n"
            )
        elif file_type == "pdf":
            code = (
                "import os\n"
                "from reportlab.lib.pagesizes import letter\n"
                "from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer\n"
                "from reportlab.lib.styles import getSampleStyleSheet\n"
                f"name = {escaped_name}\n"
                f"content = {escaped_content}\n"
                "doc = SimpleDocTemplate(name, pagesize=letter)\n"
                "styles = getSampleStyleSheet()\n"
                "story = []\n"
                "for para in content.split('\\n\\n'):\n"
                "    if para.strip():\n"
                "        story.append(Paragraph(para.strip(), styles['Normal']))\n"
                "        story.append(Spacer(1, 12))\n"
                "doc.build(story)\n"
                "sz = os.path.getsize(name)\n"
                "print(f'Created {name} ({sz} bytes)')\n"
            )
        else:
            return ToolResult(status=ToolStatus.ERROR, content=f"Unsupported: {file_type}")

        try:
            from openspace.grounding.backends.shell.session import _parse_shell_result
            working_dir = getattr(self._session, "default_working_dir", None)
            result = await self._session.connector.run_python_script(
                code, timeout=30, working_dir=working_dir,
            )
            stdout, stderr, rc = _parse_shell_result(result)
            if rc != 0:
                return ToolResult(status=ToolStatus.ERROR, content=stderr or f"Failed to create {final_name}")
            return ToolResult(
                status=ToolStatus.SUCCESS,
                content=f"Created {final_name} in workspace. {stdout.strip()}",
            )
        except Exception as e:
            return ToolResult(status=ToolStatus.ERROR, content=f"create_file failed: {e}")


class ReadFileProductivityTool(_ProductivityToolBase):
    _name = "read_file"
    _description = (
        "Read a file in various formats: pdf, docx, xlsx, pptx, png, jpg, jpeg, txt, json, md, csv, html, xml, yaml. "
        "Returns content suitable for LLM consumption (text or images). "
        "Relative paths are resolved against the task workspace directory."
    )

    def _resolve_path(self, file_path: str) -> Path:
        """Resolve relative paths against the task workspace (data_path)."""
        data_path = getattr(self._session, "default_working_dir", None) or self._data_path
        p = Path(file_path)
        if not p.is_absolute():
            resolved = Path(data_path) / p
            if resolved.exists():
                return resolved
            workspace = Path(data_path)
            if workspace.is_dir():
                name = p.name
                for candidate in workspace.rglob(name):
                    return candidate
        return p

    async def _arun(self, filetype: str, file_path: str) -> ToolResult:
        resolved = self._resolve_path(file_path)
        return await self._run_sync_tool(
            _productivity.read_file,
            {"filetype": filetype, "file_path": resolved},
        )


class ExecuteCodeSandboxTool(_ProductivityToolBase):
    _name = "execute_code_sandbox"
    _description = (
        "Execute Python code in a persistent sandbox. Supports artifact download via ARTIFACT_PATH:/path/to/file in output."
    )

    async def _arun(self, code: str, language: str = "python") -> ToolResult:
        return await self._run_sync_tool(
            _productivity.execute_code_sandbox,
            {"code": code, "language": language},
        )


class CreateVideoTool(_ProductivityToolBase):
    _name = "create_video"
    _description = (
        "Create a video from text slides and/or images. Input is a JSON string describing slides; output is MP4. "
        "The video is created in the current working directory."
    )

    async def _arun(
        self,
        slides_json: str,
        output_filename: str,
        width: int = 1280,
        height: int = 720,
        fps: int = 24,
    ) -> ToolResult:
        """Create video via Shell connector so it lands in the task workspace."""
        import os
        safe_name = os.path.basename(output_filename).replace("/", "_").replace("\\", "_")
        if not safe_name.endswith(".mp4"):
            safe_name = safe_name.rsplit(".", 1)[0] if "." in safe_name else safe_name
            safe_name += ".mp4"

        escaped_slides = json.dumps(slides_json)
        escaped_name = json.dumps(safe_name)

        code = (
            "import json, os\n"
            f"slides_json = {escaped_slides}\n"
            f"output_name = {escaped_name}\n"
            f"width, height, fps = {width}, {height}, {fps}\n"
            "slides = json.loads(slides_json)\n"
            "try:\n"
            "    from PIL import Image, ImageDraw, ImageFont\n"
            "    import subprocess, tempfile, shutil\n"
            "    tmpdir = tempfile.mkdtemp()\n"
            "    frame_paths = []\n"
            "    for i, slide in enumerate(slides):\n"
            "        dur = slide.get('duration', 3.0)\n"
            "        n_frames = int(dur * fps)\n"
            "        if slide.get('type') == 'image' and slide.get('path'):\n"
            "            img = Image.open(slide['path']).resize((width, height))\n"
            "        else:\n"
            "            bg = slide.get('bg_color', '#000000')\n"
            "            tc = slide.get('text_color', '#FFFFFF')\n"
            "            img = Image.new('RGB', (width, height), bg)\n"
            "            draw = ImageDraw.Draw(img)\n"
            "            text = slide.get('content', '')\n"
            "            try:\n"
            "                font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 36)\n"
            "            except:\n"
            "                font = ImageFont.load_default()\n"
            "            bbox = draw.textbbox((0, 0), text, font=font)\n"
            "            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]\n"
            "            draw.text(((width - tw) / 2, (height - th) / 2), text, fill=tc, font=font)\n"
            "        for j in range(n_frames):\n"
            "            fp = os.path.join(tmpdir, f'frame_{len(frame_paths):06d}.png')\n"
            "            img.save(fp)\n"
            "            frame_paths.append(fp)\n"
            "    cmd = ['ffmpeg', '-y', '-framerate', str(fps), '-i', os.path.join(tmpdir, 'frame_%06d.png'),\n"
            "           '-c:v', 'libx264', '-pix_fmt', 'yuv420p', output_name]\n"
            "    subprocess.run(cmd, capture_output=True, check=True)\n"
            "    shutil.rmtree(tmpdir)\n"
            "    sz = os.path.getsize(output_name)\n"
            "    print(f'Created {output_name} ({sz} bytes, {len(frame_paths)} frames)')\n"
            "except Exception as e:\n"
            "    print(f'ERROR: {e}')\n"
            "    raise\n"
        )

        try:
            from openspace.grounding.backends.shell.session import _parse_shell_result
            working_dir = getattr(self._session, "default_working_dir", None)
            result = await self._session.connector.run_python_script(
                code, timeout=120, working_dir=working_dir,
            )
            stdout, stderr, rc = _parse_shell_result(result)
            if rc != 0:
                return ToolResult(status=ToolStatus.ERROR, content=stderr or f"Failed to create video {safe_name}")
            return ToolResult(
                status=ToolStatus.SUCCESS,
                content=f"Created video {safe_name} in workspace. {stdout.strip()}",
            )
        except Exception as e:
            return ToolResult(status=ToolStatus.ERROR, content=f"create_video failed: {e}")


def get_productivity_tools(
    session: Any,
    data_path: Optional[str] = None,
    current_date: Optional[str] = None,
) -> List[BaseTool]:
    """
    Return ClawWork-compatible productivity tools if livebench is installed.

    Args:
        session: ShellSession (for compatibility; not used beyond data_path/date).
        data_path: Sandbox root (default: session.default_working_dir or ".").
        current_date: Date segment for sandbox paths (default: "default").

    Returns:
        List of tools to add to the session, or empty list if livebench unavailable.
    """
    if not _ensure_livebench():
        return []
    path = data_path if data_path is not None else getattr(session, "default_working_dir", None) or "."
    date = current_date if current_date is not None else "default"
    return [
        SearchWebTool(session, data_path=path, current_date=date),
        ReadWebpageTool(session, data_path=path, current_date=date),
        CreateFileProductivityTool(session, data_path=path, current_date=date),
        ReadFileProductivityTool(session, data_path=path, current_date=date),
        ExecuteCodeSandboxTool(session, data_path=path, current_date=date),
        CreateVideoTool(session, data_path=path, current_date=date),
    ]


def is_productivity_available() -> bool:
    """Return True if ClawWork productivity tools can be loaded."""
    return _ensure_livebench()
