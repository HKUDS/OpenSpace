"""Frozen experiment evidence and controlled source adoption for worker builds."""

# Persistent format and evaluation identifiers retain their historical values.

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
from typing import Mapping

from agentpilot.domain import PolicyAction, canonical_json
from agentpilot.research.presentation import experiment_result_view


HANDOFF_SCHEMA = "openthink.experiment-handoff.v1"
_FILE_SCHEMA = "openthink.experiment-handoff-file.v1"
WORKER_HANDOFF_PATH = ".agentpilot-build-input/experiments.json"
_GIT_OBJECT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def _mapping(value, name: str) -> dict:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    try:
        result = json.loads(canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain finite JSON values") from exc
    if not isinstance(result, dict):
        raise TypeError(f"{name} must be a mapping")
    return result


def _write_json(path: Path, value: Mapping) -> None:
    encoded = (canonical_json(value) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temporary.open("wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def normalize_handoff(value) -> dict | None:
    if value is None:
        return None
    handoff = _mapping(value, "experiment_handoff")
    if set(handoff) != {"schema", "adopted_ticket", "experiments"}:
        raise ValueError("experiment_handoff has unexpected fields")
    if handoff["schema"] != HANDOFF_SCHEMA:
        raise ValueError("experiment_handoff uses an unsupported schema")
    experiments = handoff["experiments"]
    if not isinstance(experiments, list) or not experiments:
        raise ValueError("experiment_handoff needs at least one experiment")
    identities = []
    for material in experiments:
        if not isinstance(material, dict):
            raise TypeError("experiment material must be a mapping")
        required = {
            "ticket_id", "track_id", "kind", "status", "instruction", "base",
            "result_digest", "result",
        }
        if set(material) != required:
            raise ValueError("experiment material has unexpected fields")
        identity = material["ticket_id"]
        if (
            not isinstance(identity, str)
            or not identity.startswith("X")
            or not identity[1:].isdigit()
            or identity[1:2] == "0"
        ):
            raise ValueError("experiment material has an invalid ticket id")
        if identity in identities or material["kind"] != "experiment":
            raise ValueError("experiment material must contain unique experiment tickets")
        if not isinstance(material["instruction"], str):
            raise TypeError("experiment instruction must be text")
        base = material["base"]
        if not isinstance(base, dict) or set(base) != {"artifact_id", "commit_sha"}:
            raise ValueError("experiment material has an invalid base")
        if not isinstance(base["artifact_id"], str) or not isinstance(base["commit_sha"], str):
            raise TypeError("experiment material base values must be text")
        if not isinstance(material["result"], dict):
            raise TypeError("experiment material result must be a mapping")
        digest = material["result_digest"]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("experiment material has an invalid result digest")
        identities.append(identity)
    adopted = handoff["adopted_ticket"]
    if adopted is not None and adopted not in identities:
        raise ValueError("adopted experiment is absent from its handoff")
    if adopted is not None:
        selected = experiments[identities.index(adopted)]
        if selected["status"] != "succeeded":
            raise ValueError("only a successful experiment can be adopted")
    return handoff


def validate_handoff_tickets(value, resolve_ticket, *, build_base_commit_sha=None) -> dict | None:
    """Bind a supplied bundle to immutable store tickets before dispatch."""
    handoff = normalize_handoff(value)
    if handoff is None:
        return None
    if not callable(resolve_ticket):
        raise TypeError("resolve_ticket must be callable")
    for material in handoff["experiments"]:
        try:
            ticket = resolve_ticket(material["ticket_id"])
        except KeyError as exc:
            raise ValueError("experiment handoff names an unknown ticket") from exc
        if getattr(ticket, "kind", None) != "experiment":
            raise ValueError("experiment handoff ticket has another kind")
        raw_result = getattr(ticket, "result", None)
        if not isinstance(raw_result, Mapping):
            raise ValueError("experiment handoff ticket has no completed result")
        actual_digest = hashlib.sha256(canonical_json(raw_result).encode("utf-8")).hexdigest()
        if material["result_digest"] != actual_digest:
            raise ValueError("experiment handoff result digest does not match its ticket")
        arguments = getattr(ticket, "arguments", None)
        if not isinstance(arguments, Mapping) or not isinstance(arguments.get("base"), Mapping):
            raise ValueError("experiment handoff ticket has no frozen base")
        base = arguments["base"]
        expected_base = {
            "artifact_id": base.get("artifact_id", base.get("id")),
            "commit_sha": base.get("commit_sha"),
        }
        public_result = experiment_result_view(raw_result)
        expected = {
            "ticket_id": getattr(ticket, "id", None),
            "track_id": getattr(ticket, "track_id", None),
            "kind": "experiment",
            "status": str(getattr(ticket, "status", "")),
            "instruction": arguments.get("instruction", ""),
            "base": expected_base,
            "result_digest": actual_digest,
            "result": public_result,
        }
        if material != expected:
            raise ValueError("experiment handoff material differs from its ticket")
    adopted = _selected_material(handoff)
    if adopted is not None:
        if str(adopted["status"]) != "succeeded":
            raise ValueError("only a successful experiment can be adopted")
        if build_base_commit_sha is not None and adopted["base"]["commit_sha"] != build_base_commit_sha:
            raise ValueError("adopted experiment was produced from another build base")
    return handoff


def freeze_handoff(path: Path, value) -> tuple[dict | None, dict | None]:
    handoff = normalize_handoff(value)
    if handoff is None:
        return None, None
    encoded = (canonical_json(handoff) + "\n").encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(info.st_mode):
            raise ValueError("experiment handoff file is not a regular file")
        if info.st_mode & 0o222 or path.read_bytes() != encoded:
            raise ValueError("frozen experiment handoff was modified")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
        with temporary.open("wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o444)
        temporary.replace(path)
    descriptor = {
        "schema": _FILE_SCHEMA,
        "path": WORKER_HANDOFF_PATH,
        "sha256": digest,
        "experiment_tickets": [row["ticket_id"] for row in handoff["experiments"]],
        "adopted_ticket": handoff["adopted_ticket"],
    }
    return handoff, descriptor


def install_worker_handoff(workspace: Path, handoff: Mapping | None) -> Path | None:
    """Create the fixed worker-visible copy without making it candidate input."""
    if handoff is None:
        return None
    target = workspace / WORKER_HANDOFF_PATH
    encoded = (canonical_json(handoff) + "\n").encode("utf-8")
    if target.exists() or target.is_symlink():
        info = target.lstat()
        if target.is_symlink() or not stat.S_ISREG(info.st_mode):
            raise ValueError("worker experiment handoff is not a regular file")
        if info.st_mode & 0o222 or target.read_bytes() != encoded:
            raise ValueError("worker experiment handoff was modified")
        return target
    if target.parent.exists() or target.parent.is_symlink():
        if target.parent.is_symlink() or not target.parent.is_dir() or any(target.parent.iterdir()):
            raise ValueError("worker experiment handoff directory is not empty")
    else:
        target.parent.mkdir(mode=0o700)
    temporary = target.with_name(target.name + f".{os.getpid()}.tmp")
    with temporary.open("wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o444)
    temporary.replace(target)
    return target


def verify_worker_handoff(workspace: Path, handoff: Mapping | None) -> None:
    if handoff is None:
        return
    target = workspace / WORKER_HANDOFF_PATH
    encoded = (canonical_json(handoff) + "\n").encode("utf-8")
    try:
        info = target.lstat()
    except FileNotFoundError as exc:
        raise ValueError("worker experiment handoff is missing") from exc
    if target.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise ValueError("worker experiment handoff is not a regular file")
    if info.st_mode & 0o222 or target.read_bytes() != encoded:
        raise ValueError("worker experiment handoff was modified")
    if set(target.parent.iterdir()) != {target}:
        raise ValueError("worker experiment handoff directory contains unexpected files")


def remove_worker_handoff(workspace: Path, handoff: Mapping | None) -> None:
    if handoff is None:
        return
    verify_worker_handoff(workspace, handoff)
    target = workspace / WORKER_HANDOFF_PATH
    target.unlink()
    target.parent.rmdir()


def verify_handoff(path: Path, handoff: Mapping | None) -> None:
    if handoff is None:
        return
    encoded = (canonical_json(handoff) + "\n").encode("utf-8")
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError("frozen experiment handoff is missing") from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise ValueError("experiment handoff file is not a regular file")
    if info.st_mode & 0o222 or path.read_bytes() != encoded:
        raise ValueError("frozen experiment handoff was modified")


def _run_git(workspace: Path, arguments: list[str], *, stdin: bytes | None = None) -> bytes:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=workspace,
        env={
            "PATH": os.environ.get("PATH", ""),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
        },
        input=stdin,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        operation = arguments[0] if arguments else "command"
        raise ValueError(f"cannot {operation} adopted experiment source")
    return completed.stdout


def _safe_path(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("experiment source path must be text")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("experiment source path is invalid")
    return value


def _patch_paths(workspace: Path, patch: bytes) -> tuple[str, ...]:
    output = _run_git(workspace, ["apply", "--numstat", "-z", "-"], stdin=patch)
    paths = []
    for record in output.split(b"\0"):
        if not record:
            continue
        fields = record.split(b"\t", 2)
        if len(fields) != 3 or not fields[2]:
            raise ValueError("adopted experiment patch has an unsupported path record")
        try:
            paths.append(_safe_path(fields[2].decode("utf-8")))
        except UnicodeDecodeError as exc:
            raise ValueError("adopted experiment patch path is not UTF-8") from exc
    if len(paths) != len(set(paths)):
        raise ValueError("adopted experiment patch repeats a path")
    return tuple(paths)


def _retained_files(material: Mapping) -> dict[str, str]:
    files = material["result"].get("files", [])
    if not isinstance(files, list):
        raise TypeError("experiment retained files must be a list")
    retained = {}
    for row in files:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str) or not isinstance(row.get("content"), str):
            raise TypeError("experiment retained file is invalid")
        if row["path"] in retained:
            raise ValueError("experiment retained file path is duplicated")
        if "bytes" in row and row["bytes"] != len(row["content"].encode("utf-8")):
            raise ValueError("experiment retained file byte count changed")
        retained[row["path"]] = row["content"]
    return retained


def _selected_material(handoff: Mapping) -> Mapping | None:
    adopted = handoff.get("adopted_ticket")
    if adopted is None:
        return None
    return next(row for row in handoff["experiments"] if row["ticket_id"] == adopted)


def development_measurement_sources(
    repository: Path,
    handoff: Mapping | None,
    final_tree_sha: str,
) -> list[dict] | None:
    """Compare measured immutable source trees with the accepted build bytes."""
    if handoff is None:
        return None
    material = _selected_material(handoff)
    if material is None:
        return None
    measurements = material["result"].get("development_measurements")
    if not isinstance(measurements, list) or not measurements:
        return None
    records = []
    for measured in measurements:
        if not isinstance(measured, Mapping):
            continue
        identity = measured.get("measurement_id")
        candidate = measured.get("candidate")
        commit = candidate.get("commit_sha") if isinstance(candidate, Mapping) else None
        tree = None
        if isinstance(commit, str) and _GIT_OBJECT.fullmatch(commit):
            completed = subprocess.run(
                ["git", "rev-parse", "--verify", f"{commit}^{{tree}}"],
                cwd=repository,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_CONFIG_SYSTEM": "/dev/null",
                    "GIT_TERMINAL_PROMPT": "0",
                },
                capture_output=True,
                check=False,
            )
            resolved = completed.stdout.decode("ascii", "ignore").strip()
            if completed.returncode == 0 and _GIT_OBJECT.fullmatch(resolved):
                tree = resolved
        records.append({
            "measurement_id": identity,
            "measured_candidate_commit_sha": commit,
            "measured_candidate_tree_sha": tree,
            "same_source": "unknown" if tree is None else tree == final_tree_sha,
        })
    return records or None


def apply_adopted_experiment(
    workspace: Path,
    action: PolicyAction,
    handoff: Mapping | None,
    *,
    plan_path: Path,
    marker_path: Path,
) -> dict | None:
    if handoff is None:
        return None
    material = _selected_material(handoff)
    if material is None:
        return None
    if material["base"]["commit_sha"] != action.parent_commit_sha:
        raise ValueError("adopted experiment was produced from another build base")
    result = material["result"]
    if result.get("base_commit_sha") != action.parent_commit_sha:
        raise ValueError("adopted experiment result does not match the build base")
    digest = hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()
    if marker_path.exists():
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if marker.get("sha256") != digest:
            raise ValueError("adopted experiment marker belongs to another input")
        return marker
    if plan_path.exists():
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if plan.get("sha256") != digest:
            raise ValueError("adopted experiment plan belongs to another input")
        _run_git(workspace, ["reset", "--hard", action.parent_commit_sha])
        _run_git(workspace, ["clean", "-fd"])
    else:
        _write_json(plan_path, {"sha256": digest})

    retained = _retained_files(material)
    patch_text = retained.get("_reusable/source.patch", "")
    patch = patch_text.encode("utf-8")
    allowed = set(action.direction.resolved_scope)
    declared_new = {row.path for row in action.direction.new_paths}
    changed = set()
    if patch:
        patch_paths = _patch_paths(workspace, patch)
        if not set(patch_paths) <= allowed:
            raise ValueError("adopted experiment patch exceeds the build scope")
        for path in patch_paths:
            exists = bool(_run_git(workspace, ["ls-tree", "--name-only", action.parent_commit_sha, "--", path]))
            if not exists and path not in declared_new:
                raise ValueError("adopted experiment patch creates an undeclared path")
        _run_git(workspace, ["apply", "--check", "--recount", "--whitespace=nowarn", "-"], stdin=patch)
        _run_git(workspace, ["apply", "--recount", "--whitespace=nowarn", "-"], stdin=patch)
        changed.update(patch_paths)

    new_sources = result.get("new_source_files", [])
    if not isinstance(new_sources, list):
        raise TypeError("experiment new_source_files must be a list")
    for row in new_sources:
        if not isinstance(row, dict) or set(row) != {"path", "retained_as"}:
            raise ValueError("experiment new source metadata is invalid")
        path = _safe_path(row["path"])
        retained_as = row["retained_as"]
        if path not in declared_new or path not in allowed:
            raise ValueError("adopted experiment new file was not declared by the build")
        if retained_as not in retained:
            raise ValueError("adopted experiment new file content is missing")
        target = workspace / path
        if target.exists() or target.is_symlink():
            raise ValueError("adopted experiment new file already exists at the build base")
        current = workspace
        for part in PurePosixPath(path).parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise ValueError("adopted experiment new file traverses a symlink")
            if current.exists() and not current.is_dir():
                raise ValueError("adopted experiment new file parent is not a directory")
            current.mkdir(exist_ok=True)
        target.write_text(retained[retained_as], encoding="utf-8")
        changed.add(path)

    retained_change = bool(changed)
    if bool(result.get("source_patch_retained")) != retained_change:
        raise ValueError("adopted experiment source-change metadata is inconsistent")
    if changed:
        _run_git(workspace, ["add", "-A", "--", *sorted(changed)])
    tree = _run_git(workspace, ["write-tree"]).decode("ascii").strip()
    source_diff = _run_git(
        workspace,
        ["diff", "--cached", "--binary", "--no-ext-diff", action.parent_commit_sha],
    )
    _run_git(workspace, ["reset", "--mixed", action.parent_commit_sha])
    marker = {
        "sha256": digest,
        "changed_files": sorted(changed),
        "preapplied_tree_sha": tree,
        "preapplied_source_diff_sha256": hashlib.sha256(source_diff).hexdigest(),
    }
    _write_json(marker_path, marker)
    return marker


__all__ = [
    "HANDOFF_SCHEMA",
    "WORKER_HANDOFF_PATH",
    "apply_adopted_experiment",
    "development_measurement_sources",
    "freeze_handoff",
    "install_worker_handoff",
    "normalize_handoff",
    "remove_worker_handoff",
    "validate_handoff_tickets",
    "verify_handoff",
    "verify_worker_handoff",
]
