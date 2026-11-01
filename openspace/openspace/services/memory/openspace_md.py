"""OPENSPACE.md hierarchy loader.

This module loads project, user, and local instruction memory while preserving
OpenSpace's ``OPENSPACE.md`` naming and project-root scoping.
"""

from __future__ import annotations

import fnmatch
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .paths import (
    OPENSPACE_DIR,
    OPENSPACE_LOCAL_MD,
    OPENSPACE_MD,
    RULES_DIR,
    MemorySource,
    find_project_root,
    get_managed_openspace_rules_dir,
    get_memory_path,
    get_user_openspace_rules_dir,
    is_rules_file_path,
    is_standard_memory_filename,
    validate_memory_path,
)

MEMORY_INSTRUCTION_PROMPT = (
    "Codebase and user instructions are shown below. Be sure to adhere to "
    "these instructions. IMPORTANT: These instructions OVERRIDE any default "
    "behavior and you MUST follow them exactly as written."
)
MAX_MEMORY_CHARACTER_COUNT = 40000
MAX_INCLUDE_DEPTH = 5

TEXT_FILE_EXTENSIONS = {
    ".md",
    ".txt",
    ".text",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".xml",
    ".csv",
    ".html",
    ".htm",
    ".css",
    ".scss",
    ".sass",
    ".less",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".mjs",
    ".cjs",
    ".mts",
    ".cts",
    ".py",
    ".pyi",
    ".pyw",
    ".rb",
    ".erb",
    ".rake",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".kts",
    ".scala",
    ".c",
    ".cpp",
    ".OpenSpace",
    ".cxx",
    ".h",
    ".hpp",
    ".hxx",
    ".cs",
    ".swift",
    ".sh",
    ".bash",
    ".zsh",
    ".fish",
    ".ps1",
    ".bat",
    ".cmd",
    ".env",
    ".ini",
    ".cfg",
    ".conf",
    ".config",
    ".properties",
    ".sql",
    ".graphql",
    ".gql",
    ".proto",
    ".vue",
    ".svelte",
    ".astro",
    ".ejs",
    ".hbs",
    ".pug",
    ".jade",
    ".php",
    ".pl",
    ".pm",
    ".lua",
    ".r",
    ".R",
    ".dart",
    ".ex",
    ".exs",
    ".erl",
    ".hrl",
    ".clj",
    ".cljs",
    ".cljc",
    ".edn",
    ".hs",
    ".lhs",
    ".elm",
    ".ml",
    ".mli",
    ".f",
    ".f90",
    ".f95",
    ".for",
    ".cmake",
    ".make",
    ".makefile",
    ".gradle",
    ".sbt",
    ".rst",
    ".adoc",
    ".asciidoc",
    ".org",
    ".tex",
    ".latex",
    ".lock",
    ".log",
    ".diff",
    ".patch",
}

_INCLUDE_RE = re.compile(r"(?:^|\s)@((?:[^\s\\]|\\ )+)")
_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", re.DOTALL)
_HTML_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")
_MEMORY_FILES_CACHE: dict[tuple[object, ...], list["MemoryFileInfo"]] = {}
_SENSITIVE_INCLUDE_EXACT_NAMES = {
    ".env",
    ".envrc",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".credential",
    ".credentials",
    ".secret",
    ".secrets",
    ".token",
    ".tokens",
    ".api_key",
    ".apikey",
    ".kubeconfig",
    "credential",
    "credentials",
    "credentials.json",
    "secret",
    "secrets",
    "token",
    "tokens",
    "token.json",
    "api_key",
    "apikey",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "private_key",
    "private-key",
    "key",
    "certificate",
    "cert",
    "kubeconfig",
    "service-account.json",
    "service_account.json",
    "google-application-credentials.json",
}
_SENSITIVE_INCLUDE_GLOBS = (
    ".env*",
    "*.env",
    "*.env.*",
    "*credential*",
    "*secret*",
    "*token*",
    "*api_key*",
    "*api-key*",
    "*apikey*",
    "*.pem",
    "*.key",
    "*.crt",
    "*.cert",
    "*.cer",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "private_key.*",
    "private-key.*",
    "*_private_key.*",
    "*-private-key.*",
    "*_key.*",
    "*-key.*",
    "certificate.*",
    "cert.*",
)
_SENSITIVE_INCLUDE_DIR_NAMES = {
    ".env",
    ".secrets",
    ".secret",
    "secrets",
    "secret",
    "credentials",
    "credential",
    "tokens",
    "token",
    ".ssh",
    ".aws",
    ".gcloud",
    ".azure",
    ".kube",
    ".docker",
}


