"""Terminal-Bench 2.1 task loading.

Input is a local clone of harbor-framework/terminal-bench-2-1.  Task IDs and
task bytes are read from the fixed official commit, independently of the
checkout's current HEAD.  Each task is a directory with ``task.toml`` (Harbor
task format): difficulty, category, docker environment and timeouts.

The adapter exposes only a *restricted subset* to the framework: the A/B/C
split runs on top of it (contract 03 §1).  Clusters are task categories so
the wave stratification spreads across domains.
"""

from __future__ import annotations

import io
import json
import math
import os
import shutil
import stat
import subprocess
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import tomllib

from rsi_arena.benchmarks.harbor_runtime.materials import (
    MaterialBoundaryError,
    task_tree_digest,
)
from rsi_arena.benchmarks.task_records import task_list_digest
from rsi_arena.contracts import canonical_digest
from rsi_arena.run_layout import atomic_write_json

from .verifier_dependencies import (
    TB21VerifierDependencyClosureError,
    load_tb21_verifier_dependency_closure,
)

OFFICIAL_TB21_COMMIT = "5c8eadf1f393183288fa08b8f73ca9a469cc5e00"
OFFICIAL_TB21_REPOSITORY_TREE = "49ca0b26221536dd9f60d8e66938873bda4bf37b"
OFFICIAL_TB21_TASKS_TREE = "2f0f5fdc68f0befd9b4745386eb8698264b00d8a"
OFFICIAL_TB21_TASK_ID_DIGEST = (
    "733562cbea1dd6f0af4b13e6c1c54702ec3f822037f05b978e56f63a1c6a1a36"
)
OFFICIAL_TB21_TASK_COUNT = 89
RUN_OWNED_TB21_SOURCE = "rsi-arena-run-owned-tb21"
RUN_OWNED_TB21_MANIFEST = "task-snapshot.json"
TB2_AGENT_SETUP_SECONDS = 360.0
TB2_ORCHESTRATION_SLACK_SECONDS = 300.0


class TB21SnapshotError(ValueError):
    """The requested task source cannot provide the official snapshot."""


@dataclass(frozen=True)
class TB2TaskTimeoutAuthority:
    """Per-task science deadlines admitted by frozen scheduling ceilings."""

    agent_deadline_seconds: float
    bridge_rpc_deadline_seconds: float
    bridge_hard_kill_deadline_seconds: float
    verifier_deadline_seconds: float
    outer_deadline_seconds: float


@dataclass(frozen=True)
class TB21PinnedTaskMaterial:
    """Candidate-visible task bytes read from the frozen Git object."""

    task_id: str
    instruction: bytes
    task_manifest: bytes
    tree_digest: str


@dataclass(frozen=True)
class TB21TaskSnapshot:
    """Verified, path-separated Terminal-Bench 2.1 task snapshot."""

    repository_root: Path
    tasks_root: Path
    commit: str
    repository_tree: str
    tasks_tree: str
    task_id_digest: str
    tasks: tuple[Mapping[str, object], ...]
    pinned_materials: tuple[TB21PinnedTaskMaterial, ...]
    source: str
    official_verified: bool

    @property
    def identity(self) -> dict[str, str]:
        identity = {
            "schema": "tb21-task-snapshot",
            "source": self.source,
            "git_commit": self.commit,
            "repository_tree": self.repository_tree,
            "tasks_tree": self.tasks_tree,
            "task_id_digest": "sha256:" + self.task_id_digest,
        }
        return identity

    @property
    def snapshot_digest(self) -> str:
        task_pool = [
            {
                "task_id": str(task["task_id"]),
                "cluster": f"{task['category']}/{task['level']}",
            }
            for task in sorted(self.tasks, key=lambda item: str(item["task_id"]))
        ]
        task_pool_digest = "sha256:" + canonical_digest(task_pool)
        return "sha256:" + canonical_digest(
            {"identity": self.identity, "task_pool_digest": task_pool_digest}
        )

    @property
    def provenance_evidence(self) -> dict[str, object]:
        evidence: dict[str, object] = {
            "schema": "tb21-source-identity",
            "official_source": self.official_verified,
            "source_commit": self.commit,
            "repository_tree": self.repository_tree,
            "tasks_tree": self.tasks_tree,
            "task_count": len(self.tasks),
            "task_list_digest": "sha256:" + self.task_id_digest,
        }
        return evidence

    def pinned_task_material(self, task_id: str) -> TB21PinnedTaskMaterial:
        matches = [item for item in self.pinned_materials if item.task_id == task_id]
        if len(matches) != 1:
            raise TB21SnapshotError("task material is absent or duplicated")
        return matches[0]


