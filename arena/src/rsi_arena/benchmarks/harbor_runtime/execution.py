"""Benchmark-neutral physical primitives for trusted Harbor task cells.

This module deliberately knows nothing about benchmark tasks, graders,
campaigns, or artifact schemas.  It identifies the selected Harbor executable,
turns a locally present Docker image into an immutable reference, stages one
private credential handle, and tears down Compose containers whose mounts prove
that they belong to one task cell.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
_REPO_DIGEST_RE = re.compile(r".+@sha256:[0-9a-f]{64}")
_DOCKER_INSPECT_MISSING_RE = re.compile(
    r"(?:Error: |Error response from daemon: )"
    r"No such (?:object|container): (?P<object_id>\S+)"
)


class HarborExecutionError(RuntimeError):
    """A trusted Harbor physical-boundary primitive failed closed."""


class _CredentialLogScanBudgetExceeded(HarborExecutionError):
    """The credential scan exceeded its budget after purging scoped outputs."""


@dataclass(frozen=True)
class HarborControllerIdentity:
    executable_sha256: str

    def as_dict(self) -> dict[str, str]:
        return {
            "schema": "rsi-arena-harbor-controller-runtime",
            "executable_sha256": self.executable_sha256,
        }


@dataclass(frozen=True)
class DockerImageInspection:
    image_id: str
    repo_digests: tuple[str, ...]

    def __post_init__(self) -> None:
        if _IMAGE_ID_RE.fullmatch(self.image_id) is None or any(
            _REPO_DIGEST_RE.fullmatch(item) is None for item in self.repo_digests
        ):
            raise HarborExecutionError("docker image identity is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "image_id": self.image_id,
            "repo_digests": list(self.repo_digests),
        }


@dataclass(frozen=True)
class ImmutableDockerImageIdentity:
    configured_ref: str
    image_id: str
    repo_digests: tuple[str, ...]
    runtime_ref: str

    def __post_init__(self) -> None:
        DockerImageInspection(self.image_id, self.repo_digests)
        if not self.configured_ref or self.runtime_ref not in {
            self.image_id,
            *self.repo_digests,
        }:
            raise HarborExecutionError("immutable docker image identity is malformed")

    @property
    def inspection(self) -> DockerImageInspection:
        return DockerImageInspection(self.image_id, self.repo_digests)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "rsi-arena-immutable-docker-image",
            "configured_ref": self.configured_ref,
            "image_id": self.image_id,
            "repo_digests": list(self.repo_digests),
            "runtime_ref": self.runtime_ref,
        }


def _trusted_docker_env(docker_host: str = "") -> dict[str, str]:
    environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    resolved_host = docker_host or os.environ.get("DOCKER_HOST", "")
    if resolved_host:
        environment["DOCKER_HOST"] = resolved_host
    if "DOCKER_CONFIG" in os.environ:
        environment["DOCKER_CONFIG"] = os.environ["DOCKER_CONFIG"]
    return environment


def harbor_controller_identity(trusted_harbor_root: Path) -> HarborControllerIdentity:
    """Bind the executable bytes actually selected for a Harbor task cell."""

    root = Path(trusted_harbor_root).expanduser().resolve(strict=True)
    harbor_bin = root / ".venv/bin/harbor"
    if not harbor_bin.is_file() or not os.access(harbor_bin, os.X_OK):
        raise HarborExecutionError("trusted Harbor executable is unavailable")
    return HarborControllerIdentity(
        executable_sha256="sha256:"
        + hashlib.sha256(harbor_bin.read_bytes()).hexdigest()
    )


def resolve_docker_executable(docker_path: str | Path) -> Path:
    resolved = shutil.which(str(docker_path))
    if resolved is None:
        raise HarborExecutionError("docker CLI is unavailable")
    return Path(resolved).resolve(strict=True)


def inspect_docker_image(
    docker: Path,
    image_ref: str,
    *,
    docker_host: str = "",
) -> DockerImageInspection:
    """Inspect one already-present image; never pull or build it."""

    result = subprocess.run(
        [str(docker), "image", "inspect", str(image_ref)],
        capture_output=True,
        text=True,
        timeout=60,
        env=_trusted_docker_env(docker_host),
    )
    if result.returncode != 0:
        raise HarborExecutionError("trusted docker image inspect failed")
    try:
        rows = json.loads(result.stdout)
        row = rows[0]
        image_id = row["Id"]
        repo_digests = tuple(sorted(row.get("RepoDigests") or ()))
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise HarborExecutionError("docker image identity is invalid") from exc
    if not isinstance(image_id, str) or not all(
        isinstance(item, str) for item in repo_digests
    ):
        raise HarborExecutionError("docker image identity is invalid")
    return DockerImageInspection(image_id=image_id, repo_digests=repo_digests)


ImageInspector = Callable[[Path, str], DockerImageInspection]


def resolve_immutable_docker_image(
    docker: Path,
    configured_ref: str,
    *,
    inspect: ImageInspector = inspect_docker_image,
) -> ImmutableDockerImageIdentity:
    """Resolve a configured ref and prove the selected immutable ref agrees."""

    if not isinstance(configured_ref, str) or not configured_ref:
        raise HarborExecutionError("configured docker image ref is missing")
    observed = inspect(docker, configured_ref)
    runtime_ref = (
        observed.repo_digests[0] if observed.repo_digests else observed.image_id
    )
    if inspect(docker, runtime_ref) != observed:
        raise HarborExecutionError("immutable runtime image identity disagrees")
    return ImmutableDockerImageIdentity(
        configured_ref=configured_ref,
        image_id=observed.image_id,
        repo_digests=observed.repo_digests,
        runtime_ref=runtime_ref,
    )


def revalidate_immutable_docker_image(
    docker: Path,
    identity: ImmutableDockerImageIdentity,
    *,
    inspect: ImageInspector = inspect_docker_image,
) -> None:
    """Fail closed if either the configured ref or immutable ref moved."""

    expected = identity.inspection
    if (
        inspect(docker, identity.configured_ref) != expected
        or inspect(docker, identity.runtime_ref) != expected
    ):
        raise HarborExecutionError("docker image identity moved after resolution")


def stage_credential_handle(cell_root: Path, value: str) -> Path:
    """Write one task-local 0600 handle, deleting partial files on failure."""

    if not isinstance(value, str) or not value or "\x00" in value:
        raise HarborExecutionError("provider credential is missing")
    root = Path(cell_root).resolve(strict=True)
    path = root / ("provider-credential-" + uuid.uuid4().hex)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        payload = value.encode("utf-8")
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short credential write")
            view = view[written:]
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        path.unlink(missing_ok=True)
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        validate_credential_handle(path)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def validate_credential_handle(path: Path) -> Path:
    """Validate a private, single-link, current-user credential file."""

    handle = Path(path)
    try:
        info = handle.lstat()
    except OSError as exc:
        raise HarborExecutionError("credential handle is unavailable") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
        or (hasattr(os, "getuid") and info.st_uid != os.getuid())
    ):
        raise HarborExecutionError("credential handle is unsafe")
    return handle.resolve(strict=True)


def remove_credential_handle(path: Path | None) -> None:
    if path is not None:
        Path(path).unlink(missing_ok=True)


def scrub_credential_literal_files(
    root: Path,
    paths: Iterable[Path],
    value: str,
    *,
    max_total_bytes: int = 512 * 1024 * 1024,
    max_paths: int = 20_000,
) -> None:
    """Delete candidate-writable files containing one exact credential value.

    The candidate process must already be gone.  This is defense in depth for
    logs/artifacts, not an attempt to stop an arbitrary encoded exfiltration.
    Unsafe, changing, or over-budget files are removed and fail closed too.
    """

    if not isinstance(value, str) or not value or "\x00" in value:
        raise HarborExecutionError("credential value is invalid")
    literal = value.encode("utf-8")
    raw_boundary = Path(root)
    boundary_info = raw_boundary.lstat()
    if not stat.S_ISDIR(boundary_info.st_mode) or stat.S_ISLNK(boundary_info.st_mode):
        raise HarborExecutionError("credential log root is unsafe")
    boundary = raw_boundary.resolve(strict=True)

    def purge_scoped_outputs() -> None:
        for child in boundary.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)

    violation = False
    total = 0
    count = 0
    overlap = max(0, len(literal) - 1)
    for supplied_path in paths:
        count += 1
        if count > max_paths:
            purge_scoped_outputs()
            error_type = (
                HarborExecutionError if violation else _CredentialLogScanBudgetExceeded
            )
            raise error_type("credential log scan exceeded its path budget")
        path = Path(supplied_path).absolute()
        try:
            relative_name = path.relative_to(boundary).as_posix()
        except ValueError:
            violation = True
            continue
        if value in relative_name:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
            violation = True
            continue
        try:
            info = path.lstat()
            path.resolve(strict=True).relative_to(boundary)
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            if path.is_symlink() or path.is_file():
                path.unlink(missing_ok=True)
            violation = True
            continue
        if stat.S_ISDIR(info.st_mode):
            continue
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_nlink != 1
        ):
            path.unlink(missing_ok=True)
            violation = True
            continue
        total += info.st_size
        if total > max_total_bytes:
            purge_scoped_outputs()
            error_type = (
                HarborExecutionError if violation else _CredentialLogScanBudgetExceeded
            )
            raise error_type("credential log scan exceeded its byte budget")
        leaked = False
        descriptor: int | None = None
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            opened = os.fstat(descriptor)
            if (
                opened.st_dev != info.st_dev
                or opened.st_ino != info.st_ino
                or opened.st_size != info.st_size
                or not stat.S_ISREG(opened.st_mode)
            ):
                raise OSError("credential log changed while opening")
            tail = b""
            while chunk := os.read(descriptor, 1024 * 1024):
                combined = tail + chunk
                if literal in combined:
                    leaked = True
                tail = combined[-overlap:] if overlap else b""
        except OSError:
            leaked = True
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if leaked:
            path.unlink(missing_ok=True)
            violation = True
    if violation:
        raise HarborExecutionError(
            "candidate-writable output contained or could conceal a credential"
        )


def docker_container_inventory(
    docker: Path, *, docker_host: str = ""
) -> list[dict[str, object]]:
    listed = subprocess.run(
        [str(docker), "ps", "-aq", "--filter", "label=com.docker.compose.project"],
        capture_output=True,
        text=True,
        timeout=60,
        env=_trusted_docker_env(docker_host),
    )
    if listed.returncode != 0:
        raise HarborExecutionError("could not inventory scoped Compose containers")
    container_ids = [row for row in listed.stdout.splitlines() if row]
    if not container_ids:
        return []
    inspected = subprocess.run(
        [str(docker), "inspect", *container_ids],
        capture_output=True,
        text=True,
        timeout=120,
        env=_trusted_docker_env(docker_host),
    )
    disappeared_ids: set[str] = set()
    if inspected.returncode != 0:
        requested_ids = set(container_ids)
        errors = [
            line.strip() for line in inspected.stderr.splitlines() if line.strip()
        ]
        for error in errors:
            match = _DOCKER_INSPECT_MISSING_RE.fullmatch(error)
            if match is None or match.group("object_id") not in requested_ids:
                raise HarborExecutionError(
                    "could not inspect scoped Compose containers"
                )
            disappeared_ids.add(match.group("object_id"))
        if not disappeared_ids:
            raise HarborExecutionError("could not inspect scoped Compose containers")
    output = inspected.stdout
    if not output.strip() and disappeared_ids == set(container_ids):
        output = "[]"
    try:
        rows = json.loads(output)
    except json.JSONDecodeError as exc:
        raise HarborExecutionError("Docker container inventory is invalid") from exc
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise HarborExecutionError("Docker container inventory is malformed")
    if disappeared_ids and len(rows) != len(container_ids) - len(disappeared_ids):
        raise HarborExecutionError("Docker container inventory is malformed")
    return rows


def scoped_compose_container_ids(
    inventory: Sequence[Mapping[str, object]],
    cell_root: Path,
    *,
    running_only: bool,
) -> tuple[str, ...]:
    """Select Compose projects anchored below one cell root.

    Harbor bind-mounts its logs into the separate official-results tree, so a
    task container need not have any mount below the runtime cell.  Compose's
    trusted project labels retain the runtime project directory and generated
    config-file paths; those are equally exact cell anchors.
    """

    root = Path(cell_root).resolve()

    def is_anchored(row: Mapping[str, object], labels: Mapping[object, object]) -> bool:
        mounts = row.get("Mounts")
        anchors = (
            [mount.get("Source") for mount in mounts if isinstance(mount, Mapping)]
            if isinstance(mounts, list)
            else []
        )
        anchors.append(labels.get("com.docker.compose.project.working_dir"))
        config_files = labels.get("com.docker.compose.project.config_files")
        if isinstance(config_files, str):
            anchors.extend(config_files.split(","))
        for source in anchors:
            if isinstance(source, str) and source:
                resolved = Path(source).resolve()
                if resolved == root or root in resolved.parents:
                    return True
        return False

    projects: set[str] = set()
    for row in inventory:
        mounts = row.get("Mounts")
        config = row.get("Config")
        labels = config.get("Labels") if isinstance(config, Mapping) else None
        if not isinstance(mounts, list) or not isinstance(labels, Mapping):
            continue
        project = labels.get("com.docker.compose.project")
        if not isinstance(project, str) or not project:
            continue
        if is_anchored(row, labels):
            projects.add(project)
    selected: list[str] = []
    for row in inventory:
        config = row.get("Config")
        labels = config.get("Labels") if isinstance(config, Mapping) else None
        state = row.get("State")
        container_id = row.get("Id")
        if (
            not isinstance(labels, Mapping)
            or labels.get("com.docker.compose.project") not in projects
            or not isinstance(container_id, str)
            or not container_id
        ):
            continue
        has_path_labels = (
            "com.docker.compose.project.working_dir" in labels
            or "com.docker.compose.project.config_files" in labels
        )
        if has_path_labels and not is_anchored(row, labels):
            continue
        if running_only and (
            not isinstance(state, Mapping) or state.get("Running") is not True
        ):
            continue
        selected.append(container_id)
    return tuple(sorted(set(selected)))


Inventory = Callable[[Path], list[dict[str, object]]]
Runner = Callable[..., subprocess.CompletedProcess[str]]


def cleanup_scoped_compose_cell(
    docker: Path,
    cell_root: Path,
    *,
    cleanup_grace_seconds: float,
    docker_host: str = "",
    inventory: Inventory = docker_container_inventory,
    run: Runner = subprocess.run,
) -> None:
    """Stop, remove, and verify disappearance of this cell's containers."""

    def current_inventory() -> list[dict[str, object]]:
        if inventory is docker_container_inventory:
            return inventory(docker, docker_host=docker_host)
        return inventory(docker)

    rows = current_inventory()
    scoped = scoped_compose_container_ids(rows, cell_root, running_only=False)
    running = scoped_compose_container_ids(rows, cell_root, running_only=True)
    stop_failed = False
    if running:
        stopped = run(
            [
                str(docker),
                "stop",
                "--time",
                str(max(1, int(cleanup_grace_seconds))),
                *running,
            ],
            capture_output=True,
            text=True,
            timeout=cleanup_grace_seconds + 60,
            env=_trusted_docker_env(docker_host),
        )
        stop_failed = stopped.returncode != 0
    remove_failed = False
    if scoped:
        removed = run(
            [str(docker), "rm", "-f", *scoped],
            capture_output=True,
            text=True,
            timeout=cleanup_grace_seconds + 60,
            env=_trusted_docker_env(docker_host),
        )
        remove_failed = removed.returncode != 0
    remaining = scoped_compose_container_ids(
        current_inventory(), cell_root, running_only=False
    )
    if remaining:
        raise HarborExecutionError("scoped Harbor containers remain after removal")
    if stop_failed or remove_failed:
        raise HarborExecutionError("scoped Harbor container cleanup reported failure")


