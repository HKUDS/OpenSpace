"""Install host skills without changing model, authentication, or host settings.

Host directory contracts live in hosts.py. WorkBuddy documents local package
import but no stable directory contract, so it requires an explicit destination
or a ZIP export. A skill install is not a plugin install.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import sys
import tempfile
from typing import Iterable
import zipfile

from .hosts import SUPPORTED_HOSTS, default_skill_dir, host_details

COMPONENTS = ("agentpilot", "openspace", "rsi-arena")
_SKILLS_ROOT = Path(__file__).with_name("skills")
_MANIFEST = ".agentpilot-install.json"
_SCHEMA = "agentpilot.host-skill-install.v1"
_COMPONENT_INFO = {
    "agentpilot": ("AgentPilot 编码改进", "AgentPilot coding improvement",
                  "由当前代理补齐任务与评估，就绪后交给 AgentPilot 运行正式闭环。",
                  "Prepare missing task/evaluation inputs, then hand a ready environment to the AgentPilot runner."),
    "openspace": ("OpenSpace 工作空间", "OpenSpace workspaces",
                  "配置 OpenSpace 工作空间以保存和复用技能与经验。",
                  "Set up OpenSpace workspaces to retain and reuse skills and experience."),
    "rsi-arena": ("RSI Arena 评估", "RSI Arena evaluation",
                  "配置 RSI Arena 的任务、方法及评估流程。",
                  "Configure RSI Arena tasks, methods, and evaluation workflows."),
}


class SetupConflictError(ValueError):
    """An installation would overwrite files not owned by this installer."""


def _components(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, str):
        values = (values,)
    result = tuple(dict.fromkeys(values))
    if not result or any(value not in COMPONENTS for value in result):
        raise ValueError(f"Choose one or more components from: {', '.join(COMPONENTS)}")
    return result


def launcher_argv() -> list[str]:
    # Resolving symlinks would turn a venv Python into its base interpreter and
    # lose the installed package. Make it absolute while preserving the venv.
    return [os.path.abspath(sys.executable), "-m", "agentpilot.app"]


def _launcher_command(argv: list[str]) -> str:
    if os.name == "nt":
        return "& " + " ".join("'" + part.replace("'", "''") + "'" for part in argv)
    return shlex.join(argv)


def _host_details(host: str, components: tuple[str, ...]) -> dict:
    return host_details(host, components)


def _workbuddy_metadata(text: str, component: str) -> str:
    """Include WorkBuddy's documented marketplace metadata in its packages."""
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", text, re.S)
    if match is None:
        raise ValueError(f"Bundled {component} skill is missing YAML frontmatter")
    header = match.group(1)
    present = set(re.findall(r"^([A-Za-z_][A-Za-z0-9_-]*):", header, re.M))
    if not {"name", "description"} <= present:
        raise ValueError(f"Bundled {component} skill needs name and description")
    chinese, english, description_zh, description_en = _COMPONENT_INFO[component]
    additions = {
        "display_name": chinese,
        "display_name_en": english,
        "description_zh": description_zh,
        "description_en": description_en,
        "version": "1.0.0",
        "author": "AgentPilot Team@HKUDS",
    }
    lines = [f"{key}: {json.dumps(value, ensure_ascii=False)}"
             for key, value in additions.items() if key not in present]
    return "---\n" + header + ("\n" + "\n".join(lines) if lines else "") + "\n---\n" + text[match.end():]


