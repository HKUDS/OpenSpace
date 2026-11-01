"""Path helpers for OpenSpace instruction and auto memory.

Implementation notes:
- ``utils/config.ts::getMemoryPath``
- ``utils/config.ts::getManagedClaudeRulesDir``
- ``utils/config.ts::getUserClaudeRulesDir``

OpenSpace keeps the same branch structure but renames
``CLAUDE.md``/``.claude`` to ``OPENSPACE.md``/``.openspace``.
Step 15.2 fills in the ``AutoMem`` branch via ``memdir.py``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Literal, Optional

MemorySource = Literal["Managed", "User", "Project", "Local", "AutoMem"]

OPENSPACE_MD = "OPENSPACE.md"
OPENSPACE_LOCAL_MD = "OPENSPACE.local.md"
OPENSPACE_DIR = ".openspace"
RULES_DIR = "rules"
MANAGED_OPENSPACE_DIR = Path("/etc/openspace")
OPENSPACE_CONFIG_HOME_ENV = "OPENSPACE_CONFIG_HOME"


def get_openspace_config_home_dir(config_home: Optional[str | Path] = None) -> Path:
    """Return the user-level OpenSpace config directory.

    OpenSpace uses ``getClaudeConfigHomeDir()``.  OS does not yet have a global
    config-home helper, so this keeps the standard ``~/.openspace`` default
    and allows tests/embedders to pass an explicit directory.
    """

    if config_home is not None:
        return Path(config_home).expanduser().resolve()
    env_home = os.environ.get(OPENSPACE_CONFIG_HOME_ENV)
    if env_home:
        return Path(env_home).expanduser().resolve()
    return (Path.home() / ".openspace").resolve()


def get_memory_path(
    memory_type: MemorySource,
    *,
    cwd: Optional[str | Path] = None,
    project_root: Optional[str | Path] = None,
    config_home: Optional[str | Path] = None,
    managed_dir: Optional[str | Path] = None,
) -> Optional[Path]:
    """OpenSpace ``getMemoryPath`` equivalent for OpenSpace names.

    OpenSpace's team-memory branch is skipped by DEC-025 and is not part of the
    OpenSpace runtime type surface.
    """

    current_dir = Path(cwd or os.getcwd()).expanduser().resolve()
    managed_root = Path(managed_dir or MANAGED_OPENSPACE_DIR).expanduser().resolve()

    if memory_type == "User":
        return get_openspace_config_home_dir(config_home) / OPENSPACE_MD
    if memory_type == "Local":
        return current_dir / OPENSPACE_LOCAL_MD
    if memory_type == "Project":
        return current_dir / OPENSPACE_MD
    if memory_type == "Managed":
        return managed_root / OPENSPACE_MD
    if memory_type == "AutoMem":
        from .memdir import get_auto_mem_entrypoint

        return get_auto_mem_entrypoint(
            cwd=current_dir,
            project_root=project_root or find_project_root(current_dir),
            config_home=config_home,
        )
    raise ValueError(f"Unknown memory type: {memory_type!r}")


def get_managed_openspace_rules_dir(
    *, managed_dir: Optional[str | Path] = None
) -> Path:
    managed_root = Path(managed_dir or MANAGED_OPENSPACE_DIR).expanduser().resolve()
    return managed_root / OPENSPACE_DIR / RULES_DIR


def get_user_openspace_rules_dir(
    *, config_home: Optional[str | Path] = None
) -> Path:
    return get_openspace_config_home_dir(config_home) / RULES_DIR


def find_project_root(cwd: Optional[str | Path] = None) -> Path:
    """Return the git root for *cwd*, falling back to *cwd* itself.

    The 15.1 design doc intentionally scopes project memory to project root
    → cwd instead of OpenSpace's filesystem-root → cwd walk.  The explicit fallback
    keeps non-git directories deterministic.
    """

    current_dir = Path(cwd or os.getcwd()).expanduser().resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(current_dir), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return current_dir

    root = result.stdout.strip()
    if result.returncode == 0 and root:
        try:
            return Path(root).expanduser().resolve()
        except OSError:
            return current_dir
    return current_dir


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_resolve(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except OSError:
        # ``strict=False`` is the default, but resolve can still raise on
        # broken permissions or malformed symlinks.  Absolute normalization is
        # sufficient for paths that will be skipped if unreadable later.
        return path.expanduser().absolute()


def is_rules_file_path(path: str | Path) -> bool:
    parts = Path(path).parts
    return (
        Path(path).name.endswith(".md")
        and OPENSPACE_DIR in parts
        and RULES_DIR in parts
        and _has_adjacent_parts(parts, OPENSPACE_DIR, RULES_DIR)
    )


def is_standard_memory_filename(path: str | Path) -> bool:
    return Path(path).name in {OPENSPACE_MD, OPENSPACE_LOCAL_MD}


def _has_adjacent_parts(parts: tuple[str, ...], first: str, second: str) -> bool:
    for index, part in enumerate(parts[:-1]):
        if part == first and parts[index + 1] == second:
            return True
    return False


def validate_memory_path(
    path: str | Path,
    *,
    memory_type: MemorySource,
    cwd: Optional[str | Path] = None,
    project_root: Optional[str | Path] = None,
    config_home: Optional[str | Path] = None,
    managed_dir: Optional[str | Path] = None,
    allow_external: bool = False,
) -> Path:
    """Validate a top-level OPENSPACE.md/rules path and return its resolved path.

    This is intentionally a structural path check, not a permission-engine
    prompt.  Startup context loading is not model-initiated tool access.
    """

    raw_path = Path(path).expanduser()
    resolved = _safe_resolve(raw_path)

    current_dir = Path(cwd or os.getcwd()).expanduser().resolve()
    project_base = Path(project_root).expanduser().resolve() if project_root else current_dir
    config_base = get_openspace_config_home_dir(config_home)
    managed_base = Path(managed_dir or MANAGED_OPENSPACE_DIR).expanduser().resolve()

    if resolved.suffix != ".md":
        raise ValueError(f"Memory path must be a markdown file: {resolved}")
    if not (
        is_standard_memory_filename(resolved)
        or is_rules_file_path(resolved)
        or _is_user_rules_file(resolved, config_base)
        or _is_managed_rules_file(resolved, managed_base)
        or (memory_type == "AutoMem" and resolved.name == "MEMORY.md")
    ):
        raise ValueError(f"Not an OpenSpace memory path: {resolved}")

    allowed_roots: list[Path]
    if memory_type == "Managed":
        allowed_roots = [managed_base]
    elif memory_type == "User":
        allowed_roots = [config_base]
    elif memory_type == "Project":
        allowed_roots = [project_base]
    elif memory_type == "Local":
        allowed_roots = [current_dir]
    elif memory_type == "AutoMem":
        from .memdir import get_auto_mem_path

        allowed_roots = [
            get_auto_mem_path(
                cwd=current_dir,
                project_root=project_base,
                config_home=config_home,
            )
        ]
    else:
        raise ValueError(f"Unknown memory type: {memory_type!r}")

    if allow_external:
        return resolved
    if not any(_is_relative_to(resolved, root) for root in allowed_roots):
        roots = ", ".join(str(root) for root in allowed_roots)
        raise ValueError(f"Memory path escapes allowed root(s) {roots}: {resolved}")
    return resolved


def _is_user_rules_file(path: Path, config_base: Path) -> bool:
    rules_root = config_base / RULES_DIR
    return path.suffix == ".md" and _is_relative_to(path, rules_root)


def _is_managed_rules_file(path: Path, managed_base: Path) -> bool:
    rules_root = managed_base / OPENSPACE_DIR / RULES_DIR
    return path.suffix == ".md" and _is_relative_to(path, rules_root)
