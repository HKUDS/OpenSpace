"""Read-only test command suggestions, not a formal environment readiness check."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import sys

_IGNORE = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    ".nox", "target", "dist", "build", ".next", "coverage", ".agentpilot",
})
_TEST_NAME = re.compile(r"(?:^test_.*\.py$|.*_test\.py$)")
_UNITTEST = re.compile(r"\b(?:unittest\.TestCase|from\s+unittest\s+import\s+[^\n]*TestCase)\b")
_MAX_PATHS = 50_000
_MAX_READ = 2_097_152
_MAX_TOTAL_READ = 16_777_216


def _root(repo: Path) -> Path:
    root = Path(repo).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("project must be a directory")
    return root


def _files(root: Path) -> list[str]:
    paths: list[str] = []
    visited = 0

    def fail(error: OSError) -> None:
        raise error

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=fail):
        visited += len(dirs) + len(files)
        if visited > _MAX_PATHS:
            raise ValueError("project inspection exceeded 50000 paths; inspect a narrower project directory")
        dirs[:] = sorted(d for d in dirs if d not in _IGNORE and not (Path(directory) / d).is_symlink())
        paths.extend((Path(directory) / f).relative_to(root).as_posix() for f in files
                     if not (Path(directory) / f).is_symlink() and (Path(directory) / f).is_file())
    return sorted(paths)


class _Reader:
    def __init__(self, root: Path, paths: list[str]) -> None:
        self.root = root
        self.paths = set(paths)
        self.cache: dict[Path, str] = {}
        self.total = 0

    def text(self, path: Path) -> str:
        if path in self.cache:
            return self.cache[path]
        if path.relative_to(self.root).as_posix() not in self.paths:
            return ""
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_READ:
            return ""
        with path.open("rb") as stream:
            content = stream.read(_MAX_READ + 1)
        self.total += len(content)
        if self.total > _MAX_TOTAL_READ:
            raise ValueError("project inspection exceeded 16 MiB of test/configuration text; inspect a narrower project directory")
        result = content.decode("utf-8", errors="replace") if len(content) <= _MAX_READ else ""
        self.cache[path] = result
        return result


def _python(root: Path) -> tuple[str, str]:
    layouts = ("Scripts/python.exe", "python.exe") if os.name == "nt" else ("bin/python", "bin/python3")
    for directory in (".venv", "venv"):
        for layout in layouts:
            relative = directory + "/" + layout
            if (root / relative).is_file() and os.access(root / relative, os.X_OK):
                return "./" + relative, "project:" + directory
    for variable in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        value = os.environ.get(variable)
        if not value:
            continue
        prefix = Path(value).expanduser()
        if not prefix.is_absolute():
            continue
        for layout in layouts:
            executable = prefix / layout
            if executable.is_file() and os.access(executable, os.X_OK):
                # Do not resolve venv symlinks: their invoked path determines
                # which site's packages Python uses, even with a shared binary.
                return str(executable), variable
    runtime_bin = os.path.normcase(os.path.abspath(Path(sys.executable).parent))
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        directory = Path(entry or ".").expanduser()
        if not directory.is_absolute():
            directory = root / directory
        if os.path.normcase(os.path.abspath(directory)) == runtime_bin:
            continue
        for name in ("python", "python3"):
            executable = shutil.which(name, path=str(directory))
            if executable:
                return os.path.abspath(executable), "PATH"
    return sys.executable, "runtime_fallback"


def _spec(kind: str, argv: list[str], evidence: list[str]) -> dict:
    return {
        "id": kind, "kind": kind, "argv": argv, "evidence": evidence,
    }


def discover_evaluators(repo: Path) -> list[dict]:
    """Suggest existing test commands without executing or installing anything."""
    root = _root(repo)
    paths = _files(root)
    reader = _Reader(root, paths)
    _text = reader.text
    results = []
    pytests = [p for p in paths if _TEST_NAME.match(Path(p).name) and p.endswith(".py")]
    pytest_config = []
    if "pytest.ini" in reader.paths:
        pytest_config.append("pytest.ini")
    if "[tool.pytest.ini_options]" in _text(root / "pyproject.toml"):
        pytest_config.append("pyproject.toml")
    if "[tool:pytest]" in _text(root / "setup.cfg"):
        pytest_config.append("setup.cfg")
    pytest_usage = [p for p in pytests if re.search(r"\b(?:import\s+pytest|from\s+pytest\s+import)\b", _text(root / p))]
    unittest_files = [p for p in pytests if _UNITTEST.search(_text(root / p))]
    # A test_*.py file with assert-style tests is meaningful pytest evidence;
    # a manifest by itself is not a reason to claim a suite exists.
    plain_tests = [p for p in pytests if p not in unittest_files and re.search(r"(?m)^\s*(?:async\s+)?def\s+test_", _text(root / p))]
    python, python_source = _python(root)
    if pytests and (pytest_config or pytest_usage or plain_tests):
        candidate = _spec("pytest", [python, "-m", "pytest", "-q"], sorted(set(pytest_config + pytest_usage + plain_tests)))
        candidate["python_env_source"] = python_source
        results.append(candidate)
    if unittest_files:
        starts = sorted({p.split("/")[0] if "/" in p else "." for p in unittest_files})
        for start in starts:
            candidate = _spec("unittest", [python, "-m", "unittest", "discover", "-s", start], [p for p in unittest_files if start == "." or p.startswith(start + "/")])
            candidate["id"] = "unittest:" + start
            candidate["python_env_source"] = python_source
            results.append(candidate)

    package = _text(root / "package.json")
    if package:
        try:
            scripts = json.loads(package).get("scripts", {})
        except (ValueError, AttributeError):
            scripts = {}
        if isinstance(scripts, dict):
            for name in ("test", "test:unit"):
                script = scripts.get(name)
                if not isinstance(script, str) or not script.strip():
                    continue
                hooks = "\n".join(value for key in ("pre" + name, "post" + name) if isinstance(value := scripts.get(key), str))
                if re.search(r"\b(?:npm|pnpm|yarn|bun)\s+(?:install|add)\b|\bnpx\b", hooks, re.I):
                    continue
                if re.search(r"no test specified|(?:^|\s)--watch(?:\s|=|$)|\bwatchAll\b|\b(?:npm|pnpm|yarn|bun)\s+(?:install|add)\b|\bnpx\b", script, re.I):
                    continue
                if re.fullmatch(r"\s*(?:true|:|exit\s+0|echo\b[^;&|]*)\s*", script):
                    continue
                manager = "npm"
                for filename, executable in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lock", "bun"), ("bun.lockb", "bun")):
                    if filename in reader.paths:
                        manager = executable
                        break
                candidate = _spec("node", [manager, "run", name], ["package.json#scripts." + name])
                candidate["id"] = "node:" + name
                results.append(candidate)

    if "Cargo.toml" in reader.paths:
        rust_tests = [p for p in paths if p.endswith(".rs") and (p.startswith("tests/") or re.search(r"#\[(?:\w+::)?test(?:\([^]]*\))?\]", _text(root / p)))]
        if rust_tests:
            results.append(_spec("cargo", ["cargo", "test", "--offline", "--locked"], ["Cargo.toml", *rust_tests]))
    go_tests = [p for p in paths if p.endswith("_test.go")]
    if "go.mod" in reader.paths and go_tests:
        results.append(_spec("go", ["go", "test", "-json", "-count=1", "-mod=readonly", "./..."], ["go.mod", *go_tests]))
    return results


def inspect_project(repo: Path) -> dict:
    root = _root(repo)
    return {
        "project": str(root), "evaluators": discover_evaluators(root),
        "execution_performed": False, "dependencies_installed": False,
        "note": "These are suggestions from bounded file inspection, not executed checks or a formal AgentPilot configuration. Read the project instructions; inspect subprojects separately when needed.",
    }
