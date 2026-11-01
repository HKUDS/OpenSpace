"""Task-package manifests and checkout boundary guards for Harbor benchmarks.

These checks are deliberately defense in depth, not a claim that the current
host-side Harbor process is hermetic.  They make task drift, path overlap and
the most direct symlink mounts fail closed, and provide a redacted manifest
that future A-dev packs may safely expose without serializing evaluator-only
content identities.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from pathlib import Path

from rsi_arena.contracts import canonical_digest
from rsi_arena.evaluation import TaskMaterialManifest

EVALUATOR_ONLY_ROOTS = frozenset({"tests", "solution"})


class MaterialBoundaryError(ValueError):
    """A task package or candidate path violates the material boundary."""


def task_tree_digest(task_dir: Path) -> str:
    """Digest one task tree without consulting ambient Git state."""

    root = Path(task_dir).expanduser().resolve()
    if not root.is_dir():
        raise MaterialBoundaryError(f"task directory is missing: {root}")
    entries: list[str] = []
    for path in sorted(
        root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()
    ):
        rel = path.relative_to(root)
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise MaterialBoundaryError(
                f"non-regular task material is forbidden: {rel}"
            )
        payload = path.read_bytes()
        git_blob = hashlib.sha1(
            f"blob {len(payload)}\0".encode("ascii") + payload,
            usedforsecurity=False,
        ).hexdigest()
        git_mode = "0755" if info.st_mode & 0o111 else "0644"
        entries.append(f"{rel.as_posix()}:{git_mode}:{git_blob}")
    return canonical_digest(entries)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def resolve_task_dir(tasks_root: Path, task_id: str) -> Path:
    """Resolve one task without permitting absolute/path-traversal ids."""
    raw = Path(task_id)
    if (
        raw.is_absolute()
        or not raw.parts
        or any(part in {"", ".", ".."} for part in raw.parts)
    ):
        raise MaterialBoundaryError(f"unsafe task id {task_id!r}")
    root = Path(tasks_root).expanduser().resolve()
    task_dir = (root / raw).resolve()
    if task_dir.parent != root:
        # Harbor dataset task ids are one directory name.  Composite prefixes
        # are removed before reaching a sub-bench, so accepting nested ids here
        # would only enlarge the path-confusion surface.
        raise MaterialBoundaryError(f"task id {task_id!r} does not name a direct child")
    if not task_dir.is_dir():
        raise MaterialBoundaryError(f"task directory is missing: {task_dir}")
    return task_dir


def snapshot_task_materials(task_dir: Path) -> TaskMaterialManifest:
    """Hash every regular task file, split by agent/evaluator role.

    Symlinks are rejected rather than followed.  A task package is trusted
    evaluator input; permitting a symlink out of the pinned tree would make
    its apparent identity depend on mutable ambient filesystem state.
    """
    root = Path(task_dir).expanduser().resolve()
    if not root.is_dir():
        raise MaterialBoundaryError(f"task directory is missing: {root}")

    public_entries: list[str] = []
    private_entries: list[str] = []
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        rel = path.relative_to(root)
        try:
            info = path.lstat()
        except FileNotFoundError as exc:
            raise MaterialBoundaryError(
                f"task material changed while being snapshotted: {rel}"
            ) from exc
        if stat.S_ISLNK(info.st_mode):
            raise MaterialBoundaryError(f"task material symlink is forbidden: {rel}")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise MaterialBoundaryError(
                f"non-regular task material is forbidden: {rel}"
            )
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entry = f"{rel.as_posix()}:{stat.S_IMODE(info.st_mode):04o}:{digest}"
        target = (
            private_entries
            if rel.parts and rel.parts[0] in EVALUATOR_ONLY_ROOTS
            else public_entries
        )
        target.append(entry)

    required_public = {"task.toml", "instruction.md"}
    present_public = {entry.split(":", 1)[0] for entry in public_entries}
    missing = required_public - present_public
    if missing:
        raise MaterialBoundaryError(
            f"task package lacks required public material: {sorted(missing)}"
        )
    if not private_entries:
        raise MaterialBoundaryError(
            "task package has no evaluator-only tests/solution material"
        )

    public_digest = canonical_digest(public_entries)
    private_digest = canonical_digest(private_entries)
    evaluator_digest = canonical_digest(
        {
            "public": public_digest,
            "evaluator_only": private_digest,
        }
    )
    # A one-way tripwire.  This value has no grading content, but if it ever
    # appears in a candidate payload we know the private manifest crossed the
    # boundary.  candidate_view() never serializes it.
    redaction_canary = canonical_digest(
        {
            "role": "evaluator-only-manifest-canary",
            "private_digest": private_digest,
        }
    )
    return TaskMaterialManifest(
        public_digest=public_digest,
        evaluator_digest=evaluator_digest,
        redaction_canary=redaction_canary,
        public_file_count=len(public_entries),
        evaluator_only_file_count=len(private_entries),
    )


def assert_candidate_path_boundary(
    checkout: Path,
    *,
    tasks_root: Path,
    artifacts_root: Path,
) -> None:
    """Reject overlapping roots and tracked symlinks into evaluator storage."""
    checkout = Path(checkout).expanduser().resolve()
    tasks_root = Path(tasks_root).expanduser().resolve()
    artifacts_root = Path(artifacts_root).expanduser().resolve()
    for label, protected in (
        ("task material", tasks_root),
        ("evaluation artifacts", artifacts_root),
    ):
        if _is_within(protected, checkout) or _is_within(checkout, protected):
            raise MaterialBoundaryError(f"candidate checkout and {label} root overlap")

    # Evaluation checkouts are clean Git commits.  Inspect tracked symlinks
    # only, avoiding .venv's legitimate link farm while still catching a
    # candidate commit that mounts a known evaluator path into its repo.
    try:
        proc = subprocess.run(
            ["git", "-C", str(checkout), "ls-files", "-s", "-z"],
            capture_output=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise MaterialBoundaryError(
            f"cannot audit candidate checkout symlinks: {checkout}"
        ) from exc
    for record in proc.stdout.split(b"\0"):
        if not record:
            continue
        try:
            metadata, encoded_path = record.split(b"\t", 1)
            mode = metadata.split(b" ", 1)[0]
            rel = Path(os.fsdecode(encoded_path))
        except (ValueError, UnicodeError) as exc:
            raise MaterialBoundaryError("malformed git index record") from exc
        if mode != b"120000":
            continue
        link = checkout / rel
        target = link.resolve(strict=False)
        if _is_within(target, tasks_root) or _is_within(target, artifacts_root):
            raise MaterialBoundaryError(
                f"candidate symlink {rel} targets evaluator-only storage"
            )