def signal_process_group(process: subprocess.Popen[bytes], signal_number: int) -> None:
    try:
        os.killpg(process.pid, signal_number)
    except ProcessLookupError:
        pass


def terminate_process_group_and_cleanup(
    process: subprocess.Popen[bytes],
    *,
    docker: Path,
    cell_root: Path,
    cleanup_grace_seconds: float,
    docker_host: str = "",
    signal_group: Callable[[subprocess.Popen[bytes], int], None] = signal_process_group,
    cleanup: Callable[..., None] = cleanup_scoped_compose_cell,
) -> None:
    """Terminate Harbor's process group, then clean only its task cell."""

    try:
        if process.returncode is None:
            signal_group(process, signal.SIGTERM)
            try:
                process.wait(timeout=cleanup_grace_seconds)
            except subprocess.TimeoutExpired:
                signal_group(process, signal.SIGKILL)
                process.wait(timeout=30)
    finally:
        cleanup_kwargs: dict[str, object] = {
            "cleanup_grace_seconds": cleanup_grace_seconds
        }
        if docker_host:
            cleanup_kwargs["docker_host"] = docker_host
        cleanup(docker, cell_root, **cleanup_kwargs)


__all__ = [
    "DockerImageInspection",
    "HarborControllerIdentity",
    "HarborExecutionError",
    "ImmutableDockerImageIdentity",
    "cleanup_scoped_compose_cell",
    "docker_container_inventory",
    "harbor_controller_identity",
    "inspect_docker_image",
    "remove_credential_handle",
    "resolve_docker_executable",
    "resolve_immutable_docker_image",
    "revalidate_immutable_docker_image",
    "scoped_compose_container_ids",
    "scrub_credential_literal_files",
    "signal_process_group",
    "stage_credential_handle",
    "terminate_process_group_and_cleanup",
    "validate_credential_handle",
]
