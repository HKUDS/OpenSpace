"""Safely restore a candidate SWE working tree for a fresh verifier.

This module is copied into every derived SWE task's private ``tests/`` image.
The input archive is produced by the trusted agent bridge after the candidate
worker has stopped; it is not a candidate-declared score or verifier result.

Regular files, directories, and relative symbolic links resolving inside the
snapshot are accepted. Absolute, escaping, dangling, or cyclic links, devices,
traversing paths, ``.git`` entries, and over-budget archives are rejected. The fresh
checkout's own ``.git`` directory is retained so the upstream SWE verifier can
continue to reset test-patch paths and use the official grader unchanged.
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_TREE_BYTES = 2 * 1024 * 1024 * 1024
MAX_MEMBER_BYTES = 512 * 1024 * 1024
MAX_MEMBERS = 200_000
MAX_PATH_BYTES = 4096
_COPY_CHUNK_BYTES = 1024 * 1024


class UnsafeArchive(ValueError):
    """The bridge archive is not safe to replay in a trusted verifier."""


@dataclass(frozen=True)
class _Member:
    info: tarfile.TarInfo
    parts: tuple[str, ...]


def _normalise_member_path(name: str) -> tuple[str, ...]:
    if not isinstance(name, str) or not name or "\x00" in name:
        raise UnsafeArchive("archive member has an empty or NUL-containing path")
    if len(name.encode("utf-8", errors="surrogatepass")) > MAX_PATH_BYTES:
        raise UnsafeArchive("archive member path exceeds the byte limit")
    if "\\" in name:
        raise UnsafeArchive("backslashes are forbidden in archive member paths")
    raw = PurePosixPath(name)
    if raw.is_absolute():
        raise UnsafeArchive("absolute archive member path is forbidden")
    parts = tuple(part for part in raw.parts if part not in ("", "."))
    if any(part == ".." for part in parts):
        raise UnsafeArchive("archive member path traversal is forbidden")
    if ".git" in parts:
        raise UnsafeArchive("candidate archive may not include Git control state")
    return parts


def _normalise_link_target(
    target: str, parent_parts: tuple[str, ...]
) -> tuple[str, ...]:
    if (
        not isinstance(target, str)
        or not target
        or "\x00" in target
        or "\\" in target
        or any(ord(character) < 32 or ord(character) == 127 for character in target)
    ):
        raise UnsafeArchive("archive symbolic link has an invalid target")
    if len(target.encode("utf-8", errors="surrogatepass")) > MAX_PATH_BYTES:
        raise UnsafeArchive("archive symbolic link target exceeds the byte limit")
    raw = PurePosixPath(target)
    if raw.is_absolute():
        raise UnsafeArchive("absolute archive symbolic link target is forbidden")
    parts = list(parent_parts)
    for part in raw.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise UnsafeArchive("archive symbolic link escapes the snapshot")
            parts.pop()
        else:
            parts.append(part)
    if ".git" in parts:
        raise UnsafeArchive("archive symbolic link may not target pristine .git")
    return tuple(parts)


def _validate_members(members: list[tarfile.TarInfo]) -> list[_Member]:
    if len(members) > MAX_MEMBERS:
        raise UnsafeArchive("archive contains too many members")

    validated: list[_Member] = []
    seen: set[tuple[str, ...]] = set()
    total_size = 0
    for info in members:
        parts = _normalise_member_path(info.name)
        if not parts:
            if info.isdir():
                continue
            raise UnsafeArchive("archive root entry must be a directory")
        if parts in seen:
            raise UnsafeArchive("archive contains duplicate normalized paths")
        seen.add(parts)
        if not (info.isdir() or info.isfile() or info.issym()):
            raise UnsafeArchive("archive hard links and special files are forbidden")
        if info.size < 0 or info.size > MAX_MEMBER_BYTES:
            raise UnsafeArchive("archive member exceeds the per-file size limit")
        if info.issym() and info.size != 0:
            raise UnsafeArchive("archive symbolic link has a payload")
        total_size += info.size
        if total_size > MAX_TREE_BYTES:
            raise UnsafeArchive("archive exceeds the expanded tree size limit")
        if info.issym():
            _normalise_link_target(info.linkname, parts[:-1])
        validated.append(_Member(info=info, parts=parts))

    leaf_paths: set[tuple[str, ...]] = set()
    for member in sorted(validated, key=lambda item: len(item.parts)):
        if any(
            member.parts[:depth] in leaf_paths for depth in range(1, len(member.parts))
        ):
            raise UnsafeArchive("archive nests a member below a file or link")
        if member.info.isfile() or member.info.issym():
            leaf_paths.add(member.parts)
    return validated


def _open_archive(path: Path):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise UnsafeArchive("candidate tree archive is unavailable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise UnsafeArchive("candidate tree archive is not a regular file")
        if info.st_size > MAX_ARCHIVE_BYTES:
            raise UnsafeArchive(
                "candidate tree archive exceeds the compressed size limit"
            )
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _extract_validated(
    archive: tarfile.TarFile, members: list[_Member], destination: Path
) -> None:
    directories = sorted(
        (member for member in members if member.info.isdir()),
        key=lambda item: len(item.parts),
    )
    for member in directories:
        target = destination.joinpath(*member.parts)
        target.mkdir(parents=True, exist_ok=True)
        target.chmod(member.info.mode & 0o777)

    links = [member for member in members if member.info.issym()]
    for member in links:
        target = destination.joinpath(*member.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(member.info.linkname)

    root_resolved = destination.resolve(strict=True)
    for member in links:
        target = destination.joinpath(*member.parts)
        try:
            target.resolve(strict=True).relative_to(root_resolved)
        except (OSError, RuntimeError, ValueError) as exc:
            raise UnsafeArchive(
                "archive symbolic link is dangling, cyclic, or escapes the snapshot"
            ) from exc

    for member in members:
        if not member.info.isfile():
            continue
        target = destination.joinpath(*member.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        source = archive.extractfile(member.info)
        if source is None:
            raise UnsafeArchive("regular archive member has no readable payload")
        remaining = member.info.size
        with source, target.open("xb") as output:
            while remaining:
                chunk = source.read(min(_COPY_CHUNK_BYTES, remaining))
                if not chunk:
                    raise UnsafeArchive("archive member payload is truncated")
                output.write(chunk)
                remaining -= len(chunk)
        target.chmod(member.info.mode & 0o777)


def _assert_pristine_testbed(testbed: Path) -> None:
    try:
        testbed_info = testbed.lstat()
        git_info = (testbed / ".git").lstat()
    except FileNotFoundError as exc:
        raise UnsafeArchive(
            "fresh verifier checkout or pristine .git is missing"
        ) from exc
    if stat.S_ISLNK(testbed_info.st_mode) or not stat.S_ISDIR(testbed_info.st_mode):
        raise UnsafeArchive("verifier testbed is not a real directory")
    if stat.S_ISLNK(git_info.st_mode) or not stat.S_ISDIR(git_info.st_mode):
        raise UnsafeArchive("verifier .git is not a pristine directory")


def restore_candidate_tree(archive_path: Path, testbed: Path) -> None:
    """Validate *archive_path*, then replace *testbed* except for ``.git``."""

    archive_path = Path(archive_path)
    testbed = Path(testbed)
    _assert_pristine_testbed(testbed)
    stage = Path(tempfile.mkdtemp(prefix="rsi-arena-swe-tree-"))
    try:
        try:
            with _open_archive(archive_path) as stream:
                with tarfile.open(fileobj=stream, mode="r:*") as archive:
                    members = _validate_members(list(archive))
                    _extract_validated(archive, members, stage)
        except (tarfile.TarError, OSError) as exc:
            raise UnsafeArchive("candidate tree archive is malformed") from exc

        # Validation and extraction complete before the fresh checkout changes.
        for child in testbed.iterdir():
            if child.name == ".git":
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        for child in stage.iterdir():
            shutil.move(str(child), testbed / child.name)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "archive",
        nargs="?",
        default="/logs/artifacts/rsi-arena-tree.tar",
    )
    parser.add_argument("testbed", nargs="?", default="/testbed")
    args = parser.parse_args()
    try:
        restore_candidate_tree(Path(args.archive), Path(args.testbed))
    except UnsafeArchive as exc:
        print(f"unsafe candidate tree: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