@dataclass(frozen=True)
class MemoryFileInfo:
    """A loaded instruction-memory file.

    ``source`` is OpenSpace's ``MemoryType`` renamed for Python readability.
    ``priority`` preserves the eager loading order: larger values are loaded
    later and therefore have higher prompt priority.
    """

    path: Path
    content: str
    source: MemorySource
    priority: int
    parent: Optional[Path] = None
    globs: Optional[list[str]] = None
    content_differs_from_disk: bool = False
    raw_content: Optional[str] = None

    @property
    def type(self) -> MemorySource:
        """legacy-compatible alias for callers comparing ``file.type``."""

        return self.source


def clear_memory_file_caches() -> None:
    """OpenSpace ``clearMemoryFileCaches`` equivalent."""

    _MEMORY_FILES_CACHE.clear()


def _normalize_for_comparison(path: Path) -> str:
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        resolved = path.expanduser().absolute()
    return os.path.normcase(str(resolved))


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _parse_frontmatter_paths(raw_content: str) -> tuple[str, Optional[list[str]]]:
    match = _FRONTMATTER_RE.match(raw_content)
    if not match:
        return raw_content, None

    frontmatter = match.group(1)
    body = raw_content[match.end():]
    paths = _extract_paths_field(frontmatter)
    if not paths:
        return body, None

    patterns = [
        pattern[:-3] if pattern.endswith("/**") else pattern
        for pattern in paths
        if pattern
    ]
    if not patterns or all(pattern == "**" for pattern in patterns):
        return body, None
    return body, patterns


def _extract_paths_field(frontmatter: str) -> list[str]:
    lines = frontmatter.splitlines()
    values: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in line:
            index += 1
            continue
        key, value = line.split(":", 1)
        if key.strip() != "paths":
            index += 1
            continue
        value = value.strip()
        if value:
            values.extend(_split_frontmatter_paths(value))
            index += 1
            continue

        index += 1
        while index < len(lines):
            child = lines[index]
            if child and not child.startswith((" ", "\t", "-")) and ":" in child:
                break
            item = child.strip()
            if item.startswith("-"):
                item = item[1:].strip()
            if item:
                values.extend(_split_frontmatter_paths(item))
            index += 1
    return values


def _split_frontmatter_paths(value: str) -> list[str]:
    value = value.strip().strip("'\"")
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    parts = re.split(r"[\s,]+", value)
    return [part.strip().strip("'\"") for part in parts if part.strip().strip("'\"")]


def strip_html_comments(content: str) -> tuple[str, bool]:
    """Strip block-level HTML comments while preserving fenced code blocks."""

    if "<!--" not in content:
        return content, False

    output: list[str] = []
    stripped = False
    in_fence = False
    in_comment = False

    for line in content.splitlines(keepends=True):
        if line.lstrip().startswith("```") or line.lstrip().startswith("~~~"):
            in_fence = not in_fence
            output.append(line)
            continue
        if in_fence:
            output.append(line)
            continue

        if in_comment:
            stripped = True
            if "-->" in line:
                line = line.split("-->", 1)[1]
                in_comment = False
            else:
                continue

        working = line
        while "<!--" in working:
            before, after = working.split("<!--", 1)
            if "-->" in after:
                _, remainder = after.split("-->", 1)
                working = before + remainder
                stripped = True
                continue
            working = before
            in_comment = True
            stripped = True
            break
        output.append(working)

    return "".join(output), stripped


def _extract_include_paths(content: str, base_path: Path) -> list[Path]:
    include_paths: list[Path] = []
    seen: set[str] = set()
    in_fence = False

    for line in content.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        line_without_comments = _HTML_COMMENT_RE.sub("", line)
        line_without_code = re.sub(r"`[^`]*`", "", line_without_comments)
        for match in _INCLUDE_RE.finditer(line_without_code):
            raw_path = match.group(1)
            if not raw_path:
                continue
            include = raw_path.split("#", 1)[0].replace("\\ ", " ").strip()
            if not _is_valid_include_syntax(include):
                continue
            resolved = _resolve_include_path(include, base_path.parent)
            key = _normalize_for_comparison(resolved)
            if key not in seen:
                seen.add(key)
                include_paths.append(resolved)
    return include_paths


def _is_valid_include_syntax(path: str) -> bool:
    if not path:
        return False
    return (
        path.startswith("./")
        or path.startswith("~/")
        or (path.startswith("/") and path != "/")
        or (
            not path.startswith("@")
            and re.match(r"^[a-zA-Z0-9._-]", path) is not None
            and re.match(r"^[#%^&*()]+", path) is None
        )
    )


def _resolve_include_path(path: str, base_dir: Path) -> Path:
    if path.startswith("~/"):
        return (Path.home() / path[2:]).expanduser().resolve()
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return (base_dir / candidate).resolve()


