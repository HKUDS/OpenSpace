"""Descriptor-relative, bounded reads of Harbor trial artifacts."""

from __future__ import annotations

import os
import stat
from pathlib import Path


class TrialFileBoundaryError(ValueError):
    """A trial artifact was unsafe, stale, non-regular, or over its cap."""


class TrialFileMissing(FileNotFoundError):
    """An optional trial artifact or one of its directories is absent."""


def read_bounded_trial_file(
    trial_dir: str | Path,
    relative_path: str,
    *,
    maximum: int,
    not_before_ns: int | None = None,
) -> bytes:
    """Read a regular trial file through one no-follow descriptor chain.

    Every directory component and the final file are opened with
    ``O_NOFOLLOW``.  The regular-file/freshness checks and bounded read use the
    same final descriptor, so replacement cannot redirect a trusted consumer
    to a host path between validation and reading. ``O_NONBLOCK`` keeps a FIFO
    from hanging before ``fstat`` rejects it.
    """

    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 0:
        raise ValueError("maximum must be a non-negative integer")
    if not_before_ns is not None and (
        isinstance(not_before_ns, bool)
        or not isinstance(not_before_ns, int)
        or not_before_ns < 0
    ):
        raise ValueError("not_before_ns must be a non-negative integer or null")
    parts = tuple(part for part in relative_path.split("/") if part)
    if not parts or any(part in {".", ".."} for part in parts):
        raise ValueError("trial artifact path is invalid")
    directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    directory_fds: list[int] = []
    file_fd: int | None = None
    try:
        try:
            directory_fds.append(os.open(Path(trial_dir), directory_flags))
            for part in parts[:-1]:
                directory_fds.append(
                    os.open(part, directory_flags, dir_fd=directory_fds[-1])
                )
            file_fd = os.open(parts[-1], file_flags, dir_fd=directory_fds[-1])
        except FileNotFoundError as exc:
            raise TrialFileMissing(relative_path) from exc
        except OSError as exc:
            raise TrialFileBoundaryError("trial artifact path is unsafe") from exc
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            raise TrialFileBoundaryError("trial artifact is not a regular file")
        if info.st_size > maximum:
            raise TrialFileBoundaryError("trial artifact exceeds its byte cap")
        if not_before_ns is not None and info.st_mtime_ns < not_before_ns:
            raise TrialFileBoundaryError("trial artifact predates this cell")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(file_fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > maximum:
            raise TrialFileBoundaryError("trial artifact exceeds its byte cap")
        after = os.fstat(file_fd)
        if (
            after.st_dev != info.st_dev
            or after.st_ino != info.st_ino
            or after.st_size != info.st_size
            or after.st_mtime_ns != info.st_mtime_ns
        ):
            raise TrialFileBoundaryError("trial artifact changed while reading")
        return payload
    except TrialFileMissing:
        raise
    except OSError as exc:
        raise TrialFileBoundaryError("trial artifact could not be read") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        for descriptor in reversed(directory_fds):
            os.close(descriptor)


__all__ = [
    "TrialFileBoundaryError",
    "TrialFileMissing",
    "read_bounded_trial_file",
]
