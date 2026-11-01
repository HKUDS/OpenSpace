"""Frozen SWE candidate snapshot and Harbor model-delivery primitives."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from rsi_arena.benchmarks.swe.restore_tree import restore_candidate_tree
from rsi_arena.run_layout import (
    atomic_write_bytes,
    file_digest,
)

_MAX_FILES = 20_000
_MAX_DIRECTORIES = 4_000
_MAX_FILE_BYTES = 128 * 1024 * 1024
_MAX_TOTAL_BYTES = 512 * 1024 * 1024
_MAX_PATH_CHARS = 1024


class SweRuntimeError(RuntimeError):
    """The trusted SWE snapshot boundary failed."""


@dataclass(frozen=True)
class HarborModelDelivery:
    """Opaque operator model route delivered to the SWE Harbor agent."""

    model_name: str
    model_revision: str
    api_base_url: str
    provider_name: str
    credential_source_env: str
    credential_delivery_env: str

    @classmethod
    def provider(
        cls,
        *,
        model_name: str,
        model_revision: str,
        api_base_url: str,
        provider_name: str,
        credential_source_env: str,
        credential_delivery_env: str,
    ) -> HarborModelDelivery:
        values = tuple(
            str(value).strip()
            for value in (
                model_name,
                model_revision,
                api_base_url,
                provider_name,
                credential_source_env,
                credential_delivery_env,
            )
        )
        if not all(values):
            raise ValueError("Harbor model delivery fields must be non-empty")
        return cls(*values)

    def delivery_identity(self) -> dict[str, object]:
        return {
            "schema": "swe-harbor-model-delivery",
            "provider": self.provider_name,
            "delivered_model_name": self.model_name,
            "model_revision": self.model_revision,
            "api_base_url": self.api_base_url,
            "api_base_url_digest": _sha256_bytes(self.api_base_url.encode()),
            "credential_source_env": self.credential_source_env,
            "credential_delivery_env": self.credential_delivery_env,
            "credential_value_recorded": False,
        }


@dataclass(frozen=True)
class FrozenTaskSnapshot:
    task_id: str
    base_commit: str
    tree_path: Path
    tree_digest: str
    patch_path: Path
    patch_digest: str
    file_count: int
    total_bytes: int


@dataclass(frozen=True)
class TrustedGrade:
    score: float
    status: str = "measured"
    raw_metric_ref: str | None = None


class FrozenSnapshotGrader(Protocol):
    """Trusted grader boundary; implementations never receive a live repo."""

    @property
    def digest(self) -> str: ...

    def grade(self, snapshot: FrozenTaskSnapshot) -> TrustedGrade: ...


def _sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def instruction_digest(instruction: str) -> str:
    """Digest the exact UTF-8 instruction delivered to native mini."""

    return _sha256_bytes(str(instruction).encode("utf-8"))


def _trusted_tool_env() -> dict[str, str]:
    """Return a minimal trusted-tool environment without provider secrets."""

    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/dev/null",
        "LC_ALL": "C",
        "PYTHONNOUSERSITE": "1",
    }


def _git(
    repo: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/dev/null",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=env,
    )
    if check and result.returncode != 0:
        raise SweRuntimeError(
            f"trusted Git command failed: git {' '.join(args)}: "
            f"{result.stderr.strip()[:500]}"
        )
    return result


def _git_output(repo: Path, *args: str) -> str:
    return _git(repo, *args).stdout.strip()


def _clean_repo_identity(repo: Path) -> tuple[str, str]:
    repo = Path(repo).resolve(strict=True)
    top = Path(_git_output(repo, "rev-parse", "--show-toplevel")).resolve()
    if top != repo:
        raise SweRuntimeError("task repo must be a standalone Git worktree")
    if _git_output(repo, "status", "--porcelain=v1", "--untracked-files=all"):
        raise SweRuntimeError("fresh task repo is not clean")
    return (
        _git_output(repo, "rev-parse", "HEAD^{commit}"),
        _git_output(repo, "rev-parse", "HEAD^{tree}"),
    )


def _task_entries(
    root: Path,
) -> tuple[list[tuple[str, Path, os.stat_result, str | None]], int, int]:
    entries: list[tuple[str, Path, os.stat_result, str | None]] = []
    files = directories = total_bytes = 0
    root_resolved = root.resolve(strict=True)
    for path in sorted(
        root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()
    ):
        relative = path.relative_to(root).as_posix()
        if ".git" in PurePosixPath(relative).parts:
            continue
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            link_target = _validated_task_symlink_target(root_resolved, path)
            files += 1
            if files > _MAX_FILES:
                raise SweRuntimeError("task snapshot size/count limit exceeded")
        elif stat.S_ISDIR(info.st_mode):
            link_target = None
            directories += 1
            if directories > _MAX_DIRECTORIES:
                raise SweRuntimeError("task snapshot directory limit exceeded")
        elif stat.S_ISREG(info.st_mode):
            link_target = None
            if info.st_nlink != 1:
                raise SweRuntimeError(
                    f"task snapshot contains a hard-linked file: {relative}"
                )
            files += 1
            total_bytes += info.st_size
            if (
                files > _MAX_FILES
                or info.st_size > _MAX_FILE_BYTES
                or total_bytes > _MAX_TOTAL_BYTES
            ):
                raise SweRuntimeError("task snapshot size/count limit exceeded")
        else:
            raise SweRuntimeError(f"task snapshot contains a special file: {relative}")
        entries.append((relative, path, info, link_target))
    return entries, files, total_bytes


def _validated_task_symlink_target(root_resolved: Path, path: Path) -> str:
    try:
        target = os.readlink(path)
    except OSError as exc:
        raise SweRuntimeError("cannot read task snapshot symlink") from exc
    if (
        not target
        or len(target) > _MAX_PATH_CHARS
        or "\\" in target
        or any(ord(character) < 32 or ord(character) == 127 for character in target)
        or PurePosixPath(target).is_absolute()
    ):
        raise SweRuntimeError("task snapshot contains an unsafe symlink")
    try:
        lexical_parts = list(
            path.parent.resolve(strict=True).relative_to(root_resolved).parts
        )
        for part in PurePosixPath(target).parts:
            if part in ("", "."):
                continue
            if part == "..":
                if not lexical_parts:
                    raise ValueError("symbolic link escapes the task snapshot")
                lexical_parts.pop()
            else:
                lexical_parts.append(part)
        resolved = (path.parent / target).resolve(strict=True)
        relative_target = resolved.relative_to(root_resolved)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SweRuntimeError("task snapshot contains an unsafe symlink") from exc
    if ".git" in (*lexical_parts, *relative_target.parts):
        raise SweRuntimeError("task snapshot symlink targets verifier-owned Git state")
    return target


def _write_tree_tar(root: Path, destination: Path) -> tuple[int, int]:
    entries, files, total_bytes = _task_entries(root)
    temporary = destination.with_name(f".{destination.name}.tmp")
    with tarfile.open(temporary, "w", format=tarfile.GNU_FORMAT) as archive:
        root_resolved = root.resolve(strict=True)
        for relative, path, before, link_target in entries:
            current = path.lstat()
            if (
                current.st_dev != before.st_dev
                or current.st_ino != before.st_ino
                or current.st_size != before.st_size
                or current.st_nlink != before.st_nlink
                or current.st_mtime_ns != before.st_mtime_ns
                or current.st_ctime_ns != before.st_ctime_ns
                or stat.S_IFMT(current.st_mode) != stat.S_IFMT(before.st_mode)
            ):
                raise SweRuntimeError("task tree changed during freeze")
            if stat.S_ISLNK(current.st_mode):
                current_target = _validated_task_symlink_target(root_resolved, path)
                if current_target != link_target:
                    raise SweRuntimeError("task tree changed during freeze")
            item = tarfile.TarInfo(
                relative + ("/" if stat.S_ISDIR(current.st_mode) else "")
            )
            item.uid = item.gid = 0
            item.uname = item.gname = ""
            item.mtime = 0
            item.mode = (
                0o755
                if stat.S_ISDIR(current.st_mode)
                else (0o755 if current.st_mode & 0o111 else 0o644)
            )
            if stat.S_ISDIR(current.st_mode):
                item.type = tarfile.DIRTYPE
                item.size = 0
                archive.addfile(item)
            elif stat.S_ISLNK(current.st_mode):
                item.type = tarfile.SYMTYPE
                item.mode = 0o777
                item.size = 0
                item.linkname = link_target or ""
                archive.addfile(item)
            else:
                item.size = current.st_size
                with path.open("rb") as source:
                    archive.addfile(item, source)
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    os.replace(temporary, destination)
    return files, total_bytes


def _remove_worktree_files(repo: Path) -> None:
    for child in repo.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def _build_patch(
    base_bare: Path, base_commit: str, tree_tar: Path, output: Path
) -> None:
    with tempfile.TemporaryDirectory(prefix="rsi-arena-swe-patch-") as temp:
        worktree = Path(temp) / "repo"
        cloned = subprocess.run(
            ["git", "clone", "--no-hardlinks", "--", str(base_bare), str(worktree)],
            capture_output=True,
            text=True,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": "/dev/null",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_SYSTEM": "/dev/null",
            },
        )
        if cloned.returncode != 0:
            raise SweRuntimeError(
                "could not reconstruct trusted task base: "
                + cloned.stderr.strip()[:500]
            )
        if _git_output(worktree, "rev-parse", "HEAD^{commit}") != base_commit:
            raise SweRuntimeError("trusted task base commit moved")
        _remove_worktree_files(worktree)
        restore_candidate_tree(tree_tar, worktree)
        _git(worktree, "add", "-A", "--", ".")
        patch = _git(
            worktree,
            "diff",
            "--cached",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            "HEAD",
        ).stdout.encode()
        atomic_write_bytes(output, patch)


def freeze_task_snapshot(
    *,
    task_id: str,
    task_repo: Path,
    base_bare: Path,
    base_commit: str,
    destination: Path,
) -> FrozenTaskSnapshot:
    """Freeze a stopped task repo and derive a complete patch from trusted base."""

    task_repo = Path(task_repo).resolve(strict=True)
    destination.mkdir(mode=0o700, parents=True)
    tree_path = destination / "tree.tar"
    patch_path = destination / "candidate.patch"
    files, total_bytes = _write_tree_tar(task_repo, tree_path)
    _build_patch(base_bare, base_commit, tree_path, patch_path)
    return FrozenTaskSnapshot(
        task_id=str(task_id),
        base_commit=base_commit,
        tree_path=tree_path,
        tree_digest=file_digest(tree_path),
        patch_path=patch_path,
        patch_digest=file_digest(patch_path),
        file_count=files,
        total_bytes=total_bytes,
    )


def freeze_exported_task_snapshot(
    *,
    task_id: str,
    exported_tree_path: Path,
    base_bare: Path,
    base_commit: str,
    destination: Path,
) -> FrozenTaskSnapshot:
    """Admit a stopped Harbor tree export and derive its trusted Git patch.

    ``TreeSnapshotCodec`` already excludes verifier-owned ``.git`` state and
    writes a deterministic archive only after the task container is stopped.
    This helper is the physical-cell counterpart of
    :func:`freeze_task_snapshot`: it validates the exported archive by using
    the normal restore boundary, publishes it without overwriting an existing
    measurement, and derives the binary/full-index patch from the separately
    captured official base repository.
    """

    source = Path(exported_tree_path).resolve(strict=True)
    source_info = source.lstat()
    if not stat.S_ISREG(source_info.st_mode) or source_info.st_nlink != 1:
        raise SweRuntimeError("exported Harbor tree is not a regular file")
    destination = Path(destination).absolute()
    destination.mkdir(mode=0o700, parents=True)
    tree_path = destination / "tree.tar"
    patch_path = destination / "candidate.patch"
    if tree_path.exists() or patch_path.exists():
        raise SweRuntimeError("task measurement destination is not empty")

    temporary = tree_path.with_name(f".{tree_path.name}.tmp")
    try:
        with source.open("rb") as input_stream, temporary.open("xb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        os.replace(temporary, tree_path)
    finally:
        temporary.unlink(missing_ok=True)

    with tempfile.TemporaryDirectory(prefix="rsi-arena-swe-tree-validate-") as raw:
        restored = Path(raw) / "tree"
        cloned = subprocess.run(
            [
                "git",
                "clone",
                "--quiet",
                "--no-hardlinks",
                "--",
                str(base_bare),
                str(restored),
            ],
            capture_output=True,
            text=True,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": "/dev/null",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_SYSTEM": "/dev/null",
            },
        )
        if cloned.returncode != 0:
            raise SweRuntimeError("could not validate exported tree against base")
        restore_candidate_tree(tree_path, restored)
        _entries, files, total_bytes = _task_entries(restored)
    if _git_output(Path(base_bare), "rev-parse", "HEAD^{commit}") != base_commit:
        raise SweRuntimeError("trusted task base moved before patch derivation")
    _build_patch(Path(base_bare), base_commit, tree_path, patch_path)
    return FrozenTaskSnapshot(
        task_id=str(task_id),
        base_commit=str(base_commit),
        tree_path=tree_path,
        tree_digest=file_digest(tree_path),
        patch_path=patch_path,
        patch_digest=file_digest(patch_path),
        file_count=files,
        total_bytes=total_bytes,
    )


__all__ = [
    "FrozenTaskSnapshot",
    "HarborModelDelivery",
    "SweRuntimeError",
    "TrustedGrade",
    "freeze_exported_task_snapshot",
    "freeze_task_snapshot",
    "instruction_digest",
]
