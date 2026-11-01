"""Shared baseline infrastructure for usage accounting and safe snapshots.

Doc 16 §6 makes fine-grained accounting a hard requirement: every LLM call
lands as its own row (aggregates only are forbidden), split by role so the
report can answer "did the money go into editing code or into evaluating
it".
"""

from __future__ import annotations

import os
import posixpath
import shutil
import stat
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from rsi_arena.run_layout import AppendOnlyJsonl, read_jsonl

ROLES = ("proposer/meta", "coding", "solver-agent", "verifier/judge")


@dataclass(frozen=True)
class UsageRow:
    """One LLM call (or one attributable slice of agent/verifier spend)."""

    ts: float
    method: str
    run_id: str
    role: str  # one of ROLES
    model: str
    provider: str = "unknown"
    attempt_id: Optional[str] = None
    candidate_id: Optional[str] = None
    iteration: Optional[int] = None
    evaluation_id: Optional[str] = None
    benchmark_id: Optional[str] = None
    benchmark_evaluation_id: Optional[str] = None
    task_id: Optional[str] = None
    replicate: Optional[int] = None
    measurement_seed: Optional[int] = None
    request_id: Optional[str] = None
    call_id: Optional[str] = None
    logical_call_ordinal: Optional[int] = None
    provider_response_id: Optional[str] = None
    provider_generation_id: Optional[str] = None
    provider_call_status: Optional[str] = None
    session_id: Optional[str] = None
    segment_id: Optional[str] = None
    message_id: Optional[str] = None
    usage_scope: Optional[str] = None
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    thinking_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    cache_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    wall_seconds: float = 0.0
    latency_ms: Optional[float] = None
    retry_count: int = 0
    failure_status: Optional[str] = None
    cache_status: str = "unknown"
    token_source: str = "unknown"
    token_status: str = "missing"
    input_token_status: str = "missing"
    output_token_status: str = "missing"
    cache_token_status: str = "missing"
    provider_reported_cost: Optional[float] = None
    cost_source: str = "unknown"
    cost_status: str = "missing"
    spend_kind: str = "search"


class UsageLedger:
    """Single-writer, fsync'd usage ledger with independent token/cost facts."""

    def __init__(self, path: Path, *, method: str, run_id: str):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._method = method
        self._run_id = run_id
        self._writer = AppendOnlyJsonl(self._path)

    def close(self) -> None:
        self._writer.close()

    @property
    def path(self) -> Path:
        return self._path

    def record(self, role: str, model: str, **fields) -> UsageRow:
        if role not in ROLES:
            raise ValueError(f"unknown ledger role {role!r}; use one of {ROLES}")
        normalized = dict(fields)
        token_fields = (
            "tokens_in",
            "tokens_out",
            "thinking_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "cache_tokens",
        )
        tokens_present = any(name in normalized for name in token_fields)
        for name in token_fields:
            if name not in normalized:
                continue
            value = normalized[name]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"invalid usage token field {name}: {value!r}")
        normalized.setdefault("token_source", "cli" if tokens_present else "unknown")
        both_primary_tokens = all(
            name in normalized for name in ("tokens_in", "tokens_out")
        )
        normalized.setdefault(
            "token_status",
            (
                "reported"
                if both_primary_tokens
                else "unverified"
                if tokens_present
                else "missing"
            ),
        )
        token_status = str(normalized["token_status"])
        if token_status not in {"reported", "recounted", "missing", "unverified"}:
            raise ValueError(f"invalid usage token status: {token_status!r}")
        if token_status in {"reported", "recounted"} and not both_primary_tokens:
            raise ValueError("reported usage requires separate input/output tokens")
        primary_status = token_status if token_status != "missing" else "missing"
        normalized.setdefault(
            "input_token_status",
            primary_status if "tokens_in" in normalized else "missing",
        )
        normalized.setdefault(
            "output_token_status",
            primary_status if "tokens_out" in normalized else "missing",
        )
        normalized.setdefault(
            "cache_token_status",
            (
                primary_status
                if "cache_read_tokens" in normalized
                or "cache_write_tokens" in normalized
                or "cache_tokens" in normalized
                else "missing"
            ),
        )
        normalized.setdefault("cost_source", "unknown")
        normalized.setdefault("cost_usd", None)
        normalized.setdefault(
            "cost_status",
            (
                "reported"
                if normalized["cost_usd"] is not None
                and normalized["cost_source"] in {"openrouter", "provider", "cli"}
                else "missing"
            ),
        )
        normalized.setdefault(
            "provider_reported_cost",
            (
                normalized.get("cost_usd")
                if normalized["cost_status"] == "reported"
                else None
            ),
        )
        if normalized["cost_status"] == "missing":
            normalized["provider_reported_cost"] = None
        normalized.setdefault(
            "spend_kind", "evaluation" if role == "verifier/judge" else "search"
        )
        row = UsageRow(
            ts=time.time(),
            method=self._method,
            run_id=self._run_id,
            role=role,
            model=model,
            **normalized,
        )
        payload = asdict(row)
        payload.update(
            {
                "timestamp": row.ts,
                "input_tokens": row.tokens_in,
                "output_tokens": row.tokens_out,
                "total_tokens": (
                    row.tokens_in + row.tokens_out
                    if row.tokens_in is not None and row.tokens_out is not None
                    else None
                ),
                # Accounting/price-snapshot cost is deliberately separate
                # from the provider-reported observation.
                "accounting_cost_usd": row.cost_usd,
            }
        )
        self._writer.append(payload)
        return row

    def total_cost_usd(self) -> float:
        if not self._path.exists():
            return 0.0
        total = 0.0
        for row in read_jsonl(self._path):
            value = row.get("accounting_cost_usd", row.get("cost_usd"))
            if value is not None:
                total += float(value)
        return total