def _isolated_git_env() -> dict[str, str]:
    env = dict(os.environ)
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    ):
        env.pop(name, None)
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    return env


def _git(root: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            env=_isolated_git_env(),
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise TB21SnapshotError(f"cannot establish TB2.1 Git identity: {root}") from exc
    return completed.stdout.strip()


def _git_bytes(root: Path, *args: str) -> bytes:
    """Read one pinned Git object without decoding or consulting worktree bytes."""

    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            timeout=30,
            env=_isolated_git_env(),
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise TB21SnapshotError(f"cannot read pinned TB2.1 Git blob: {root}") from exc
    return completed.stdout


def materialize_tb21_task(
    snapshot: TB21TaskSnapshot,
    task_id: str,
    destination: Path,
) -> Path:
    """Materialize one complete task package from the snapshot's fixed object."""

    relative_id = PurePosixPath(task_id)
    if (
        relative_id.is_absolute()
        or len(relative_id.parts) != 1
        or task_id in {"", ".", ".."}
    ):
        raise TB21SnapshotError("TB2.1 task identity is invalid")
    pinned = snapshot.pinned_task_material(task_id)
    target = Path(destination).absolute()
    if target.exists() or target.is_symlink():
        raise TB21SnapshotError("TB2.1 task materialization target already exists")
    try:
        if snapshot.source == RUN_OWNED_TB21_SOURCE:
            source = snapshot.tasks_root / task_id
            target.mkdir(mode=0o700, parents=True)
            _copy_regular_task_tree(source, target)
        else:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(snapshot.repository_root),
                    "archive",
                    "--format=tar",
                    f"{snapshot.commit}:tasks/{task_id}",
                ],
                check=True,
                capture_output=True,
                timeout=120,
                env=_isolated_git_env(),
            )
            target.mkdir(mode=0o700, parents=True)
            with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
                for member in archive.getmembers():
                    relative = PurePosixPath(member.name)
                    if (
                        relative.is_absolute()
                        or not relative.parts
                        or any(part in {"", ".", ".."} for part in relative.parts)
                    ):
                        raise TB21SnapshotError("TB2.1 task archive is unsafe")
                    output = target.joinpath(*relative.parts)
                    if member.isdir():
                        output.mkdir(mode=0o700, parents=True, exist_ok=True)
                        continue
                    if not member.isfile():
                        raise TB21SnapshotError("TB2.1 task archive is unsafe")
                    source_file = archive.extractfile(member)
                    if source_file is None:
                        raise TB21SnapshotError("TB2.1 task archive is invalid")
                    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    descriptor = os.open(
                        output,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_NOFOLLOW", 0),
                        stat.S_IMODE(member.mode) & 0o777,
                    )
                    try:
                        while chunk := source_file.read(1024 * 1024):
                            view = memoryview(chunk)
                            while view:
                                written = os.write(descriptor, view)
                                if written <= 0:
                                    raise OSError("short task material write")
                                view = view[written:]
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
        if task_tree_digest(target) != pinned.tree_digest:
            raise TB21SnapshotError("TB2.1 materialized task disagrees with snapshot")
    except (
        MaterialBoundaryError,
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        tarfile.TarError,
    ) as exc:
        raise TB21SnapshotError("cannot materialize frozen TB2.1 task") from exc
    return target


def _copy_regular_task_tree(source: Path, destination: Path) -> None:
    for path in sorted(
        source.rglob("*"),
        key=lambda item: item.relative_to(source).as_posix(),
    ):
        relative = path.relative_to(source)
        info = path.lstat()
        target = destination / relative
        if stat.S_ISLNK(info.st_mode):
            raise TB21SnapshotError("TB2.1 task contains a symlink")
        if stat.S_ISDIR(info.st_mode):
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
        elif stat.S_ISREG(info.st_mode):
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copy2(path, target, follow_symlinks=False)
        else:
            raise TB21SnapshotError("TB2.1 task contains unsupported material")


