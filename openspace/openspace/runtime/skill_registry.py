from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)


@dataclass(slots=True)
class SkillRegistryRoots:
    skill_dirs: list[Path]
    skill_dir_sources: dict[str, str]
    skill_dir_loaded_from: dict[str, str]
    skill_override_dir: Path


def _configured_skill_dirs(configured_skill_dirs: Iterable[str | Path] | None) -> list[Path]:
    return [Path(path) for path in configured_skill_dirs or [] if str(path).strip()]


def discover_skill_registry_roots(
    *,
    workspace_dir: str | Path | None = None,
    configured_skill_dirs: Iterable[str | Path] | None = None,
) -> SkillRegistryRoots | None:
    """Discover local skill roots using the runtime's canonical order."""

    from openspace.skill_engine import default_project_skill_roots, default_user_skill_roots

    skill_paths: list[Path] = []
    sources: dict[str, str] = {}
    loaded_from: dict[str, str] = {}

    def add_skill_root(path: Path, *, source: str, loaded: str, label: str) -> None:
        expanded = Path(path).expanduser()
        if not expanded.exists():
            return
        resolved = str(expanded.resolve())
        if resolved in sources:
            return
        skill_paths.append(expanded)
        sources[resolved] = source
        loaded_from[resolved] = loaded
        logger.info("%s skill dir: %s", label, expanded)

    host_dirs_raw = os.environ.get("OPENSPACE_HOST_SKILL_DIRS", "")
    if host_dirs_raw:
        for raw_path in host_dirs_raw.split(","):
            raw_path = raw_path.strip()
            if not raw_path:
                continue
            path = Path(raw_path)
            if path.exists():
                add_skill_root(path, source="project", loaded="skills", label="Host")
            else:
                logger.warning("Host skill dir does not exist: %s", raw_path)

    for path in _configured_skill_dirs(configured_skill_dirs):
        if path.exists():
            add_skill_root(path, source="project", loaded="skills", label="Configured")
        else:
            logger.warning("Configured skill dir does not exist: %s", path)

    project_root = Path(workspace_dir or os.getcwd()).expanduser()
    for root in default_project_skill_roots(project_root, cwd=os.getcwd()):
        add_skill_root(root, source="project", loaded="skills", label="Project")
    for root in default_user_skill_roots():
        add_skill_root(root, source="user", loaded="skills", label="User")

    builtin_skills = Path(__file__).resolve().parents[1] / "skills"
    add_skill_root(builtin_skills, source="bundled", loaded="bundled", label="Bundled")

    if not skill_paths:
        return None

    return SkillRegistryRoots(
        skill_dirs=skill_paths,
        skill_dir_sources=sources,
        skill_dir_loaded_from=loaded_from,
        skill_override_dir=project_root / ".openspace" / "skill-overlays",
    )


def build_skill_registry(
    *,
    workspace_dir: str | Path | None = None,
    configured_skill_dirs: Iterable[str | Path] | None = None,
    metadata_only_discovery: bool = False,
) -> Any | None:
    """Build and populate a SkillRegistry from canonical runtime roots."""

    from openspace.skill_engine import SkillRegistry

    roots = discover_skill_registry_roots(
        workspace_dir=workspace_dir,
        configured_skill_dirs=configured_skill_dirs,
    )
    if roots is None:
        logger.debug("No skill directories found, skills disabled")
        return None

    registry = SkillRegistry(
        skill_dirs=roots.skill_dirs,
        skill_dir_sources=roots.skill_dir_sources,
        skill_dir_loaded_from=roots.skill_dir_loaded_from,
        skill_override_dir=roots.skill_override_dir,
        metadata_only_discovery=metadata_only_discovery,
    )
    registry.discover()
    return registry