def _parse_memory_file_content(
    raw_content: str,
    file_path: Path,
    source: MemorySource,
    include_base_path: Optional[Path] = None,
) -> tuple[Optional[MemoryFileInfo], list[Path]]:
    ext = file_path.suffix.lower()
    if ext and ext not in TEXT_FILE_EXTENSIONS:
        return None, []

    without_frontmatter, globs = _parse_frontmatter_paths(raw_content)
    without_comments, _ = strip_html_comments(without_frontmatter)
    include_paths = (
        _extract_include_paths(without_comments, include_base_path)
        if include_base_path is not None
        else []
    )

    final_content = without_comments
    if source == "AutoMem":
        from .memdir import truncate_entrypoint_content

        final_content = truncate_entrypoint_content(without_comments).content
    content_differs = final_content != raw_content
    return (
        MemoryFileInfo(
            path=file_path,
            content=final_content,
            source=source,
            priority=0,
            globs=globs,
            content_differs_from_disk=content_differs,
            raw_content=raw_content if content_differs else None,
        ),
        include_paths,
    )


def _path_in_project(path: Path, project_root: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path.absolute()
    return _is_relative_to(resolved, project_root)


def _is_include_external(path: Path, project_root: Path) -> bool:
    return not _path_in_project(path, project_root)


def _is_instruction_memory_path(path: Path) -> bool:
    return is_standard_memory_filename(path) or is_rules_file_path(path)


def _is_sensitive_include_path(path: Path) -> bool:
    lower_parts = [part.lower() for part in path.parts if part]
    for part in lower_parts:
        if part in _SENSITIVE_INCLUDE_DIR_NAMES:
            return True

    name = path.name.lower()
    if name in _SENSITIVE_INCLUDE_EXACT_NAMES:
        return True
    for pattern in _SENSITIVE_INCLUDE_GLOBS:
        if fnmatch.fnmatchcase(name, pattern):
            return True

    hidden_parts = [
        part
        for part in lower_parts
        if part.startswith(".")
        and part not in {".", ".."}
        and part != ".openspace"
    ]
    if hidden_parts:
        return True

    try:
        from openspace.grounding.core.permissions.filesystem import is_sensitive_path

        sensitive = is_sensitive_path(str(path))
    except Exception:
        name = path.name.lower()
        sensitive = name == ".env" or name.startswith(".env.")
    return sensitive and not _is_instruction_memory_path(path)


def process_memory_file(
    file_path: str | Path,
    source: MemorySource,
    processed_paths: set[str],
    include_external: bool,
    *,
    cwd: Optional[str | Path] = None,
    project_root: Optional[str | Path] = None,
    config_home: Optional[str | Path] = None,
    managed_dir: Optional[str | Path] = None,
    depth: int = 0,
    parent: Optional[str | Path] = None,
    priority: int = 0,
) -> list[MemoryFileInfo]:
    """Recursively process an OPENSPACE.md file and its ``@include`` paths.

    Returns the main file first and included files after it, matching the
    current OpenSpace implementation even though the top-level comment says includes
    are conceptually added before the including file.
    """

    if depth >= MAX_INCLUDE_DEPTH:
        return []

    current_dir = Path(cwd or os.getcwd()).expanduser().resolve()
    project_base = Path(project_root).expanduser().resolve() if project_root else current_dir
    raw_path = Path(file_path).expanduser()
    normalized_path = _normalize_for_comparison(raw_path)
    if normalized_path in processed_paths:
        return []

    try:
        if parent is None:
            resolved_path = validate_memory_path(
                raw_path,
                memory_type=source,
                cwd=current_dir,
                project_root=project_base,
                config_home=config_home,
                managed_dir=managed_dir,
            )
        else:
            resolved_path = raw_path.resolve()
            if (
                not include_external
                and _is_include_external(resolved_path, project_base)
            ):
                return []
            if _is_sensitive_include_path(resolved_path):
                return []
            if resolved_path.suffix.lower() not in TEXT_FILE_EXTENSIONS:
                return []
    except (OSError, ValueError):
        return []

    processed_paths.add(normalized_path)
    processed_paths.add(_normalize_for_comparison(resolved_path))

    raw_content = _read_text(resolved_path)
    if raw_content is None:
        return []

    info, include_paths = _parse_memory_file_content(
        raw_content,
        resolved_path,
        source,
        include_base_path=None if source == "AutoMem" else resolved_path,
    )
    if info is None or not info.content.strip():
        return []

    info = MemoryFileInfo(
        path=info.path,
        content=info.content,
        source=info.source,
        priority=priority,
        parent=Path(parent).expanduser().resolve() if parent else None,
        globs=info.globs,
        content_differs_from_disk=info.content_differs_from_disk,
        raw_content=info.raw_content,
    )

    result = [info]
    for include_path in include_paths:
        result.extend(
            process_memory_file(
                include_path,
                source,
                processed_paths,
                include_external,
                cwd=current_dir,
                project_root=project_base,
                config_home=config_home,
                managed_dir=managed_dir,
                depth=depth + 1,
                parent=resolved_path,
                priority=priority,
            )
        )
    return result


def process_md_rules(
    rules_dir: str | Path,
    source: MemorySource,
    processed_paths: set[str],
    include_external: bool,
    *,
    conditional_rule: bool,
    cwd: Optional[str | Path] = None,
    project_root: Optional[str | Path] = None,
    config_home: Optional[str | Path] = None,
    managed_dir: Optional[str | Path] = None,
    priority: int = 0,
    visited_dirs: Optional[set[str]] = None,
) -> list[MemoryFileInfo]:
    """Process ``.openspace/rules/**/*.md`` files.

    ``conditional_rule=False`` is the eager 15.1 path and keeps only files
    without frontmatter ``paths``.  Conditional matching is implemented for
    later 15.8 nested-memory callers.
    """

    current_dir = Path(cwd or os.getcwd()).expanduser().resolve()
    project_base = Path(project_root).expanduser().resolve() if project_root else current_dir
    visited = visited_dirs if visited_dirs is not None else set()

    try:
        resolved_dir = Path(rules_dir).expanduser().resolve()
    except OSError:
        return []
    key = _normalize_for_comparison(resolved_dir)
    if key in visited:
        return []
    visited.add(key)
    if not resolved_dir.is_dir():
        return []

    result: list[MemoryFileInfo] = []
    try:
        entries = sorted(resolved_dir.iterdir(), key=lambda path: path.name)
    except OSError:
        return []

    for entry in entries:
        try:
            resolved_entry = entry.resolve()
        except OSError:
            continue
        if resolved_entry.is_dir():
            result.extend(
                process_md_rules(
                    resolved_entry,
                    source,
                    processed_paths,
                    include_external,
                    conditional_rule=conditional_rule,
                    cwd=current_dir,
                    project_root=project_base,
                    config_home=config_home,
                    managed_dir=managed_dir,
                    priority=priority,
                    visited_dirs=visited,
                )
            )
            continue
        if not resolved_entry.is_file() or resolved_entry.suffix != ".md":
            continue
        if _is_sensitive_include_path(resolved_entry):
            continue

        files = process_memory_file(
            resolved_entry,
            source,
            processed_paths,
            include_external,
            cwd=current_dir,
            project_root=project_base,
            config_home=config_home,
            managed_dir=managed_dir,
            priority=priority,
        )
        result.extend(
            file
            for file in files
            if bool(file.globs) is conditional_rule
        )
    return result


def _project_walk_dirs(project_root: Path, cwd: Path) -> list[Path]:
    if not _is_relative_to(cwd, project_root):
        return [cwd]
    dirs = [project_root]
    current = project_root
    for part in cwd.relative_to(project_root).parts:
        current = current / part
        dirs.append(current)
    return dirs


def _truthy_env(name: str) -> bool:
    value = os.environ.get(name, "")
    return value.lower() in {"1", "true", "yes", "on"}


def _env_additional_dirs() -> list[Path]:
    raw = os.environ.get("OPENSPACE_ADDITIONAL_DIRECTORIES", "")
    if not raw:
        return []
    return [Path(part).expanduser().resolve() for part in raw.split(os.pathsep) if part]


def get_memory_files(
    *,
    cwd: Optional[str | Path] = None,
    project_root: Optional[str | Path] = None,
    config_home: Optional[str | Path] = None,
    managed_dir: Optional[str | Path] = None,
    force_include_external: bool = False,
    include_external: bool = False,
    additional_directories: Optional[Iterable[str | Path]] = None,
    use_cache: bool = True,
    load_managed: bool = True,
    load_user: bool = True,
    load_project: bool = True,
    load_local: bool = True,
    load_auto_memory: bool = True,
) -> list[MemoryFileInfo]:
    """Aggregate instruction files and the auto-memory entrypoint."""

    current_dir = Path(cwd or os.getcwd()).expanduser().resolve()
    project_base = (
        Path(project_root).expanduser().resolve()
        if project_root is not None
        else find_project_root(current_dir)
    )
    managed_root = Path(managed_dir or "/etc/openspace").expanduser().resolve()
    from .paths import get_openspace_config_home_dir

    config_root = get_openspace_config_home_dir(config_home)
    extra_dirs = [
        Path(path).expanduser().resolve()
        for path in (additional_directories or [])
    ]
    if _truthy_env("OPENSPACE_ADDITIONAL_DIRECTORIES_OPENSPACE_MD"):
        extra_dirs.extend(_env_additional_dirs())

    auto_memory_enabled = False
    auto_memory_entrypoint: Optional[Path] = None
    if load_auto_memory:
        from .memdir import is_auto_memory_enabled

        auto_memory_enabled = is_auto_memory_enabled()
        if auto_memory_enabled:
            auto_memory_entrypoint = get_memory_path(
                "AutoMem",
                cwd=current_dir,
                project_root=project_base,
                config_home=config_root,
            )

    cache_key = (
        str(current_dir),
        str(project_base),
        str(config_root),
        str(managed_root),
        force_include_external,
        include_external,
        tuple(str(path) for path in extra_dirs),
        auto_memory_enabled,
        str(auto_memory_entrypoint) if auto_memory_entrypoint else None,
        load_managed,
        load_user,
        load_project,
        load_local,
        load_auto_memory,
    )
    if use_cache and cache_key in _MEMORY_FILES_CACHE:
        return list(_MEMORY_FILES_CACHE[cache_key])

    files: list[MemoryFileInfo] = []
    processed_paths: set[str] = set()
    allow_project_external = force_include_external or include_external
    priority = 0

    def add_file(
        path: Optional[Path],
        source: MemorySource,
        include_ext: bool,
        *,
        root: Optional[Path] = None,
    ) -> None:
        nonlocal priority
        if path is None:
            return
        loaded = process_memory_file(
            path,
            source,
            processed_paths,
            include_ext,
            cwd=current_dir,
            project_root=root or project_base,
            config_home=config_root,
            managed_dir=managed_root,
            priority=priority,
        )
        files.extend(loaded)
        if loaded:
            priority += 1

    def add_rules(
        path: Path,
        source: MemorySource,
        include_ext: bool,
        *,
        root: Optional[Path] = None,
    ) -> None:
        nonlocal priority
        loaded = process_md_rules(
            path,
            source,
            processed_paths,
            include_ext,
            conditional_rule=False,
            cwd=current_dir,
            project_root=root or project_base,
            config_home=config_root,
            managed_dir=managed_root,
            priority=priority,
        )
        files.extend(loaded)
        if loaded:
            priority += 1

    if load_managed:
        add_file(
            get_memory_path("Managed", cwd=current_dir, managed_dir=managed_root),
            "Managed",
            allow_project_external,
        )
        add_rules(
            get_managed_openspace_rules_dir(managed_dir=managed_root),
            "Managed",
            allow_project_external,
        )

    if load_user:
        add_file(
            get_memory_path("User", cwd=current_dir, config_home=config_root),
            "User",
            True,
        )
        add_rules(
            get_user_openspace_rules_dir(config_home=config_root),
            "User",
            True,
        )

    if load_project or load_local:
        for directory in _project_walk_dirs(project_base, current_dir):
            if load_project:
                add_file(directory / OPENSPACE_MD, "Project", allow_project_external)
                add_file(
                    directory / OPENSPACE_DIR / OPENSPACE_MD,
                    "Project",
                    allow_project_external,
                )
                add_rules(
                    directory / OPENSPACE_DIR / RULES_DIR,
                    "Project",
                    allow_project_external,
                )
            if load_local:
                add_file(directory / OPENSPACE_LOCAL_MD, "Local", allow_project_external)

    for directory in extra_dirs:
        add_file(
            directory / OPENSPACE_MD,
            "Project",
            allow_project_external,
            root=directory,
        )
        add_file(
            directory / OPENSPACE_DIR / OPENSPACE_MD,
            "Project",
            allow_project_external,
            root=directory,
        )
        add_rules(
            directory / OPENSPACE_DIR / RULES_DIR,
            "Project",
            allow_project_external,
            root=directory,
        )

    if load_auto_memory and auto_memory_enabled:
        add_file(auto_memory_entrypoint, "AutoMem", False)

    if use_cache:
        _MEMORY_FILES_CACHE[cache_key] = list(files)
    return files


def get_large_memory_files(files: Iterable[MemoryFileInfo]) -> list[MemoryFileInfo]:
    return [file for file in files if len(file.content) > MAX_MEMORY_CHARACTER_COUNT]


def filter_injected_memory_files(
    files: Iterable[MemoryFileInfo],
    *,
    skip_auto_memory_index: bool = False,
) -> list[MemoryFileInfo]:
    """OpenSpace ``filterInjectedMemoryFiles`` without GrowthBook coupling."""

    result = list(files)
    if not skip_auto_memory_index:
        return result
    return [file for file in result if file.source != "AutoMem"]


def get_openspace_mds(
    memory_files: Iterable[MemoryFileInfo],
    filter: Optional[Callable[[MemorySource], bool]] = None,
) -> str:
    """OpenSpace ``getClaudeMds`` equivalent for OpenSpace files."""

    memories: list[str] = []
    for file in memory_files:
        if filter is not None and not filter(file.source):
            continue
        content = file.content.strip()
        if not content:
            continue
        description = _description_for_source(file.source)
        memories.append(f"Contents of {file.path}{description}:\n\n{content}")

    if not memories:
        return ""
    return f"{MEMORY_INSTRUCTION_PROMPT}\n\n" + "\n\n".join(memories)


def _description_for_source(source: MemorySource) -> str:
    if source == "Project":
        return " (project instructions, checked into the codebase)"
    if source == "Local":
        return " (user's private project instructions, not checked in)"
    if source == "AutoMem":
        return " (user's auto-memory, persists across conversations)"
    if source == "Managed":
        return " (managed global instructions for all users)"
    return " (user's private global instructions for all projects)"


def is_memory_file_path(file_path: str | Path) -> bool:
    path = Path(file_path)
    if path.name in {OPENSPACE_MD, OPENSPACE_LOCAL_MD, "MEMORY.md"}:
        return True
    return is_rules_file_path(path)


def process_conditioned_md_rules(
    target_path: str | Path,
    rules_dir: str | Path,
    source: MemorySource,
    processed_paths: set[str],
    include_external: bool,
    *,
    cwd: Optional[str | Path] = None,
    project_root: Optional[str | Path] = None,
    config_home: Optional[str | Path] = None,
    managed_dir: Optional[str | Path] = None,
) -> list[MemoryFileInfo]:
    """15.8-ready conditional rules branch from OpenSpace.

    15.1 eager loading calls ``process_md_rules(..., conditional_rule=False)``.
    This helper preserves the target-path return structure for nested-memory
    follow-up without injecting it into the agent loop yet.
    """

    files = process_md_rules(
        rules_dir,
        source,
        processed_paths,
        include_external,
        conditional_rule=True,
        cwd=cwd,
        project_root=project_root,
        config_home=config_home,
        managed_dir=managed_dir,
    )
    target = Path(target_path)
    base_dir = (
        Path(rules_dir).expanduser().resolve().parents[1]
        if source == "Project"
        else Path(cwd or os.getcwd()).expanduser().resolve()
    )
    try:
        relative_target = target.resolve().relative_to(base_dir)
    except (OSError, ValueError):
        return []
    posix_target = relative_target.as_posix()
    return [
        file
        for file in files
        if file.globs and any(fnmatch.fnmatch(posix_target, pattern) for pattern in file.globs)
    ]


Attachment = dict[str, Any]


def get_directories_to_process(
    target_path: str | Path,
    original_cwd: str | Path,
) -> tuple[list[Path], list[Path]]:
    """OpenSpace ``getDirectoriesToProcess`` with OpenSpace project-root scoping.

    Returns ``(nested_dirs, cwd_level_dirs)``:
    - ``nested_dirs`` are directories between ``original_cwd`` and the target
      file, ordered from parent to child.
    - ``cwd_level_dirs`` are project-root → ``original_cwd`` directories used
      for conditional rules only.  OpenSpace walks filesystem root → cwd; OpenSpace
      intentionally scopes project memory to the project root from DEC-025.
    """

    cwd = Path(original_cwd).expanduser().resolve()
    target_dir = Path(target_path).expanduser().resolve().parent

    nested_dirs: list[Path] = []
    current = target_dir
    while current != cwd and current != current.parent:
        if _is_relative_to(current, cwd):
            nested_dirs.append(current)
        current = current.parent
    nested_dirs.reverse()

    project_root = find_project_root(cwd)
    cwd_level_dirs = _project_walk_dirs(project_root, cwd)
    return nested_dirs, cwd_level_dirs


def get_managed_and_user_conditional_rules(
    target_path: str | Path,
    processed_paths: set[str],
    *,
    cwd: Optional[str | Path] = None,
    config_home: Optional[str | Path] = None,
    managed_dir: Optional[str | Path] = None,
) -> list[MemoryFileInfo]:
    """OpenSpace ``getManagedAndUserConditionalRules`` for OPENSPACE rules."""

    current_dir = Path(cwd or os.getcwd()).expanduser().resolve()
    result: list[MemoryFileInfo] = []
    result.extend(
        process_conditioned_md_rules(
            target_path,
            get_managed_openspace_rules_dir(managed_dir=managed_dir),
            "Managed",
            processed_paths,
            False,
            cwd=current_dir,
            config_home=config_home,
            managed_dir=managed_dir,
        )
    )
    result.extend(
        process_conditioned_md_rules(
            target_path,
            get_user_openspace_rules_dir(config_home=config_home),
            "User",
            processed_paths,
            True,
            cwd=current_dir,
            config_home=config_home,
            managed_dir=managed_dir,
        )
    )
    return result


def get_memory_files_for_nested_directory(
    directory: str | Path,
    target_path: str | Path,
    processed_paths: set[str],
    *,
    cwd: Optional[str | Path] = None,
    project_root: Optional[str | Path] = None,
    config_home: Optional[str | Path] = None,
    managed_dir: Optional[str | Path] = None,
) -> list[MemoryFileInfo]:
    """OpenSpace ``getMemoryFilesForNestedDirectory`` for OPENSPACE.md.

    Loads, in order, ``OPENSPACE.md``, ``.openspace/OPENSPACE.md``,
    ``OPENSPACE.local.md``, unconditional ``.openspace/rules/**/*.md``, then
    conditional rules matching ``target_path``.
    """

    current_dir = Path(cwd or os.getcwd()).expanduser().resolve()
    project_base = (
        Path(project_root).expanduser().resolve()
        if project_root is not None
        else find_project_root(current_dir)
    )
    nested_dir = Path(directory).expanduser().resolve()
    result: list[MemoryFileInfo] = []

    result.extend(
        process_memory_file(
            nested_dir / OPENSPACE_MD,
            "Project",
            processed_paths,
            False,
            cwd=current_dir,
            project_root=project_base,
            config_home=config_home,
            managed_dir=managed_dir,
        )
    )
    result.extend(
        process_memory_file(
            nested_dir / OPENSPACE_DIR / OPENSPACE_MD,
            "Project",
            processed_paths,
            False,
            cwd=current_dir,
            project_root=project_base,
            config_home=config_home,
            managed_dir=managed_dir,
        )
    )
    result.extend(
        process_memory_file(
            nested_dir / OPENSPACE_LOCAL_MD,
            "Local",
            processed_paths,
            False,
            cwd=nested_dir,
            project_root=project_base,
            config_home=config_home,
            managed_dir=managed_dir,
        )
    )

    rules_dir = nested_dir / OPENSPACE_DIR / RULES_DIR
    unconditional_processed_paths = set(processed_paths)
    result.extend(
        process_md_rules(
            rules_dir,
            "Project",
            unconditional_processed_paths,
            False,
            conditional_rule=False,
            cwd=current_dir,
            project_root=project_base,
            config_home=config_home,
            managed_dir=managed_dir,
        )
    )
    result.extend(
        process_conditioned_md_rules(
            target_path,
            rules_dir,
            "Project",
            processed_paths,
            False,
            cwd=current_dir,
            project_root=project_base,
            config_home=config_home,
            managed_dir=managed_dir,
        )
    )
    processed_paths.update(unconditional_processed_paths)
    return result


def get_conditional_rules_for_cwd_level_directory(
    directory: str | Path,
    target_path: str | Path,
    processed_paths: set[str],
    *,
    cwd: Optional[str | Path] = None,
    project_root: Optional[str | Path] = None,
    config_home: Optional[str | Path] = None,
    managed_dir: Optional[str | Path] = None,
) -> list[MemoryFileInfo]:
    """OpenSpace ``getConditionalRulesForCwdLevelDirectory`` for OPENSPACE rules."""

    current_dir = Path(cwd or os.getcwd()).expanduser().resolve()
    project_base = (
        Path(project_root).expanduser().resolve()
        if project_root is not None
        else find_project_root(current_dir)
    )
    return process_conditioned_md_rules(
        target_path,
        Path(directory).expanduser().resolve() / OPENSPACE_DIR / RULES_DIR,
        "Project",
        processed_paths,
        False,
        cwd=current_dir,
        project_root=project_base,
        config_home=config_home,
        managed_dir=managed_dir,
    )


def memory_files_to_attachments(
    memory_files: Iterable[MemoryFileInfo],
    tool_use_context: Any,
    trigger_file_path: str | Path | None = None,
    *,
    ignore_read_file_state: bool = False,
) -> list[Attachment]:
    """Convert OPENSPACE.md files to OpenSpace ``nested_memory`` attachments."""

    attachments: list[Attachment] = []
    read_file_state = getattr(tool_use_context, "read_file_state", None)
    loaded_paths = getattr(tool_use_context, "loaded_nested_memory_paths", None)
    if not isinstance(read_file_state, dict):
        read_file_state = {}
    if not isinstance(loaded_paths, set):
        loaded_paths = set()
        try:
            tool_use_context.loaded_nested_memory_paths = loaded_paths
        except Exception:
            pass

    cwd = Path(getattr(tool_use_context, "cwd", os.getcwd())).expanduser().resolve()
    for memory_file in memory_files:
        path = memory_file.path.expanduser().resolve()
        path_key = str(path)
        if path_key in loaded_paths:
            continue
        if not ignore_read_file_state and path_key in read_file_state:
            continue

        attachments.append(
            {
                "type": "nested_memory",
                "path": path_key,
                "content": _memory_file_info_payload(memory_file),
                "displayPath": _display_path(path, cwd),
                **(
                    {"triggerFilePath": str(Path(trigger_file_path).expanduser().resolve())}
                    if trigger_file_path is not None
                    else {}
                ),
            }
        )
        loaded_paths.add(path_key)
        _mark_memory_file_read(tool_use_context, memory_file)

    return attachments


def get_nested_memory_attachments_for_file(
    file_path: str | Path,
    tool_use_context: Any,
    *,
    ignore_read_file_state: bool = False,
) -> list[Attachment]:
    """Discover nested OPENSPACE.md/rules for one FileRead target path."""

    attachments: list[Attachment] = []
    try:
        target = Path(file_path).expanduser().resolve()
        if not _path_allowed_for_nested_memory(target, tool_use_context):
            return attachments

        cwd = Path(getattr(tool_use_context, "cwd", os.getcwd())).expanduser().resolve()
        project_root = find_project_root(cwd)
        processed_paths: set[str] = set()

        attachments.extend(
            memory_files_to_attachments(
                get_managed_and_user_conditional_rules(
                    target,
                    processed_paths,
                    cwd=cwd,
                ),
                tool_use_context,
                target,
                ignore_read_file_state=ignore_read_file_state,
            )
        )

        nested_dirs, cwd_level_dirs = get_directories_to_process(target, cwd)
        for directory in nested_dirs:
            attachments.extend(
                memory_files_to_attachments(
                    get_memory_files_for_nested_directory(
                        directory,
                        target,
                        processed_paths,
                        cwd=cwd,
                        project_root=project_root,
                    ),
                    tool_use_context,
                    target,
                    ignore_read_file_state=ignore_read_file_state,
                )
            )

        for directory in cwd_level_dirs:
            attachments.extend(
                memory_files_to_attachments(
                    get_conditional_rules_for_cwd_level_directory(
                        directory,
                        target,
                        processed_paths,
                        cwd=cwd,
                        project_root=project_root,
                    ),
                    tool_use_context,
                    target,
                    ignore_read_file_state=ignore_read_file_state,
                )
            )
    except Exception:
        return attachments
    return attachments


async def consume_nested_memory_triggers(tool_use_context: Any) -> list[Attachment]:
    """Consume and clear OpenSpace ``nestedMemoryAttachmentTriggers``."""

    triggers = getattr(tool_use_context, "nested_memory_triggers", None)
    if not isinstance(triggers, set) or not triggers:
        return []

    attachments: list[Attachment] = []
    try:
        for file_path in list(triggers):
            attachments.extend(
                get_nested_memory_attachments_for_file(
                    file_path,
                    tool_use_context,
                )
            )
    finally:
        triggers.clear()
    return attachments


def get_post_compact_nested_memory_attachments(tool_use_context: Any) -> list[Attachment]:
    """Rebuild nested memory after compact clears prior attachment messages."""

    loaded = getattr(tool_use_context, "loaded_nested_memory_paths", None)
    if isinstance(loaded, set):
        loaded.clear()

    read_file_state = getattr(tool_use_context, "read_file_state", None)
    if not isinstance(read_file_state, dict):
        return []

    attachments: list[Attachment] = []
    for path in list(read_file_state.keys()):
        if is_memory_file_path(path):
            continue
        attachments.extend(
            get_nested_memory_attachments_for_file(
                path,
                tool_use_context,
                ignore_read_file_state=True,
            )
        )
    return attachments


def _memory_file_info_payload(memory_file: MemoryFileInfo) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "path": str(memory_file.path),
        "content": memory_file.content,
        "source": memory_file.source,
        "type": memory_file.source,
        "priority": memory_file.priority,
    }
    if memory_file.parent is not None:
        payload["parent"] = str(memory_file.parent)
    if memory_file.globs:
        payload["globs"] = list(memory_file.globs)
    if memory_file.content_differs_from_disk:
        payload["contentDiffersFromDisk"] = True
    return payload