def materialize_tb21_tasks(
    snapshot: TB21TaskSnapshot,
    destination_root: Path,
    task_ids: tuple[str, ...],
) -> Path:
    """Materialize selected fixed-object tasks into one persistent owned root."""

    root = Path(destination_root).absolute()
    if not task_ids or len(set(task_ids)) != len(task_ids):
        raise TB21SnapshotError("TB2.1 materialization task IDs must be unique")
    if root.exists() or root.is_symlink():
        raise TB21SnapshotError("TB2.1 task root already exists")
    root.mkdir(mode=0o700, parents=True)
    try:
        for task_id in task_ids:
            materialize_tb21_task(snapshot, task_id, root / task_id)
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return root


def _task_record_from_manifest(task_id: str, task_manifest: bytes) -> dict:
    raw = tomllib.loads(task_manifest.decode("utf-8"))
    metadata = raw.get("metadata", {})
    environment = raw.get("environment", {})
    return {
        "task_id": task_id,
        "level": metadata.get("difficulty", "unknown"),
        "category": metadata.get("category", "unknown"),
        "tags": list(metadata.get("tags", [])),
        "agent_timeout_sec": float(raw.get("agent", {}).get("timeout_sec", 900.0)),
        "verifier_timeout_sec": float(
            raw.get("verifier", {}).get("timeout_sec", 900.0)
        ),
        "build_timeout_sec": float(environment.get("build_timeout_sec", 600.0)),
        "allow_internet": bool(environment.get("allow_internet", False)),
    }


