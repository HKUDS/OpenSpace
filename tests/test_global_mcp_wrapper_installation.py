from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "scripts" / "install-global-codex-openspace"


def _install_wrappers(tmp_path: Path) -> tuple[Path, Path]:
    codex_home = tmp_path / ".codex"
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    subprocess.run(
        ["bash", str(INSTALLER)],
        check=True,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    return codex_home / "bin" / "openspace-global-mcp", codex_home / "bin" / "openspace-evolution-global-mcp"


def _make_stub_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo_root = tmp_path / "stub-repo"
    python_path = repo_root / ".venv" / "bin" / "python"
    capture_path = tmp_path / "capture.txt"
    python_path.parent.mkdir(parents=True)
    python_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"path = {str(capture_path)!r}\n"
        "payload = {\n"
        "  'argv': sys.argv,\n"
        "  'OPENSPACE_WORKSPACE': os.environ.get('OPENSPACE_WORKSPACE'),\n"
        "  'OPENSPACE_MCP_PROXY_MODE': os.environ.get('OPENSPACE_MCP_PROXY_MODE'),\n"
        "  'OPENSPACE_HOST_SKILL_DIRS': os.environ.get('OPENSPACE_HOST_SKILL_DIRS'),\n"
        "}\n"
        "with open(path, 'w', encoding='utf-8') as fh:\n"
        "    json.dump(payload, fh)\n",
        encoding="utf-8",
    )
    python_path.chmod(0o755)
    return repo_root, capture_path


def _rewrite_wrapper_repo_root(wrapper_path: Path, repo_root: Path) -> None:
    text = wrapper_path.read_text(encoding="utf-8")
    rewritten = text.replace(f'REPO_ROOT="{REPO_ROOT}"', f'REPO_ROOT="{repo_root}"')
    wrapper_path.write_text(rewritten, encoding="utf-8")


def test_generated_wrapper_uses_direct_mode_when_workspace_is_invalid(tmp_path: Path) -> None:
    main_wrapper, _ = _install_wrappers(tmp_path)
    stub_repo, capture_path = _make_stub_repo(tmp_path)
    _rewrite_wrapper_repo_root(main_wrapper, stub_repo)

    env = os.environ.copy()
    env.pop("OPENSPACE_WORKSPACE", None)
    proc = subprocess.run(
        [str(main_wrapper)],
        check=True,
        cwd="/",
        env=env,
        capture_output=True,
        text=True,
    )

    payload = __import__("json").loads(capture_path.read_text(encoding="utf-8"))
    assert payload["OPENSPACE_MCP_PROXY_MODE"] == "direct"
    assert payload["OPENSPACE_WORKSPACE"] in (None, "")
    assert payload["OPENSPACE_HOST_SKILL_DIRS"] == (
        f"{Path.home() / '.codex' / 'projects' / 'default' / 'skills'},{Path.home() / '.codex' / 'skills'}"
    )
    assert "Codex Desktop did not provide a usable workspace" in proc.stderr


def test_generated_wrapper_keeps_daemon_mode_for_valid_workspace(tmp_path: Path) -> None:
    _, evolution_wrapper = _install_wrappers(tmp_path)
    stub_repo, capture_path = _make_stub_repo(tmp_path)
    nested = stub_repo / "nested"
    nested.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=stub_repo, check=True, capture_output=True, text=True)
    _rewrite_wrapper_repo_root(evolution_wrapper, stub_repo)

    env = os.environ.copy()
    env.pop("OPENSPACE_WORKSPACE", None)
    subprocess.run(
        [str(evolution_wrapper)],
        check=True,
        cwd=nested,
        env=env,
        capture_output=True,
        text=True,
    )

    payload = __import__("json").loads(capture_path.read_text(encoding="utf-8"))
    assert payload["OPENSPACE_MCP_PROXY_MODE"] == "daemon"
    assert payload["OPENSPACE_WORKSPACE"] == str(stub_repo.resolve())
