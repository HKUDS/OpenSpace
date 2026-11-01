"""Trusted materialisation boundary for external RSI baselines.

External frameworks produce ordinary directory trees.  A physical bench must
not evaluate those trees directly: Git may silently ignore files, a directory
may inherit an unrelated ancestor repository, and an in-place evaluator gives
the framework an opportunity to change the tree while it is being scored.

``TrustedEvaluationGateway`` fixes those problems by rebuilding every proposal
from one pinned trusted commit, staging *all* filesystem differences (including
ignored and untracked files), applying Arena's normal scope/secret/object
gate, and issuing an opaque reference to a clean commit.  Physical evaluation
evaluation accepts only references issued by the same gateway and creates a
new detached checkout for every cell.

The gateway is a trusted host component.  Candidate code should receive
neither the gateway object nor its private control repository.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator

from rsi_arena.contracts import ScopeSpec, canonical_digest
from rsi_arena.gitops import WorktreeManager, run_gate


class GatewayError(RuntimeError):
    """Base class for trusted-gateway failures."""


class GatewayRejected(GatewayError):
    """A candidate-controlled tree failed materialisation or the scope gate."""

    def __init__(self, message: str, *, violations: Iterable[str] = ()) -> None:
        self.violations = tuple(str(item) for item in violations)
        suffix = ": " + "; ".join(self.violations) if self.violations else ""
        super().__init__(message + suffix)


class GatewayCandidateRequired(GatewayError):
    """A physical evaluator was given an unauthenticated directory/commit."""


@dataclass(frozen=True)
class GatewayCandidate:
    """Opaque, process-local capability for one admitted candidate commit.

    The fields are useful trusted-side provenance, but are not authentication:
    the issuing gateway also retains the exact object in a private registry.
    Reconstructing this dataclass, even with identical field values, does not
    create a valid capability.
    """

    checkout_path: Path
    commit_sha: str
    tree_sha: str
    root_commit_sha: str
    scope_digest: str
    gateway_id: str
    issue_id: str


def _git_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_NAME": "rsi-arena-baseline-gateway",
        "GIT_AUTHOR_EMAIL": "rsi-arena-baseline-gateway@localhost",
        "GIT_COMMITTER_NAME": "rsi-arena-baseline-gateway",
        "GIT_COMMITTER_EMAIL": "rsi-arena-baseline-gateway@localhost",
    }


def _git(
    cwd: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=_git_env(),
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise GatewayError(
            f"trusted Git operation failed: git {' '.join(args)}: "
            f"{proc.stderr.strip()[:500]}"
        )
    return proc


def _git_output(cwd: Path, *args: str) -> str:
    return _git(cwd, *args).stdout.strip()


def clean_standalone_git_identity(root: Path) -> tuple[str, str]:
    """Return ``(commit, tree)`` for an exact, fully clean worktree root.

    This intentionally checks ``--show-toplevel``.  Merely running
    ``rev-parse HEAD`` in a directory is unsafe because Git walks upward and
    may return an unrelated ancestor repository's identity.

    This is the formal trusted clone, not a developer checkout: ignored local
    state such as an in-tree ``.venv`` is deliberately rejected and should be
    kept outside this dedicated root.
    """

    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise GatewayError("trusted root is not a directory")
    top = Path(_git_output(root, "rev-parse", "--show-toplevel")).resolve()
    if top != root:
        raise GatewayError("trusted root must itself be a standalone Git worktree root")
    status_text = _git_output(
        root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    if status_text:
        raise GatewayError("trusted root must be a clean fixed commit checkout")
    ignored = _git_output(
        root,
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "-z",
        "--",
        ".",
    )
    if ignored:
        raise GatewayError(
            "trusted root must not contain ignored files outside its commit"
        )
    tracked = _git_output(root, "ls-files", "-v", "-z", "--", ".")
    rows = [row for row in tracked.split("\0") if row]
    if any(not row.startswith("H ") for row in rows):
        raise GatewayError("trusted root refuses assume-unchanged/skip-worktree files")
    commit_sha = _git_output(root, "rev-parse", "HEAD^{commit}")
    tree_sha = _git_output(root, "rev-parse", "HEAD^{tree}")
    if len(commit_sha) != 40 or len(tree_sha) != 40:
        raise GatewayError("trusted root Git identity is malformed")
    return commit_sha, tree_sha


def _paths_overlap(left: Path, right: Path) -> bool:
    left = left.resolve()
    right = right.resolve()
    return left == right or left in right.parents or right in left.parents


def _remove_entry(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
)
_SOURCE_FILE_OPEN_FLAGS = (
    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
)
_GENERATED_CACHE_DIRECTORY_NAMES = frozenset({"__pycache__", ".pytest_cache"})


def _stat_fingerprint(info: os.stat_result) -> tuple[int, ...]:
    """Fields that must stay fixed while an untrusted entry is ingested."""

    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _same_object(left: os.stat_result, right: os.stat_result) -> bool:
    return _stat_fingerprint(left) == _stat_fingerprint(right)


def _open_candidate_root(source_tree: Path) -> tuple[Path, Path, int, os.stat_result]:
    """Pin a candidate root FD and prove resolution names that same inode."""

    source_path = Path(source_tree).expanduser().absolute()
    try:
        before = source_path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
            raise GatewayRejected(
                "candidate source must be a real directory, not a link"
            )
        descriptor = os.open(source_path, _DIRECTORY_OPEN_FLAGS)
    except GatewayRejected:
        raise
    except OSError as exc:
        raise GatewayRejected("candidate source could not be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode) or not _same_object(before, opened):
            raise GatewayRejected("candidate source changed while it was opened")
        try:
            resolved = source_path.resolve(strict=True)
            resolved_info = resolved.stat()
        except OSError as exc:
            raise GatewayRejected(
                "candidate source changed during path resolution"
            ) from exc
        if not _same_object(opened, resolved_info):
            raise GatewayRejected(
                "candidate source path no longer identifies the opened directory"
            )
        return source_path, resolved, descriptor, opened
    except BaseException:
        os.close(descriptor)
        raise


def _assert_entry_stable(
    directory_fd: int,
    name: str,
    expected: os.stat_result,
    *,
    relative: str,
) -> None:
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise GatewayRejected(
            f"candidate entry changed during ingestion: {relative}"
        ) from exc
    if not _same_object(expected, current):
        raise GatewayRejected(f"candidate entry changed during ingestion: {relative}")


def _copy_regular_from_fd(
    source_parent_fd: int,
    destination_parent_fd: int,
    name: str,
    before: os.stat_result,
    *,
    relative: str,
) -> None:
    try:
        source_fd = os.open(name, _SOURCE_FILE_OPEN_FLAGS, dir_fd=source_parent_fd)
    except OSError as exc:
        raise GatewayRejected(
            f"candidate regular file could not be opened safely: {relative}"
        ) from exc
    destination_fd: int | None = None
    try:
        opened = os.fstat(source_fd)
        if not _same_object(before, opened) or not stat.S_ISREG(opened.st_mode):
            raise GatewayRejected(
                f"candidate regular file changed before copy: {relative}"
            )
        if opened.st_nlink != 1:
            raise GatewayRejected(f"candidate contains a hard-linked file: {relative}")
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
                    raise GatewayRejected(
                        f"short write while copying candidate file: {relative}"
                    )
                view = view[written:]
        after = os.fstat(source_fd)
        if not _same_object(opened, after):
            raise GatewayRejected(
                f"candidate regular file changed during copy: {relative}"
            )
        # Never chmod a destination path: only the newly created regular FD.
        os.fchmod(destination_fd, opened.st_mode & 0o777)
        _assert_entry_stable(source_parent_fd, name, opened, relative=relative)
    except GatewayRejected:
        raise
    except OSError as exc:
        raise GatewayRejected(
            f"candidate regular file copy failed safely: {relative}"
        ) from exc
    finally:
        os.close(source_fd)
        if destination_fd is not None:
            os.close(destination_fd)


def _copy_candidate_directory(
    source_fd: int,
    destination_fd: int,
    *,
    relative_parts: tuple[str, ...] = (),
) -> None:
    """Copy one stable FD-pinned tree without following candidate paths."""

    directory_before = os.fstat(source_fd)
    try:
        names = sorted(os.listdir(source_fd))
        entries = {
            name: os.stat(name, dir_fd=source_fd, follow_symlinks=False)
            for name in names
        }
    except OSError as exc:
        raise GatewayRejected("candidate directory could not be listed safely") from exc

    for name in names:
        parts = (*relative_parts, name)
        relative = "/".join(parts)
        before = entries[name]
        if name == ".rsi-arena" and not relative_parts:
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
                raise GatewayRejected("candidate Arena mirror is not a real directory")
            continue
        if name == ".git":
            if relative_parts:
                raise GatewayRejected("candidate contains nested .git metadata")
            # Candidate Git metadata is inert: never invoke it or follow its
            # pointers. Normal repository directories and worktree marker
            # files are excluded; links and special objects fail closed.
            if not (stat.S_ISDIR(before.st_mode) or stat.S_ISREG(before.st_mode)):
                raise GatewayRejected("candidate .git marker is not regular metadata")
            continue
        if name in _GENERATED_CACHE_DIRECTORY_NAMES and stat.S_ISDIR(before.st_mode):
            continue

        if stat.S_ISDIR(before.st_mode):
            try:
                child_source_fd = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=source_fd)
            except OSError as exc:
                raise GatewayRejected(
                    f"candidate directory changed before copy: {relative}"
                ) from exc
            child_destination_fd: int | None = None
            try:
                opened = os.fstat(child_source_fd)
                if not _same_object(before, opened):
                    raise GatewayRejected(
                        f"candidate directory changed before copy: {relative}"
                    )
                os.mkdir(name, mode=0o700, dir_fd=destination_fd)
                child_destination_fd = os.open(
                    name, _DIRECTORY_OPEN_FLAGS, dir_fd=destination_fd
                )
                _copy_candidate_directory(
                    child_source_fd,
                    child_destination_fd,
                    relative_parts=parts,
                )
                if not _same_object(opened, os.fstat(child_source_fd)):
                    raise GatewayRejected(
                        f"candidate directory changed during copy: {relative}"
                    )
                _assert_entry_stable(source_fd, name, opened, relative=relative)
            except GatewayRejected:
                raise
            except OSError as exc:
                raise GatewayRejected(
                    f"candidate directory copy failed safely: {relative}"
                ) from exc
            finally:
                os.close(child_source_fd)
                if child_destination_fd is not None:
                    os.close(child_destination_fd)
            continue

        if stat.S_ISREG(before.st_mode):
            _copy_regular_from_fd(
                source_fd,
                destination_fd,
                name,
                before,
                relative=relative,
            )
            continue

        if stat.S_ISLNK(before.st_mode):
            try:
                target = os.readlink(name, dir_fd=source_fd)
                _assert_entry_stable(source_fd, name, before, relative=relative)
                os.symlink(target, name, dir_fd=destination_fd)
            except GatewayRejected:
                raise
            except OSError as exc:
                raise GatewayRejected(
                    f"candidate symbolic link changed during copy: {relative}"
                ) from exc
            continue

        raise GatewayRejected(
            f"candidate contains a special filesystem object: {relative}"
        )

    try:
        names_after = sorted(os.listdir(source_fd))
        directory_after = os.fstat(source_fd)
    except OSError as exc:
        raise GatewayRejected("candidate directory changed during ingestion") from exc
    if names_after != names or not _same_object(directory_before, directory_after):
        raise GatewayRejected("candidate directory changed during ingestion")
    for name, expected in entries.items():
        _assert_entry_stable(
            source_fd,
            name,
            expected,
            relative="/".join((*relative_parts, name)),
        )


class TrustedEvaluationGateway:
    """Materialise and authenticate candidates for physical evaluation."""

    def __init__(
        self,
        *,
        trusted_root: Path,
        scope: ScopeSpec,
        work_dir: Path,
        expected_root_commit: str | None = None,
        sensitive_literals: Iterable[str] = (),
    ) -> None:
        self.trusted_root = Path(trusted_root).expanduser().resolve()
        self.scope = scope
        self.scope_digest = canonical_digest(scope.as_dict())
        self.work_dir = Path(work_dir).expanduser().resolve()
        if _paths_overlap(self.trusted_root, self.work_dir):
            raise GatewayError("gateway work_dir and trusted root must be disjoint")
        self.root_commit_sha, self.root_tree_sha = clean_standalone_git_identity(
            self.trusted_root
        )
        if (
            expected_root_commit is not None
            and expected_root_commit != self.root_commit_sha
        ):
            raise GatewayError("trusted root moved after gateway configuration")

        self.work_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._instance = self.work_dir / f"gateway-{uuid.uuid4().hex}"
        self._instance.mkdir(mode=0o700)
        self._control = self._instance / "control.git"
        clone = subprocess.run(
            [
                "git",
                "clone",
                "--bare",
                "--no-hardlinks",
                "--",
                str(self.trusted_root),
                str(self._control),
            ],
            cwd=str(self._instance),
            env=_git_env(),
            capture_output=True,
            text=True,
        )
        if clone.returncode != 0:
            raise GatewayError(
                "could not create private gateway repository: "
                + clone.stderr.strip()[:500]
            )
        if (
            _git_output(self._control, "rev-parse", f"{self.root_commit_sha}^{{tree}}")
            != self.root_tree_sha
        ):
            raise GatewayError("private gateway clone does not match trusted root")

        self._staging_root = self._instance / "staging"
        self._issued_root = self._instance / "issued"
        self._evaluation_root = self._instance / "evaluation"
        for path in (self._staging_root, self._issued_root, self._evaluation_root):
            path.mkdir(mode=0o700)
        self._manager = WorktreeManager(self._control, self._instance / "worktrees")
        self._git_lock = threading.Lock()
        self._issue_lock = threading.Lock()
        self._issued: dict[str, GatewayCandidate] = {}
        self._gateway_id = uuid.uuid4().hex
        self._sensitive_literals = tuple(
            value for value in sensitive_literals if isinstance(value, str) and value
        )

    def materialize(
        self,
        source_tree: Path,
        *,
        before_gate: Callable[[Path, str, str, str], None] | None = None,
    ) -> GatewayCandidate:
        """Build a gated commit from an untrusted physical directory tree.

        ``before_gate`` is the trusted campaign recorder seam.  When supplied,
        it durably binds the exact staged commit before the candidate gate can
        reject it.  Other gateway users need no recorder and leave it unset.
        """

        source_path, source_identity, source_fd, source_opened = _open_candidate_root(
            Path(source_tree)
        )
        try:
            if _paths_overlap(source_identity, self._instance):
                raise GatewayRejected("candidate source overlaps gateway private state")
            issue_id = uuid.uuid4().hex
            staging = self._staging_root / issue_id
            with self._git_lock:
                self._manager.checkout_commit(self.root_commit_sha, staging)
            try:
                for child in list(staging.iterdir()):
                    if child.name != ".git":
                        _remove_entry(child)
                try:
                    destination_fd = os.open(staging, _DIRECTORY_OPEN_FLAGS)
                except OSError as exc:
                    raise GatewayError(
                        "trusted staging directory could not be opened safely"
                    ) from exc
                try:
                    _copy_candidate_directory(source_fd, destination_fd)
                finally:
                    os.close(destination_fd)
                try:
                    current_root = source_path.lstat()
                except OSError as exc:
                    raise GatewayRejected(
                        "candidate source changed during ingestion"
                    ) from exc
                if not _same_object(
                    source_opened, os.fstat(source_fd)
                ) or not _same_object(source_opened, current_root):
                    raise GatewayRejected("candidate source changed during ingestion")

                # ``-f`` is intentional: ignored and untracked candidate files
                # are evidence, not exclusions. The subsequent gate decides
                # whether those objects are admissible.
                _git(staging, "add", "-A", "-f", "--", ".")
                changed_files = self._manager.staged_files(staging)
                diff_text = self._manager.staged_diff(staging)

                def require_gate() -> None:
                    try:
                        gate = run_gate(
                            changed_files,
                            diff_text,
                            self.scope,
                            worktree=staging,
                            sensitive_literals=self._sensitive_literals,
                        )
                    except (OSError, ValueError) as exc:
                        raise GatewayError(
                            "trusted gate could not inspect candidate"
                        ) from exc
                    if not gate.ok:
                        raise GatewayRejected(
                            "candidate rejected by trusted gate",
                            violations=gate.violations,
                        )

                if before_gate is None:
                    require_gate()
                with self._git_lock:
                    commit_sha = self._manager.commit_candidate(
                        staging, f"external baseline candidate {issue_id}"
                    )
                if commit_sha is None:
                    commit_sha = self.root_commit_sha
                tree_sha = _git_output(
                    self._control, "rev-parse", f"{commit_sha}^{{tree}}"
                )
                if before_gate is not None:
                    before_gate(
                        staging,
                        commit_sha,
                        tree_sha,
                        self.root_commit_sha,
                    )
                    # The commit only freezes submitted bytes; reservation is
                    # durable before this gate decides whether to admit them.
                    require_gate()

                issued_path = self._issued_root / issue_id
                with self._git_lock:
                    self._manager.checkout_commit(commit_sha, issued_path)
                candidate = GatewayCandidate(
                    checkout_path=issued_path,
                    commit_sha=commit_sha,
                    tree_sha=tree_sha,
                    root_commit_sha=self.root_commit_sha,
                    scope_digest=self.scope_digest,
                    gateway_id=self._gateway_id,
                    issue_id=issue_id,
                )
                with self._issue_lock:
                    self._issued[issue_id] = candidate
                self.validate(candidate)
                return candidate
            finally:
                with self._git_lock:
                    self._manager.remove(staging)
        finally:
            os.close(source_fd)

    def validate(self, candidate: GatewayCandidate) -> None:
        """Fail closed unless ``candidate`` is this gateway's unchanged output."""

        if not isinstance(candidate, GatewayCandidate):
            raise GatewayCandidateRequired(
                "physical evaluation requires a trusted gateway candidate"
            )
        with self._issue_lock:
            issued = self._issued.get(candidate.issue_id)
        if issued is not candidate:
            raise GatewayCandidateRequired(
                "candidate capability was not issued by this gateway"
            )
        if (
            candidate.gateway_id != self._gateway_id
            or candidate.root_commit_sha != self.root_commit_sha
            or candidate.scope_digest != self.scope_digest
        ):
            raise GatewayCandidateRequired("candidate capability identity mismatch")

        path = candidate.checkout_path.resolve()
        top = Path(_git_output(path, "rev-parse", "--show-toplevel")).resolve()
        if top != path:
            raise GatewayCandidateRequired(
                "issued candidate is not a standalone Git worktree root"
            )
        status_text = _git_output(
            path,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=matching",
        )
        if status_text:
            raise GatewayCandidateRequired("issued candidate checkout is not clean")
        commit_sha = _git_output(path, "rev-parse", "HEAD^{commit}")
        tree_sha = _git_output(path, "rev-parse", "HEAD^{tree}")
        if commit_sha != candidate.commit_sha or tree_sha != candidate.tree_sha:
            raise GatewayCandidateRequired("issued candidate Git identity changed")

        ancestor = _git(
            self._control,
            "merge-base",
            "--is-ancestor",
            self.root_commit_sha,
            candidate.commit_sha,
            check=False,
        )
        if ancestor.returncode != 0:
            raise GatewayCandidateRequired(
                "candidate commit is not descended from the fixed trusted root"
            )
        row = _git_output(
            self._control, "rev-list", "--parents", "-n", "1", candidate.commit_sha
        ).split()
        if candidate.commit_sha == self.root_commit_sha:
            # The fixed root may naturally have its own trusted history.
            ancestry_valid = bool(row) and row[0] == self.root_commit_sha
        else:
            # Gateway-created candidates are always a single commit directly
            # on the fixed root; no framework-selected ancestry is accepted.
            ancestry_valid = row == [candidate.commit_sha, self.root_commit_sha]
        if not ancestry_valid:
            raise GatewayCandidateRequired(
                "candidate commit has an unexpected ancestor identity"
            )

    @contextmanager
    def fresh_checkout(self, candidate: GatewayCandidate) -> Iterator[Path]:
        """Yield one disposable detached checkout of an issued commit."""

        self.validate(candidate)
        checkout = self._evaluation_root / f"cell-{uuid.uuid4().hex}"
        with self._git_lock:
            self._manager.checkout_commit(candidate.commit_sha, checkout)
        try:
            top = Path(_git_output(checkout, "rev-parse", "--show-toplevel")).resolve()
            if top != checkout.resolve():
                raise GatewayCandidateRequired(
                    "fresh evaluation checkout inherited ancestor Git identity"
                )
            if _git_output(
                checkout,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignored=matching",
            ):
                raise GatewayCandidateRequired("fresh evaluation checkout is not clean")
            if (
                _git_output(checkout, "rev-parse", "HEAD^{commit}")
                != candidate.commit_sha
            ):
                raise GatewayCandidateRequired("fresh checkout commit mismatch")
            yield checkout
        finally:
            with self._git_lock:
                self._manager.remove(checkout)