def _pinned_task_tree_digest(repo: Path, commit: str, task_id: str) -> str:
    entries: list[str] = []
    treeish = f"{commit}:tasks/{task_id}"
    for record in _git(repo, "ls-tree", "-r", "-z", treeish).split("\0"):
        if not record:
            continue
        try:
            header, raw_path = record.split("\t", 1)
            mode, object_type, object_id = header.split(" ", 2)
        except ValueError as exc:
            raise TB21SnapshotError("TB2.1 task tree record is malformed") from exc
        path = Path(raw_path)
        if (
            object_type != "blob"
            or mode not in {"100644", "100755"}
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise TB21SnapshotError("TB2.1 task tree contains unsupported material")
        entries.append(f"{path.as_posix()}:{mode[-4:]}:{object_id}")
    if not entries:
        raise TB21SnapshotError("TB2.1 task tree is empty")
    return canonical_digest(entries)


def resolve_tb2_task_timeout_authority(
    task: Mapping[str, object],
    *,
    worker_timeout_seconds: float,
    outer_timeout_seconds: float,
    cleanup_grace_seconds: float,
    verifier_timeout_multiplier: float,
) -> TB2TaskTimeoutAuthority:
    """Resolve one task's official deadlines without clamping to ceilings."""

    task_id = str(task["task_id"])
    agent = float(task["agent_timeout_sec"])
    verifier = float(task["verifier_timeout_sec"])
    verifier_multiplier = float(verifier_timeout_multiplier)
    build = float(task["build_timeout_sec"])
    worker_ceiling = float(worker_timeout_seconds)
    outer_ceiling = float(outer_timeout_seconds)
    cleanup = float(cleanup_grace_seconds)
    if not math.isfinite(verifier_multiplier) or verifier_multiplier <= 0.0:
        raise ValueError("verifier_timeout_multiplier must be positive and finite")
    verifier_deadline = verifier * verifier_multiplier
    if agent > worker_ceiling:
        raise ValueError(
            f"TB2 task {task_id!r} official agent timeout {agent:g}s exceeds "
            f"worker_timeout_seconds ceiling {worker_ceiling:g}s"
        )
    outer = (
        2 * build
        + TB2_AGENT_SETUP_SECONDS
        + agent
        + verifier_deadline
        + 2 * cleanup
        + TB2_ORCHESTRATION_SLACK_SECONDS
    )
    if outer >= outer_ceiling:
        raise ValueError(
            f"TB2 task {task_id!r} outer deadline {outer:g}s must be below "
            f"the per-task outer ceiling {outer_ceiling:g}s"
        )
    return TB2TaskTimeoutAuthority(
        agent_deadline_seconds=agent,
        bridge_rpc_deadline_seconds=agent,
        bridge_hard_kill_deadline_seconds=agent + cleanup,
        verifier_deadline_seconds=verifier_deadline,
        outer_deadline_seconds=outer,
    )


def _load_tb21_task_snapshot_with_expected_identity(
    repository_root: str | Path,
    *,
    expected_commit: str,
    expected_repository_tree: str,
    expected_tasks_tree: str,
    expected_task_count: int,
    expected_task_id_digest: str,
    source: str,
    official_verified: bool,
) -> TB21TaskSnapshot:
    """Validate one explicit identity; production pins are supplied below."""

    try:
        repo = Path(repository_root).expanduser().resolve(strict=True)
    except OSError as exc:
        raise TB21SnapshotError(
            f"TB2.1 repository root is unavailable: {repository_root}"
        ) from exc
    if not repo.is_dir():
        raise TB21SnapshotError("TB2.1 repository root is not a directory")
    top_level = Path(_git(repo, "rev-parse", "--show-toplevel")).resolve(strict=True)
    if top_level != repo:
        raise TB21SnapshotError("TB2.1 path is not an independent Git checkout")
    commit = _git(repo, "rev-parse", f"{expected_commit}^{{commit}}")
    repository_tree = _git(repo, "rev-parse", f"{commit}^{{tree}}")
    tasks_tree = _git(repo, "rev-parse", f"{commit}:tasks")
    if repository_tree != expected_repository_tree or tasks_tree != expected_tasks_tree:
        raise TB21SnapshotError("TB2.1 repository/tasks tree identity mismatch")

    tasks_root = repo / "tasks"
    task_ids = tuple(
        task_id
        for task_id in _git(
            repo, "ls-tree", "-d", "--name-only", "-z", f"{commit}:tasks"
        ).split("\0")
        if task_id
    )
    records: list[dict] = []
    pinned_materials: list[TB21PinnedTaskMaterial] = []
    for task_id in task_ids:
        task_manifest = _git_bytes(repo, "show", f"{commit}:tasks/{task_id}/task.toml")
        instruction = _git_bytes(
            repo, "show", f"{commit}:tasks/{task_id}/instruction.md"
        )
        records.append(_task_record_from_manifest(task_id, task_manifest))
        pinned_materials.append(
            TB21PinnedTaskMaterial(
                task_id=task_id,
                instruction=instruction,
                task_manifest=task_manifest,
                tree_digest=_pinned_task_tree_digest(repo, commit, task_id),
            )
        )
    digest = task_list_digest(list(records))
    if len(records) != expected_task_count:
        raise TB21SnapshotError(
            f"TB2.1 task count {len(records)} != {expected_task_count}"
        )
    if digest != expected_task_id_digest:
        raise TB21SnapshotError("TB2.1 task ID digest mismatch")
    return TB21TaskSnapshot(
        repository_root=repo,
        tasks_root=tasks_root,
        commit=commit,
        repository_tree=repository_tree,
        tasks_tree=tasks_tree,
        task_id_digest=digest,
        tasks=tuple(records),
        pinned_materials=tuple(pinned_materials),
        source=source,
        official_verified=official_verified,
    )


def load_tb21_task_snapshot(repository_root: str | Path) -> TB21TaskSnapshot:
    """Load only the canonical official snapshot with non-overridable pins."""

    snapshot = _load_tb21_task_snapshot_with_expected_identity(
        repository_root,
        expected_commit=OFFICIAL_TB21_COMMIT,
        expected_repository_tree=OFFICIAL_TB21_REPOSITORY_TREE,
        expected_tasks_tree=OFFICIAL_TB21_TASKS_TREE,
        expected_task_count=OFFICIAL_TB21_TASK_COUNT,
        expected_task_id_digest=OFFICIAL_TB21_TASK_ID_DIGEST,
        source="harbor-framework/terminal-bench-2-1",
        official_verified=True,
    )
    verifier_tree_oids: dict[str, str] = {}
    environment_tree_oids: dict[str, str] = {}
    for record in _git(
        snapshot.repository_root,
        "ls-tree",
        "-r",
        "-t",
        "-z",
        f"{snapshot.commit}:tasks",
    ).split("\0"):
        if not record:
            continue
        try:
            metadata, path = record.split("\t", 1)
            _mode, kind, object_id = metadata.split(" ", 2)
        except ValueError as exc:
            raise TB21SnapshotError("malformed TB2.1 Git tree record") from exc
        parts = path.split("/")
        if kind == "tree" and len(parts) == 2 and parts[1] == "tests":
            verifier_tree_oids[parts[0]] = object_id
        if kind == "tree" and len(parts) == 2 and parts[1] == "environment":
            environment_tree_oids[parts[0]] = object_id
    environment_images: dict[str, str] = {}
    for material in snapshot.pinned_materials:
        task_manifest = tomllib.loads(material.task_manifest.decode("utf-8"))
        environment = task_manifest.get("environment", {})
        image = (
            environment.get("docker_image")
            if isinstance(environment, Mapping)
            else None
        )
        if not isinstance(image, str) or not image:
            raise TB21SnapshotError(
                f"TB2.1 task {material.task_id!r} has no environment image"
            )
        environment_images[material.task_id] = image
    try:
        load_tb21_verifier_dependency_closure(
            repository=snapshot.source,
            repository_root=snapshot.repository_root,
            commit=snapshot.commit,
            tasks_tree=snapshot.tasks_tree,
            task_ids=tuple(str(task["task_id"]) for task in snapshot.tasks),
            verifier_tree_oids=verifier_tree_oids,
            environment_tree_oids=environment_tree_oids,
            environment_images=environment_images,
        )
    except TB21VerifierDependencyClosureError as exc:
        raise TB21SnapshotError(
            "TB2.1 verifier dependency closure does not match the official snapshot"
        ) from exc
    return snapshot


def validate_tb21_task_snapshot(snapshot: TB21TaskSnapshot) -> None:
    """Validate either the complete official source or one run-owned slice."""

    records = list(snapshot.tasks)
    task_ids = [str(record.get("task_id", "")) for record in records]
    materials = {material.task_id: material for material in snapshot.pinned_materials}
    if (
        not snapshot.official_verified
        or snapshot.commit != OFFICIAL_TB21_COMMIT
        or snapshot.repository_tree != OFFICIAL_TB21_REPOSITORY_TREE
        or snapshot.tasks_tree != OFFICIAL_TB21_TASKS_TREE
        or not task_ids
        or len(set(task_ids)) != len(task_ids)
        or set(task_ids) != set(materials)
        or snapshot.task_id_digest != task_list_digest(records)
    ):
        raise TB21SnapshotError("TB2.1 snapshot identity is invalid")
    if snapshot.source == "harbor-framework/terminal-bench-2-1":
        if (
            len(records) != OFFICIAL_TB21_TASK_COUNT
            or snapshot.task_id_digest != OFFICIAL_TB21_TASK_ID_DIGEST
        ):
            raise TB21SnapshotError("official TB2.1 snapshot is incomplete")
        return
    elif snapshot.source != RUN_OWNED_TB21_SOURCE:
        raise TB21SnapshotError("TB2.1 snapshot source is invalid")
    for task_id in task_ids:
        task_dir = snapshot.tasks_root / task_id
        material = materials[task_id]
        try:
            if (
                (task_dir / "instruction.md").read_bytes() != material.instruction
                or (task_dir / "task.toml").read_bytes() != material.task_manifest
                or task_tree_digest(task_dir) != material.tree_digest
            ):
                raise TB21SnapshotError("TB2.1 task material changed")
        except OSError as exc:
            raise TB21SnapshotError("TB2.1 task material is unavailable") from exc


def materialize_run_owned_tb21_snapshot(
    source: TB21TaskSnapshot,
    authority_root: Path,
    task_ids: tuple[str, ...],
) -> TB21TaskSnapshot:
    """Freeze only this run's authorized A/B task packages and metadata."""

    if not task_ids or len(set(task_ids)) != len(task_ids):
        raise TB21SnapshotError("run-owned TB2.1 task IDs must be unique")
    source_records = {str(record["task_id"]): dict(record) for record in source.tasks}
    if not set(task_ids).issubset(source_records):
        raise TB21SnapshotError("run-owned TB2.1 task escaped the official source")
    root = Path(authority_root).absolute()
    if root.exists() or root.is_symlink():
        raise TB21SnapshotError("run-owned TB2.1 authority already exists")
    tasks_root = materialize_tb21_tasks(source, root / "tasks", task_ids)
    records = tuple(source_records[task_id] for task_id in task_ids)
    materials = tuple(
        TB21PinnedTaskMaterial(
            task_id=task_id,
            instruction=(tasks_root / task_id / "instruction.md").read_bytes(),
            task_manifest=(tasks_root / task_id / "task.toml").read_bytes(),
            tree_digest=task_tree_digest(tasks_root / task_id),
        )
        for task_id in task_ids
    )
    snapshot = TB21TaskSnapshot(
        repository_root=root,
        tasks_root=tasks_root,
        commit=source.commit,
        repository_tree=source.repository_tree,
        tasks_tree=source.tasks_tree,
        task_id_digest=task_list_digest(list(records)),
        tasks=records,
        pinned_materials=materials,
        source=RUN_OWNED_TB21_SOURCE,
        official_verified=True,
    )
    atomic_write_json(
        root / RUN_OWNED_TB21_MANIFEST,
        {
            "schema": "rsi-arena-run-owned-tb21",
            "upstream": {
                "source": "harbor-framework/terminal-bench-2-1",
                "commit": source.commit,
                "repository_tree": source.repository_tree,
                "tasks_tree": source.tasks_tree,
            },
            "task_id_digest": snapshot.task_id_digest,
            "tasks": list(records),
            "materials": [
                {"task_id": item.task_id, "tree_digest": item.tree_digest}
                for item in materials
            ],
        },
    )
    validate_tb21_task_snapshot(snapshot)
    return snapshot


def load_run_owned_tb21_snapshot(authority_root: str | Path) -> TB21TaskSnapshot:
    """Reload a frozen task slice without consulting its ambient source checkout."""

    root = Path(authority_root).expanduser().resolve(strict=True)
    try:
        raw = json.loads((root / RUN_OWNED_TB21_MANIFEST).read_text(encoding="utf-8"))
        upstream = raw["upstream"]
        records = tuple(raw["tasks"])
        material_records = raw["materials"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise TB21SnapshotError("run-owned TB2.1 authority is invalid") from exc
    if (
        raw.get("schema") != "rsi-arena-run-owned-tb21"
        or upstream
        != {
            "source": "harbor-framework/terminal-bench-2-1",
            "commit": OFFICIAL_TB21_COMMIT,
            "repository_tree": OFFICIAL_TB21_REPOSITORY_TREE,
            "tasks_tree": OFFICIAL_TB21_TASKS_TREE,
        }
        or not all(isinstance(record, dict) for record in records)
        or not isinstance(material_records, list)
    ):
        raise TB21SnapshotError("run-owned TB2.1 authority changed")
    expected_materials = {
        str(item.get("task_id", "")): str(item.get("tree_digest", ""))
        for item in material_records
        if isinstance(item, dict)
    }
    tasks_root = root / "tasks"
    materials = tuple(
        TB21PinnedTaskMaterial(
            task_id=str(record["task_id"]),
            instruction=(
                tasks_root / str(record["task_id"]) / "instruction.md"
            ).read_bytes(),
            task_manifest=(
                tasks_root / str(record["task_id"]) / "task.toml"
            ).read_bytes(),
            tree_digest=expected_materials[str(record["task_id"])],
        )
        for record in records
    )
    snapshot = TB21TaskSnapshot(
        repository_root=root,
        tasks_root=tasks_root,
        commit=OFFICIAL_TB21_COMMIT,
        repository_tree=OFFICIAL_TB21_REPOSITORY_TREE,
        tasks_tree=OFFICIAL_TB21_TASKS_TREE,
        task_id_digest=str(raw.get("task_id_digest", "")),
        tasks=records,
        pinned_materials=materials,
        source=RUN_OWNED_TB21_SOURCE,
        official_verified=True,
    )
    validate_tb21_task_snapshot(snapshot)
    return snapshot