def _mark_memory_file_read(tool_use_context: Any, memory_file: MemoryFileInfo) -> None:
    read_file_state = getattr(tool_use_context, "read_file_state", None)
    if not isinstance(read_file_state, dict):
        return

    from openspace.services.tooling.context import ReadFileEntry

    read_file_state[str(memory_file.path.expanduser().resolve())] = ReadFileEntry(
        content=(
            memory_file.raw_content
            if memory_file.content_differs_from_disk and memory_file.raw_content is not None
            else memory_file.content
        ),
        timestamp=time.time_ns(),
        offset=None,
        limit=None,
        is_partial_view=memory_file.content_differs_from_disk,
    )


def _path_allowed_for_nested_memory(path: Path, tool_use_context: Any) -> bool:
    permission_context = getattr(tool_use_context, "permission_context", None)
    if permission_context is not None and hasattr(
        permission_context, "additional_working_directories"
    ):
        try:
            from openspace.grounding.core.permissions.filesystem import (
                path_in_allowed_working_path,
            )

            return path_in_allowed_working_path(str(path), permission_context)
        except Exception:
            return False

    cwd = Path(getattr(tool_use_context, "cwd", os.getcwd())).expanduser().resolve()
    return _is_relative_to(path, cwd)


def _display_path(path: Path, cwd: Path) -> str:
    try:
        return str(path.relative_to(cwd))
    except ValueError:
        return str(path)
