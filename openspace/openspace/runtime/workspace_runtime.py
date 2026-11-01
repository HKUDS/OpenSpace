from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True, frozen=True)
class WorkspaceResolution:
    workspace_dir: str
    source: str


class WorkspaceRuntime:
    """Resolve per-execution workspace facts without owning persistence."""

    def __init__(self, config_workspace_dir: str | None = None) -> None:
        self._config_workspace_dir = config_workspace_dir

    @classmethod
    def from_config(cls, config: Any) -> "WorkspaceRuntime":
        return cls(config_workspace_dir=getattr(config, "workspace_dir", None))

    def resolve(
        self,
        *,
        request_workspace_dir: str | Path | None,
        recording_workspace_dir: str | Path | None,
        task_id: str,
        context_workspace_dir: str | Path | None = None,
    ) -> WorkspaceResolution:
        if request_workspace_dir:
            return WorkspaceResolution(str(request_workspace_dir), "request")
        if context_workspace_dir:
            return WorkspaceResolution(str(context_workspace_dir), "context")
        if self._config_workspace_dir:
            return WorkspaceResolution(str(self._config_workspace_dir), "config")
        if recording_workspace_dir:
            return WorkspaceResolution(str(recording_workspace_dir), "recording")

        workspace = Path(tempfile.gettempdir()) / "openspace_workspace" / task_id
        workspace.mkdir(parents=True, exist_ok=True)
        return WorkspaceResolution(str(workspace), "temp")

    async def configure_shell_backend(
        self,
        grounding_client: Any,
        workspace_dir: str,
    ) -> None:
        from openspace.grounding.core.types import BackendType

        grounding_client.configure_backend_workspace(
            BackendType.SHELL,
            workspace_dir,
        )