class SnapshotAcquisitionError(RuntimeError):
    """A live candidate tree could not be copied without following it."""


_SNAPSHOT_CACHE_NAMES = frozenset(
    {".venv", "__pycache__", "node_modules", ".pytest_cache", ".ruff_cache"}
)
_SNAPSHOT_DIRECTORY_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
)
_SNAPSHOT_SOURCE_FILE_FLAGS = (
    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
)


def _snapshot_fingerprint(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _snapshot_identity(info: os.stat_result) -> tuple[int, int, int]:
    return (info.st_dev, info.st_ino, info.st_mode)


def _snapshot_paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _open_snapshot_root(
    worktree: Path,
) -> tuple[Path, Path, int, os.stat_result]:
    source_path = Path(worktree).expanduser().absolute()
    try:
        before = source_path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
            raise SnapshotAcquisitionError(
                "snapshot source must be a real directory, not a link"
            )
        descriptor = os.open(source_path, _SNAPSHOT_DIRECTORY_FLAGS)
    except SnapshotAcquisitionError:
        raise
    except OSError as exc:
        raise SnapshotAcquisitionError(
            "snapshot source could not be opened safely"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if _snapshot_fingerprint(before) != _snapshot_fingerprint(opened):
            raise SnapshotAcquisitionError(
                "snapshot source changed while it was opened"
            )
        try:
            resolved = source_path.resolve(strict=True)
            resolved_info = resolved.stat()
        except OSError as exc:
            raise SnapshotAcquisitionError(
                "snapshot source changed during path resolution"
            ) from exc
        if _snapshot_fingerprint(opened) != _snapshot_fingerprint(resolved_info):
            raise SnapshotAcquisitionError(
                "snapshot source path changed during acquisition"
            )
        return source_path, resolved, descriptor, opened
    except BaseException:
        os.close(descriptor)
        raise


def _snapshot_relative(parts: tuple[str, ...]) -> str:
    return "/".join(parts)


def _snapshot_is_excluded(
    name: str,
    info: os.stat_result,
    *,
    depth: int,
    relative: str,
) -> bool:
    if name == ".git":
        if depth:
            raise SnapshotAcquisitionError(
                f"snapshot source contains nested .git metadata: {relative}"
            )
        excluded = True
    elif name == ".rsi-arena" and depth == 0:
        excluded = True
    else:
        excluded = name in _SNAPSHOT_CACHE_NAMES
    if not excluded:
        return False
    if stat.S_ISLNK(info.st_mode):
        raise SnapshotAcquisitionError(
            f"excluded snapshot path is a symbolic link: {relative}"
        )
    if stat.S_ISREG(info.st_mode):
        if info.st_nlink != 1:
            raise SnapshotAcquisitionError(
                f"excluded snapshot path is hard-linked: {relative}"
            )
        return True
    if stat.S_ISDIR(info.st_mode):
        return True
    raise SnapshotAcquisitionError(
        f"excluded snapshot path is a special filesystem object: {relative}"
    )


def _snapshot_assert_entry(
    directory_fd: int,
    name: str,
    expected: os.stat_result,
    *,
    relative: str,
    directory: bool = False,
) -> None:
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise SnapshotAcquisitionError(
            f"snapshot source entry changed during copy: {relative}"
        ) from exc
    before = (
        _snapshot_identity(expected) if directory else _snapshot_fingerprint(expected)
    )
    after = _snapshot_identity(current) if directory else _snapshot_fingerprint(current)
    if before != after:
        raise SnapshotAcquisitionError(
            f"snapshot source entry changed during copy: {relative}"
        )


def _snapshot_copy_regular(
    source_parent_fd: int,
    destination_parent_fd: int,
    name: str,
    before: os.stat_result,
    *,
    relative: str,
) -> None:
    try:
        source_fd = os.open(name, _SNAPSHOT_SOURCE_FILE_FLAGS, dir_fd=source_parent_fd)
    except OSError as exc:
        raise SnapshotAcquisitionError(
            f"snapshot source file could not be opened safely: {relative}"
        ) from exc
    destination_fd: int | None = None
    try:
        opened = os.fstat(source_fd)
        if _snapshot_fingerprint(before) != _snapshot_fingerprint(
            opened
        ) or not stat.S_ISREG(opened.st_mode):
            raise SnapshotAcquisitionError(
                f"snapshot source file changed before copy: {relative}"
            )
        if opened.st_nlink != 1:
            raise SnapshotAcquisitionError(
                f"snapshot source contains a hard-linked file: {relative}"
            )
        destination_flags = (
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        )
        destination_fd = os.open(
            name,
            destination_flags,
            0o600,
            dir_fd=destination_parent_fd,
        )
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise SnapshotAcquisitionError(
                        f"short write while acquiring snapshot: {relative}"
                    )
                view = view[written:]
        after = os.fstat(source_fd)
        if _snapshot_fingerprint(opened) != _snapshot_fingerprint(after):
            raise SnapshotAcquisitionError(
                f"snapshot source file changed during copy: {relative}"
            )
        os.fchmod(destination_fd, opened.st_mode & 0o777)
        _snapshot_assert_entry(
            source_parent_fd,
            name,
            opened,
            relative=relative,
        )
    except SnapshotAcquisitionError:
        raise
    except OSError as exc:
        raise SnapshotAcquisitionError(
            f"snapshot file copy failed safely: {relative}"
        ) from exc
    finally:
        os.close(source_fd)
        if destination_fd is not None:
            os.close(destination_fd)


def _snapshot_copy_directory(
    source_fd: int,
    destination_fd: int,
    *,
    relative_parts: tuple[str, ...] = (),
    allow_safe_symlinks: bool = False,
) -> None:
    directory_before = os.fstat(source_fd)
    try:
        names = sorted(os.listdir(source_fd))
        entries = {
            name: os.stat(name, dir_fd=source_fd, follow_symlinks=False)
            for name in names
        }
    except OSError as exc:
        raise SnapshotAcquisitionError(
            "snapshot source directory could not be listed safely"
        ) from exc

    relevant: dict[str, os.stat_result] = {}
    for name in names:
        parts = (*relative_parts, name)
        relative = _snapshot_relative(parts)
        before = entries[name]
        if _snapshot_is_excluded(
            name,
            before,
            depth=len(relative_parts),
            relative=relative,
        ):
            continue
        relevant[name] = before

        if stat.S_ISLNK(before.st_mode):
            if not allow_safe_symlinks:
                raise SnapshotAcquisitionError(
                    f"snapshot source contains a symbolic link: {relative}"
                )
            try:
                target = os.readlink(name, dir_fd=source_fd)
            except OSError as exc:
                raise SnapshotAcquisitionError(
                    f"snapshot symbolic link changed before copy: {relative}"
                ) from exc
            resolved = posixpath.normpath(posixpath.join(*relative_parts, target))
            resolved_parts = tuple(
                part for part in resolved.split("/") if part not in {"", "."}
            )
            if (
                not target
                or target.startswith("/")
                or resolved == ".."
                or resolved.startswith("../")
                or any(
                    part in _SNAPSHOT_CACHE_NAMES or part in {".git", ".rsi-arena"}
                    for part in resolved_parts
                )
            ):
                raise SnapshotAcquisitionError(
                    "snapshot symbolic link escapes its visible tree: " + relative
                )
            _snapshot_assert_entry(source_fd, name, before, relative=relative)
            try:
                os.symlink(target, name, dir_fd=destination_fd)
            except OSError as exc:
                raise SnapshotAcquisitionError(
                    f"snapshot symbolic link copy failed safely: {relative}"
                ) from exc
            continue
        if stat.S_ISDIR(before.st_mode):
            try:
                child_source_fd = os.open(
                    name, _SNAPSHOT_DIRECTORY_FLAGS, dir_fd=source_fd
                )
            except OSError as exc:
                raise SnapshotAcquisitionError(
                    f"snapshot source directory changed before copy: {relative}"
                ) from exc
            child_destination_fd: int | None = None
            try:
                opened = os.fstat(child_source_fd)
                if _snapshot_identity(before) != _snapshot_identity(opened):
                    raise SnapshotAcquisitionError(
                        f"snapshot source directory changed before copy: {relative}"
                    )
                os.mkdir(name, mode=0o700, dir_fd=destination_fd)
                child_destination_fd = os.open(
                    name, _SNAPSHOT_DIRECTORY_FLAGS, dir_fd=destination_fd
                )
                _snapshot_copy_directory(
                    child_source_fd,
                    child_destination_fd,
                    relative_parts=parts,
                    allow_safe_symlinks=allow_safe_symlinks,
                )
                if _snapshot_identity(opened) != _snapshot_identity(
                    os.fstat(child_source_fd)
                ):
                    raise SnapshotAcquisitionError(
                        f"snapshot source directory changed during copy: {relative}"
                    )
                _snapshot_assert_entry(
                    source_fd,
                    name,
                    opened,
                    relative=relative,
                    directory=True,
                )
            except SnapshotAcquisitionError:
                raise
            except OSError as exc:
                raise SnapshotAcquisitionError(
                    f"snapshot directory copy failed safely: {relative}"
                ) from exc
            finally:
                os.close(child_source_fd)
                if child_destination_fd is not None:
                    os.close(child_destination_fd)
            continue
        if stat.S_ISREG(before.st_mode):
            _snapshot_copy_regular(
                source_fd,
                destination_fd,
                name,
                before,
                relative=relative,
            )
            continue
        raise SnapshotAcquisitionError(
            f"snapshot source contains a special filesystem object: {relative}"
        )

    try:
        names_after = sorted(os.listdir(source_fd))
        entries_after = {
            name: os.stat(name, dir_fd=source_fd, follow_symlinks=False)
            for name in names_after
        }
    except OSError as exc:
        raise SnapshotAcquisitionError(
            "snapshot source directory changed during copy"
        ) from exc
    relevant_after: dict[str, os.stat_result] = {}
    for name in names_after:
        relative = _snapshot_relative((*relative_parts, name))
        info = entries_after[name]
        if not _snapshot_is_excluded(
            name,
            info,
            depth=len(relative_parts),
            relative=relative,
        ):
            relevant_after[name] = info
    if sorted(relevant_after) != sorted(relevant):
        raise SnapshotAcquisitionError(
            "snapshot source directory entries changed during copy"
        )
    for name, expected in relevant.items():
        relative = _snapshot_relative((*relative_parts, name))
        current = relevant_after[name]
        directory = stat.S_ISDIR(expected.st_mode)
        before_value = (
            _snapshot_identity(expected)
            if directory
            else _snapshot_fingerprint(expected)
        )
        after_value = (
            _snapshot_identity(current) if directory else _snapshot_fingerprint(current)
        )
        if before_value != after_value:
            raise SnapshotAcquisitionError(
                f"snapshot source entry changed during copy: {relative}"
            )
    if _snapshot_identity(directory_before) != _snapshot_identity(os.fstat(source_fd)):
        raise SnapshotAcquisitionError(
            "snapshot source directory identity changed during copy"
        )


def _validate_snapshot_symlinks(root: Path) -> None:
    """Check resolved file targets in the private copy before publishing it.

    Lexical bounds alone cannot constrain a chain containing intermediate
    symlinks and ``..``. Directory links are intentionally unsupported: they
    can introduce recursive traversal cycles even when each target is local.
    """
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in (*directories, *files):
            path = Path(directory) / name
            if not path.is_symlink():
                continue
            relative = path.relative_to(root)
            try:
                target = path.resolve(strict=True)
                target_parts = target.relative_to(root).parts
                safe = target.is_file() and not any(
                    part in _SNAPSHOT_CACHE_NAMES or part in {".git", ".rsi-arena"}
                    for part in target_parts
                )
            except (OSError, RuntimeError, ValueError):
                safe = False
            if not safe:
                raise SnapshotAcquisitionError(
                    f"snapshot symbolic link must resolve to a regular file inside its visible tree: {relative}"
                )


def _remove_snapshot_path(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def snapshot_worktree(
    worktree: Path, dest: Path, *, allow_safe_symlinks: bool = False
) -> Path:
    """Acquire an atomic, stable regular-file snapshot for evaluation.

    The live CLI may mutate ``worktree`` while this runs. Source objects are
    therefore pinned with directory FDs and ``O_NOFOLLOW``; hardlinks, special
    objects, nested Git metadata and entry mutations fail closed. Symbolic links
    fail by default; the opt-in mode preserves only relative links resolving
    to regular files inside the evaluator-visible tree.
    Cache directories, the method-visible ``.rsi-arena`` mirror, and the
    top-level ``.git`` metadata remain excluded.
    No evaluator-visible destination appears until the complete tree passes
    all stability checks.
    """

    source_path, source_identity, source_fd, source_opened = _open_snapshot_root(
        Path(worktree)
    )
    temporary: Path | None = None
    try:
        destination_input = Path(dest).expanduser().absolute()
        try:
            destination_parent = destination_input.parent.resolve(strict=True)
        except OSError as exc:
            raise SnapshotAcquisitionError(
                "snapshot destination parent must already exist"
            ) from exc
        destination = destination_parent / destination_input.name
        if _snapshot_paths_overlap(source_identity, destination):
            raise SnapshotAcquisitionError(
                "snapshot destination must be disjoint from the live worktree"
            )
        _remove_snapshot_path(destination)
        temporary = destination_parent / (
            f".{destination.name}.snapshot-{uuid.uuid4().hex}"
        )
        temporary.mkdir(mode=0o700)
        try:
            destination_fd = os.open(temporary, _SNAPSHOT_DIRECTORY_FLAGS)
        except OSError as exc:
            raise SnapshotAcquisitionError(
                "snapshot temporary destination could not be opened safely"
            ) from exc
        try:
            _snapshot_copy_directory(
                source_fd,
                destination_fd,
                allow_safe_symlinks=allow_safe_symlinks,
            )
        finally:
            os.close(destination_fd)
        if allow_safe_symlinks:
            _validate_snapshot_symlinks(temporary)
        try:
            source_current = source_path.lstat()
        except OSError as exc:
            raise SnapshotAcquisitionError(
                "snapshot source root changed during acquisition"
            ) from exc
        if _snapshot_identity(source_opened) != _snapshot_identity(
            os.fstat(source_fd)
        ) or _snapshot_identity(source_opened) != _snapshot_identity(source_current):
            raise SnapshotAcquisitionError(
                "snapshot source root changed during acquisition"
            )
        os.rename(temporary, destination)
        temporary = None
        return destination
    except SnapshotAcquisitionError:
        raise
    except OSError as exc:
        raise SnapshotAcquisitionError("snapshot acquisition failed safely") from exc
    finally:
        os.close(source_fd)
        if temporary is not None:
            try:
                _remove_snapshot_path(temporary)
            except OSError:
                pass
