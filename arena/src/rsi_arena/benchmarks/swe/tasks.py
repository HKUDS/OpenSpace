"""SWE-bench Verified loading and isolated Harbor task derivation.

Input is a local download of the hub dataset ``swe-bench/swe-bench-verified``
(``harbor dataset download swe-bench/swe-bench-verified -o <dir>``): 500 task
directories in the same Harbor format as Terminal-Bench 2.1.  The gold patch
and the test patch live in ``tests/`` and ``solution/`` — delivered to the
container only in the verifier/oracle phase, never visible to the agent.

RSI Arena never amends the downloaded dataset in place.  Instead,
``build_derived_task_tree`` produces a deterministic task tree with:

- an offline agent phase;
- Harbor ``environment_mode = "separate"``;
- a private verifier image which safely restores the trusted bridge artifact
  ``/logs/artifacts/rsi-arena-tree.tar`` into a fresh ``/testbed`` while
  retaining the verifier image's pristine ``.git``; and
- the unmodified upstream SWE test script run only after that restoration.

The bridge artifact is produced by trusted supervisor code after the candidate
worker exits.  This package only declares and consumes the contract; it does
not pretend that a candidate-written archive is trusted.

The transform identity, source content, transform templates and derived content
are all pinned.  A private test/gold change moves the evaluator identity without
being copied into agent-visible ``task.toml``/instruction/environment files.
- clusters are the upstream *repos* (django, sympy, ...) parsed from task
  ids, not the uniform "debugging" category the packaging ships.

The digest is computed over the actual bytes of every task's defining files,
so any drift — upstream re-publish or local edit — changes the bench
identity loudly.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomllib

from rsi_arena.contracts import canonical_digest

_OFFLINE_MARKER = 'network_mode = "no-network"'
DERIVED_TASK_TRANSFORM_IDENTITY = "swe-separate-verifier"
DERIVATION_STAMP = ".rsi-arena-derived.json"
TREE_ARTIFACT_PATH = "/logs/artifacts/rsi-arena-tree.tar"
_RESTORE_SCRIPT_SOURCE = Path(__file__).with_name("restore_tree.py")

_WRAPPER_TEST_SH = """\
#!/bin/bash
set -euo pipefail

# rsi-arena-tree.tar is written by the trusted agent bridge after the
# candidate worker exits.  The restore helper rejects traversal, links,
# special files and size/count overages before changing /testbed.
python3 /tests/rsi_arena_restore_tree.py \
  /logs/artifacts/rsi-arena-tree.tar /testbed
exec bash /tests/upstream-test.sh
"""

_VERIFIER_DOCKERFILE_SUFFIX = """\

# RSI Arena fresh-verifier additions.  The upstream environment Dockerfile is
# retained above so /testbed starts from the official pristine SWE image.
RUN mkdir -p /tests /logs/verifier /logs/artifacts
COPY test.sh /tests/test.sh
COPY upstream-test.sh /tests/upstream-test.sh
COPY config.json /tests/config.json
COPY rsi_arena_restore_tree.py /tests/rsi_arena_restore_tree.py
RUN chmod 0555 /tests/test.sh /tests/upstream-test.sh \
    /tests/rsi_arena_restore_tree.py
"""

_AGENT_DOCKERFILE_SUFFIX = """\