def _skill_files(component: str, *, host: str, argv: list[str]) -> dict[str, bytes]:
    source = _SKILLS_ROOT / component
    if not (source / "SKILL.md").is_file():
        raise ValueError(f"The installed package is missing the {component} skill")
    result = {}
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Bundled skills must not contain symlinks: {path}")
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        relative = path.relative_to(source).as_posix()
        if relative == _MANIFEST:
            continue
        content = path.read_bytes()
        if path.suffix == ".md":
            text = content.decode("utf-8").replace("{{AGENTPILOT_COMMAND}}", _launcher_command(argv))
            if relative == "SKILL.md" and host == "workbuddy":
                text = _workbuddy_metadata(text, component)
            content = text.encode("utf-8")
        result[relative] = content
    return result


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _relative_file(root: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    if relative.is_absolute() or not relative.parts or any(part in {".", ".."} for part in relative.parts) or "\\" in name:
        raise SetupConflictError("Invalid managed file path in installation manifest")
    target = root.joinpath(*relative.parts)
    current = target
    while current != root:
        if current.is_symlink():
            raise SetupConflictError(f"Refusing to overwrite a symlink: {current}")
        current = current.parent
    return target


def _plan(root: Path, files: dict[str, bytes], identity: str, *, allow_unrelated: bool = False) -> tuple[dict[str, bytes], tuple[Path, ...]]:
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise SetupConflictError(f"Installation destination is not a plain directory: {root}")
    manifest_path = root / _MANIFEST
    prior = {}
    if manifest_path.is_symlink():
        raise SetupConflictError(f"Refusing to overwrite a symlink: {manifest_path}")
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise SetupConflictError(f"Cannot read installation manifest: {manifest_path}") from error
        if not isinstance(manifest, dict) or manifest.get("schema") != _SCHEMA or manifest.get("identity") != identity or not isinstance(manifest.get("files"), dict):
            raise SetupConflictError(f"Destination belongs to another installation: {root}")
        prior = manifest["files"]
    elif root.exists() and any(root.iterdir()) and not allow_unrelated:
        raise SetupConflictError(f"Existing skill is not managed by AgentPilot; use another --skill-dir: {root}")

    for name, digest in prior.items():
        if not isinstance(name, str) or not isinstance(digest, str):
            raise SetupConflictError(f"Invalid installation manifest: {manifest_path}")
        target = _relative_file(root, name)
        if target.exists() and (not target.is_file() or _digest(target.read_bytes()) != digest):
            raise SetupConflictError(f"Preserving locally modified managed file: {target}")
    for name in files:
        target = _relative_file(root, name)
        if target.exists() and name not in prior:
            raise SetupConflictError(f"Preserving an existing unmanaged file: {target}")

    desired = dict(files)
    desired[_MANIFEST] = (json.dumps({
        "schema": _SCHEMA, "identity": identity,
        "files": {name: _digest(content) for name, content in sorted(files.items())},
    }, sort_keys=True, indent=2) + "\n").encode("utf-8")
    obsolete = tuple(_relative_file(root, name) for name in prior if name not in files)
    return desired, obsolete


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".agentpilot-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _apply(root: Path, plan: tuple[dict[str, bytes], tuple[Path, ...]]) -> str:
    files, obsolete = plan
    existed = (root / _MANIFEST).exists()
    changed = False
    for name, content in files.items():
        path = _relative_file(root, name)
        if not path.is_file() or path.read_bytes() != content:
            _atomic_write(path, content)
            changed = True
    for path in obsolete:
        if path.exists():
            path.unlink()
            changed = True
    return "installed" if not existed else "updated" if changed else "unchanged"


def install_skills(*, host: str, components: Iterable[str] = ("agentpilot",), skill_dir: Path | str | None = None, home: Path | str | None = None, check_only: bool = False) -> dict:
    """Install selected components beneath a host's skills root.

    All destinations are checked before writing any component. User changes to
    an installed skill block an update; unrelated files and host settings stay.
    WorkBuddy and generic hosts require an explicit directory confirmed by that host.
    check_only validates the same plans without creating or changing files.
    """
    selected = _components(components)
    details = _host_details(host, selected)
    if skill_dir is None:
        root = default_skill_dir(host, home=home)
        if root is None:
            if host == "workbuddy":
                raise ValueError("WorkBuddy has no assumed skill directory. Supply --skill-dir with a directory confirmed by WorkBuddy, or use --export-dir and import the ZIP in WorkBuddy's Skills UI.")
            raise ValueError("A generic host requires its documented skills directory through --skill-dir")
    else:
        root = Path(skill_dir).expanduser()
    root = root.absolute()
    argv = launcher_argv()
    plans = [(name, root / name, _plan(root / name, _skill_files(name, host=host, argv=argv), name)) for name in selected]
    installed = [{"component": name, "path": str(path), "status": "checked" if check_only else _apply(path, plan),
                  "invocation": details["invocations"][name]} for name, path, plan in plans]
    return {"host": host, "kind": "local_skills", "skills_root": str(root), "skills": installed,
            "launcher_argv": argv, "plugin_registered": False, "runtime_verified": False, **details}


def export_skills(output: Path | str, *, components: Iterable[str] = ("agentpilot",), check_only: bool = False) -> dict:
    """Export one WorkBuddy-compatible ZIP per skill for local UI import.

    These packages use the current installation's Python launcher. Import them
    on the same machine, or install AgentPilot and export again on the target.
    check_only validates the export plan without writing ZIPs or manifests.
    """
    selected = _components(components)
    argv = launcher_argv()
    files = {}
    for component in selected:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in _skill_files(component, host="workbuddy", argv=argv).items():
                info = zipfile.ZipInfo(name)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, content)
        files[f"{component}.zip"] = buffer.getvalue()
    root = Path(output).expanduser().absolute()
    plan = _plan(root, files, "workbuddy-exports", allow_unrelated=True)
    status = "checked" if check_only else _apply(root, plan)
    return {"host": "workbuddy", "kind": "skill_exports", "status": status,
            "archives": [str(root / name) for name in files], "launcher_argv": argv,
            "plugin_registered": False, "runtime_verified": False,
            "import_note": "Import each ZIP through WorkBuddy's Skills UI on this machine, then select the skill in a conversation. This export does not install or activate it in WorkBuddy.",
            **_host_details("workbuddy", selected)}


__all__ = ["COMPONENTS", "SUPPORTED_HOSTS", "SetupConflictError", "export_skills", "install_skills", "launcher_argv"]