# RSI Arena Terminus-2 runtime dependencies. The derived task is offline
# during agent execution, so both tools are part of the frozen image content.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tmux asciinema \
    && rm -rf /var/lib/apt/lists/* \
    && tmux_path="$(command -v tmux)" \
    && asciinema_path="$(command -v asciinema)" \
    && test -x "$tmux_path" \
    && test -x "$asciinema_path" \
    && "$tmux_path" -V
"""

# Upstream difficulty strings → coarse level labels (kept alongside the raw
# string in tags; the split stratifies on repo × level).
_LEVEL_BY_DIFFICULTY = {
    "<15 min fix": "easy",
    "15 min - 1 hour": "medium",
    "1-4 hours": "hard",
    ">4 hours": "expert",
}


@dataclass(frozen=True)
class DerivedTaskTree:
    root: Path
    transform_identity: str
    source_digest: str
    transform_digest: str
    content_digest: str


def _safe_task_id(task_id: str) -> str:
    path = Path(task_id)
    if (
        not task_id
        or path.is_absolute()
        or len(path.parts) != 1
        or path.parts[0] in {".", ".."}
    ):
        raise ValueError(f"unsafe SWE task id {task_id!r}")
    return task_id


def _copy_regular_tree(source: Path, destination: Path) -> None:
    """Copy a task without following ambient/smuggled filesystem links."""

    destination.mkdir(parents=True)
    for path in sorted(
        source.rglob("*"), key=lambda item: item.relative_to(source).as_posix()
    ):
        rel = path.relative_to(source)
        info = path.lstat()
        target = destination / rel
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"SWE source task contains forbidden symlink: {rel}")
        if stat.S_ISDIR(info.st_mode):
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"SWE source task contains non-regular file: {rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target, follow_symlinks=False)


def _set_top_level_artifacts(text: str) -> str:
    lines = text.splitlines(keepends=True)
    first_section = next(
        (index for index, line in enumerate(lines) if line.lstrip().startswith("[")),
        len(lines),
    )
    matches = [
        index
        for index, line in enumerate(lines[:first_section])
        if re.match(r"^\s*artifacts\s*=", line)
    ]
    replacement = 'artifacts = ["/logs/artifacts"]\n'
    if len(matches) > 1:
        raise ValueError("SWE task has multiple top-level artifacts declarations")
    if matches:
        lines[matches[0]] = replacement
    else:
        lines.insert(first_section, replacement + "\n")
    return "".join(lines)


def _set_section_key(text: str, section: str, key: str, rendered_value: str) -> str:
    lines = text.splitlines(keepends=True)
    header = f"[{section}]"
    starts = [index for index, line in enumerate(lines) if line.strip() == header]
    if len(starts) != 1:
        raise ValueError(f"SWE task must contain exactly one {header} section")
    start = starts[0]
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].lstrip().startswith("[")
        ),
        len(lines),
    )
    matcher = re.compile(rf"^\s*{re.escape(key)}\s*=")
    matches = [index for index in range(start + 1, end) if matcher.match(lines[index])]
    replacement = f'{key} = "{rendered_value}"\n'
    if len(matches) > 1:
        raise ValueError(f"SWE task has multiple {section}.{key} declarations")
    if matches:
        lines[matches[0]] = replacement
    else:
        lines.insert(start + 1, replacement)
    return "".join(lines)


def _render_task_toml(source_text: str) -> str:
    parsed = tomllib.loads(source_text)
    artifacts = parsed.get("artifacts", [])
    if artifacts not in ([], ["/logs/artifacts"]):
        raise ValueError(
            "SWE derivation refuses unexpected upstream artifact declarations"
        )
    if parsed.get("verifier", {}).get("environment") is not None:
        raise ValueError(
            "SWE derivation refuses an upstream custom verifier environment"
        )
    rendered = _set_top_level_artifacts(source_text)
    rendered = _set_section_key(rendered, "agent", "network_mode", "no-network")
    rendered = _set_section_key(rendered, "verifier", "environment_mode", "separate")
    # Parse the output before it becomes a durable task package.
    checked = tomllib.loads(rendered)
    if checked["agent"].get("network_mode") != "no-network":
        raise ValueError("derived SWE agent is not offline")
    if checked["verifier"].get("environment_mode") != "separate":
        raise ValueError("derived SWE verifier is not separate")
    return rendered


_VERIFIER_CONFIG_ALLOWLIST = frozenset(
    {
        "repo",
        "instance_id",
        "base_commit",
        "version",
        "environment_setup_commit",
        "test_patch",
        "FAIL_TO_PASS",
        "PASS_TO_PASS",
    }
)


def _sanitize_verifier_config(config_path: Path) -> None:
    """Remove reference/hint fields before config enters the verifier.

    SWE's grading code needs the test patch and FAIL_TO_PASS/PASS_TO_PASS, but
    ``make_test_spec`` does not consume the reference ``patch`` or maintainer
    ``hints_text``.  Leaving either in ``/tests/config.json`` would let
    candidate code installed or executed by the verifier recover answer-like
    material as a delayed payload, even though the agent phase itself is
    properly sealed.  The allowlist also fails closed if upstream adds a new
    descriptive/gold field later.
    """

    try:
        config = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid SWE verifier config: {config_path}") from exc
    if not isinstance(config, dict):
        raise ValueError(f"SWE verifier config must be a JSON object: {config_path}")
    gold_patch = config.pop("patch", None)
    if not isinstance(gold_patch, str):
        raise ValueError(
            f"SWE verifier config lacks a string reference patch: {config_path}"
        )
    config = {
        key: value for key, value in config.items() if key in _VERIFIER_CONFIG_ALLOWLIST
    }
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )


def _render_one_task(source: Path, destination: Path) -> None:
    _copy_regular_tree(source, destination)
    reserved = (
        destination / "tests" / "upstream-test.sh",
        destination / "tests" / "rsi_arena_restore_tree.py",
    )
    if any(path.exists() for path in reserved):
        raise ValueError(
            "upstream SWE task collides with RSI Arena reserved test files"
        )

    manifest = destination / "task.toml"
    manifest.write_text(_render_task_toml(manifest.read_text()))

    tests_dir = destination / "tests"
    _sanitize_verifier_config(tests_dir / "config.json")
    upstream_test = tests_dir / "test.sh"
    if not upstream_test.is_file():
        raise FileNotFoundError(f"upstream SWE verifier missing: {upstream_test}")
    upstream_bytes = upstream_test.read_bytes()
    upstream_mode = stat.S_IMODE(upstream_test.stat().st_mode)
    (tests_dir / "upstream-test.sh").write_bytes(upstream_bytes)
    (tests_dir / "upstream-test.sh").chmod(upstream_mode | 0o500)
    upstream_test.write_text(_WRAPPER_TEST_SH)
    upstream_test.chmod(0o555)

    restore_target = tests_dir / "rsi_arena_restore_tree.py"
    restore_target.write_bytes(_RESTORE_SCRIPT_SOURCE.read_bytes())
    restore_target.chmod(0o555)

    environment_dockerfile = destination / "environment" / "Dockerfile"
    if not environment_dockerfile.is_file():
        raise FileNotFoundError(
            f"upstream SWE environment Dockerfile missing: {environment_dockerfile}"
        )
    base = environment_dockerfile.read_text()
    environment_dockerfile.write_text(base.rstrip() + "\n" + _AGENT_DOCKERFILE_SUFFIX)
    (tests_dir / "Dockerfile").write_text(
        base.rstrip() + "\n" + _VERIFIER_DOCKERFILE_SUFFIX
    )


def _listed_task_ids(root: Path) -> tuple[str, ...]:
    if not root.is_dir():
        return ()
    return tuple(
        sorted(
            child.name
            for child in root.iterdir()
            if child.is_dir() and (child / "task.toml").is_file()
        )
    )


def _transform_digest(source_digest: str) -> str:
    return canonical_digest(
        {
            "identity": DERIVED_TASK_TRANSFORM_IDENTITY,
            "source_content": source_digest,
            "task_toml_contract": {
                "artifacts": "/logs/artifacts",
                "agent_network_mode": "no-network",
                "verifier_environment_mode": "separate",
            },
            "wrapper": _WRAPPER_TEST_SH,
            "agent_dockerfile_suffix": _AGENT_DOCKERFILE_SUFFIX,
            "verifier_dockerfile_suffix": _VERIFIER_DOCKERFILE_SUFFIX,
            "restore_script_sha256": hashlib.sha256(
                _RESTORE_SCRIPT_SOURCE.read_bytes()
            ).hexdigest(),
        }
    )


def build_derived_task_tree(
    source_root: Path,
    derived_root: Path,
    task_ids: list[str] | tuple[str, ...],
) -> DerivedTaskTree:
    """Build or reuse a deterministic, content-checked separate-verifier tree."""

    source_root = Path(source_root).expanduser().resolve()
    derived_root = Path(derived_root).expanduser().resolve()
    ids = tuple(sorted(_safe_task_id(task_id) for task_id in task_ids))
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("SWE derivation requires unique non-empty task ids")
    if (
        source_root == derived_root
        or source_root in derived_root.parents
        or derived_root in source_root.parents
    ):
        raise ValueError("source and derived SWE roots must not overlap")

    source_digest = tree_digest(source_root, list(ids))
    transform_digest = _transform_digest(source_digest)
    expected = {
        "transform_identity": DERIVED_TASK_TRANSFORM_IDENTITY,
        "source_digest": source_digest,
        "transform_digest": transform_digest,
        "task_ids": list(ids),
    }
    stamp_path = derived_root / DERIVATION_STAMP
    try:
        stamp = json.loads(stamp_path.read_text())
        recorded_content = stamp.get("content_digest")
        if (
            all(stamp.get(key) == value for key, value in expected.items())
            and isinstance(recorded_content, str)
            and _listed_task_ids(derived_root) == ids
            and tree_digest(derived_root, list(ids)) == recorded_content
        ):
            return DerivedTaskTree(
                root=derived_root,
                transform_identity=DERIVED_TASK_TRANSFORM_IDENTITY,
                source_digest=source_digest,
                transform_digest=transform_digest,
                content_digest=recorded_content,
            )
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        pass

    derived_root.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(
            prefix=f".{derived_root.name}.tmp-", dir=str(derived_root.parent)
        )
    )
    try:
        for task_id in ids:
            source_task = source_root / task_id
            if not source_task.is_dir():
                raise FileNotFoundError(f"SWE source task missing: {source_task}")
            _render_one_task(source_task, stage / task_id)
        content_digest = tree_digest(stage, list(ids))
        (stage / DERIVATION_STAMP).write_text(
            json.dumps({**expected, "content_digest": content_digest}, sort_keys=True)
            + "\n"
        )
        if derived_root.exists():
            shutil.rmtree(derived_root)
        os.replace(stage, derived_root)
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)

    return DerivedTaskTree(
        root=derived_root,
        transform_identity=DERIVED_TASK_TRANSFORM_IDENTITY,
        source_digest=source_digest,
        transform_digest=transform_digest,
        content_digest=content_digest,
    )


def load_swe_tasks(tasks_root: Path) -> list[dict]:
    tasks_root = Path(tasks_root)
    tasks = []
    for task_dir in sorted(tasks_root.iterdir()):
        manifest = task_dir / "task.toml"
        if not task_dir.is_dir() or not manifest.is_file():
            continue
        manifest_text = manifest.read_text()
        raw = tomllib.loads(manifest_text)
        metadata = raw.get("metadata", {})
        environment = raw.get("environment", {})
        difficulty = metadata.get("difficulty", "unknown")
        repo = task_dir.name.split("__")[0]
        tasks.append(
            {
                "task_id": task_dir.name,
                "level": _LEVEL_BY_DIFFICULTY.get(difficulty, "unknown"),
                "category": repo,
                "tags": [*metadata.get("tags", []), difficulty],
                "agent_timeout_sec": float(
                    raw.get("agent", {}).get("timeout_sec", 3000.0)
                ),
                "verifier_timeout_sec": float(
                    raw.get("verifier", {}).get("timeout_sec", 3000.0)
                ),
                "build_timeout_sec": float(
                    environment.get("build_timeout_sec", 1800.0)
                ),
                "allow_internet": bool(environment.get("allow_internet", False)),
                "agent_offline": _OFFLINE_MARKER in manifest_text,
            }
        )
    if not tasks:
        raise ValueError(f"no task directories with task.toml under {tasks_root}")
    return tasks


def a_materials(
    tasks_root: Path, task_ids: Sequence[str]
) -> Mapping[str, Mapping[str, Any]]:
    """Describe the SWE-owned public A corpus without exposing private bytes."""

    root = Path(tasks_root).expanduser().resolve(strict=True)
    result: dict[str, Mapping[str, Any]] = {}
    field_roots = {
        "task_instruction": ("instruction.md",),
        "task_local_tests": ("tests",),
        "solution_or_gold": ("solution",),
    }
    for task_id in sorted(map(str, task_ids)):
        task_root = (root / _safe_task_id(task_id)).resolve(strict=True)
        if task_root.parent != root:
            raise ValueError(f"unsafe SWE A task ID: {task_id!r}")
        fields: dict[str, Any] = {}
        for field, names in field_roots.items():
            paths: list[Path] = []
            for name in names:
                source = task_root / name
                if source.is_file():
                    paths.append(source.relative_to(task_root))
                elif source.is_dir():
                    paths.extend(
                        path.relative_to(task_root)
                        for path in source.rglob("*")
                        if path.is_file() or path.is_symlink()
                    )
            if not paths:
                fields[field] = {"status": "unsupported"}
                continue
            entries: list[dict[str, Any]] = []
            total_bytes = 0
            for relative in sorted(paths, key=lambda item: item.as_posix()):
                path = task_root / relative
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    raise ValueError(
                        "SWE A corpus material is not a regular file: "
                        + relative.as_posix()
                    )
                raw = path.read_bytes()
                total_bytes += len(raw)
                entries.append(
                    {
                        "path": relative.as_posix(),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "bytes": len(raw),
                    }
                )
            fields[field] = {
                "status": "present",
                "content_digest": "sha256:" + canonical_digest(entries),
                "file_count": len(entries),
                "total_bytes": total_bytes,
                "content_ref": f"bench-snapshot://swe/{task_id}/{field}",
            }
        result[task_id] = fields
    return result


def tree_digest(tasks_root: Path, task_ids: list[str]) -> str:
    """Content digest over every regular file in the restricted task tree.

    Unlike the old fixed filename list, this covers auxiliary private grader
    files and all generated restore/verifier inputs.  Symlinks and special
    files fail closed rather than making identity depend on ambient state.
    The top-level derivation stamp is excluded to avoid self-reference; its
    fields are independently validated by :func:`build_derived_task_tree`.
    """

    tasks_root = Path(tasks_root)
    entries = []
    for task_id in sorted(task_ids):
        safe_id = _safe_task_id(task_id)
        task_root = tasks_root / safe_id
        if not task_root.is_dir():
            raise FileNotFoundError(f"SWE task directory missing: {task_root}")
        found = False
        for path in sorted(
            task_root.rglob("*"),
            key=lambda item: item.relative_to(task_root).as_posix(),
        ):
            rel = path.relative_to(task_root)
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"SWE task digest refuses symlink: {safe_id}/{rel}")
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(
                    f"SWE task digest refuses non-regular file: {safe_id}/{rel}"
                )
            found = True
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            entries.append(
                f"{safe_id}/{rel.as_posix()}:{stat.S_IMODE(info.st_mode):04o}:{digest}"
            )
        if not found:
            raise ValueError(f"SWE task tree is empty: {task_root}")
    return canonical_digest(entries)
