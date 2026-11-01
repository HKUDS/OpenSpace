"""Thin trusted Docker source/grader seam for one frozen SWE task.

Docker authority stays in this controller module.  A candidate receives only a
fresh export of the official image's ``/testbed`` and the public instruction.
After the candidate process is gone, :mod:`rsi_arena.benchmarks.swe.runtime` freezes
the complete tree/patch and ``OfficialSweDockerGrader`` gives that immutable
tree to the existing separate-verifier package.  The grader never consumes a
candidate-declared score.

The module intentionally does not schedule campaigns or build a service.  It
is a one-task primitive for a controlled probe and later recorder-owned cells.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import tomllib

from rsi_arena.benchmarks.harbor_runtime.execution import (
    DockerImageInspection,
    HarborExecutionError,
    ImmutableDockerImageIdentity,
    inspect_docker_image,
    resolve_immutable_docker_image,
    revalidate_immutable_docker_image,
)
from rsi_arena.benchmarks.harbor_runtime.materials import snapshot_task_materials
from rsi_arena.benchmarks.swe.runtime import (
    FrozenTaskSnapshot,
    TrustedGrade,
    instruction_digest,
)
from rsi_arena.contracts import canonical_digest

SWE_GRADER_VERSION = "4.0.3"
SWE_GRADER_SOURCE_COMMIT = "dc4c087"
OFFICIAL_GRADING_IMPORTS = (
    "swebench.harness.grading.get_logs_eval",
    "swebench.harness.grading.get_eval_tests_report",
    "swebench.harness.grading.get_resolution_status",
)
_FROM_RE = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?([^\s]+)", re.IGNORECASE)
_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
_REPO_DIGEST_RE = re.compile(r".+@sha256:[0-9a-f]{64}")
_GIT_OBJECT_RE = re.compile(r"[0-9a-f]{40}")


class OfficialSweDockerError(RuntimeError):
    """The trusted official source/grader path failed closed."""


def _trusted_git_env() -> dict[str, str]:
    """Return a Git-only environment that excludes provider credentials."""

    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/dev/null",
        "LC_ALL": "C",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }


@dataclass(frozen=True)
class OfficialSweTaskIdentity:
    task_id: str
    instance_id: str
    repo: str
    base_commit: str
    source_image: str
    verifier_base_image: str
    public_material_digest: str
    evaluator_material_digest: str
    verifier_tree_digest: str
    instruction_digest: str
    transform_identity: str
    swebench_version: str = SWE_GRADER_VERSION
    swebench_source_commit: str = SWE_GRADER_SOURCE_COMMIT

    @property
    def static_evaluator_digest(self) -> str:
        return canonical_digest(
            {
                "schema": "swe-docker-grader",
                **self.__dict__,
                "official_grading_imports": list(OFFICIAL_GRADING_IMPORTS),
                "controller_implementation_digest": "sha256:"
                + hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            }
        )


@dataclass(frozen=True)
class SwePackagedRuntimeIdentity:
    """Immutable image and packaged Git tree actually used by one task cell."""

    source_image: str
    source_image_id: str
    source_image_repo_digests: tuple[str, ...]
    runtime_image_ref: str
    packaged_head: str
    packaged_tree: str

    def __post_init__(self) -> None:
        if (
            not self.source_image
            or _IMAGE_ID_RE.fullmatch(self.source_image_id) is None
            or any(
                _REPO_DIGEST_RE.fullmatch(item) is None
                for item in self.source_image_repo_digests
            )
            or self.runtime_image_ref
            not in {self.source_image_id, *self.source_image_repo_digests}
            or _GIT_OBJECT_RE.fullmatch(self.packaged_head) is None
            or _GIT_OBJECT_RE.fullmatch(self.packaged_tree) is None
        ):
            raise OfficialSweDockerError("SWE runtime-use identity is malformed")

    def payload(self) -> dict[str, object]:
        return {
            "schema": "swe-packaged-runtime",
            "source_image": self.source_image,
            "source_image_id": self.source_image_id,
            "source_image_repo_digests": list(self.source_image_repo_digests),
            "runtime_image_ref": self.runtime_image_ref,
            "packaged_head": self.packaged_head,
            "packaged_tree": self.packaged_tree,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self.payload())

    def as_dict(self) -> dict[str, object]:
        return {**self.payload(), "runtime_identity_digest": self.digest}

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> SwePackagedRuntimeIdentity:
        repo_digests = raw.get("source_image_repo_digests")
        required_strings = (
            "source_image",
            "source_image_id",
            "runtime_image_ref",
            "packaged_head",
            "packaged_tree",
            "runtime_identity_digest",
        )
        if (
            raw.get("schema") != "swe-packaged-runtime"
            or any(
                not isinstance(raw.get(name), str) or not raw[name]
                for name in required_strings
            )
            or not isinstance(repo_digests, list)
            or not all(isinstance(item, str) for item in repo_digests)
        ):
            raise OfficialSweDockerError("runtime-use identity evidence is malformed")
        identity = cls(
            source_image=str(raw["source_image"]),
            source_image_id=str(raw["source_image_id"]),
            source_image_repo_digests=tuple(repo_digests),
            runtime_image_ref=str(raw["runtime_image_ref"]),
            packaged_head=str(raw["packaged_head"]),
            packaged_tree=str(raw["packaged_tree"]),
        )
        if raw["runtime_identity_digest"] != identity.digest:
            raise OfficialSweDockerError("runtime-use identity digest disagrees")
        return identity


@dataclass(frozen=True)
class OfficialSourceExport:
    """Actual immutable image and packaged Git baseline exported for a cell."""

    task_id: str
    repo: str
    source_image: str
    source_image_id: str
    source_image_repo_digests: tuple[str, ...]
    runtime_image_ref: str
    instruction_digest: str
    task_static_evaluator_digest: str
    repo_head: str
    repo_tree: str
    runtime_identity_digest: str
    exported_at: float

    def binding(self) -> dict[str, object]:
        return {
            "schema": "swe-source-export",
            "task_id": self.task_id,
            "repo": self.repo,
            "source_image": self.source_image,
            "source_image_id": self.source_image_id,
            "source_image_repo_digests": list(self.source_image_repo_digests),
            "runtime_image_ref": self.runtime_image_ref,
            "instruction_digest": self.instruction_digest,
            "task_static_evaluator_digest": self.task_static_evaluator_digest,
            "repo_head": self.repo_head,
            "repo_tree": self.repo_tree,
            "runtime_identity_digest": self.runtime_identity_digest,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self.binding())

    def as_dict(self) -> dict[str, object]:
        return {
            **self.binding(),
            "source_export_digest": self.digest,
            "exported_at": self.exported_at,
        }

    def runtime_identity(self) -> SwePackagedRuntimeIdentity:
        identity = SwePackagedRuntimeIdentity(
            source_image=self.source_image,
            source_image_id=self.source_image_id,
            source_image_repo_digests=self.source_image_repo_digests,
            runtime_image_ref=self.runtime_image_ref,
            packaged_head=self.repo_head,
            packaged_tree=self.repo_tree,
        )
        if identity.digest != self.runtime_identity_digest:
            raise OfficialSweDockerError("source runtime identity digest moved")
        return identity


def _dockerfile_base(path: Path) -> str:
    try:
        text = path.read_text()
    except OSError as exc:
        raise OfficialSweDockerError(f"cannot read Dockerfile: {path}") from exc
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        matched = _FROM_RE.match(line)
        if matched:
            return matched.group(1)
    raise OfficialSweDockerError(f"Dockerfile has no base image: {path}")


def _regular_tree_digest(root: Path) -> str:
    rows: list[tuple[str, str, str]] = []
    for path in sorted(
        root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()
    ):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise OfficialSweDockerError(f"verifier tree contains symlink: {relative}")
        if stat.S_ISDIR(info.st_mode):
            rows.append((relative, "dir", ""))
        elif stat.S_ISREG(info.st_mode):
            rows.append(
                (relative, "file", hashlib.sha256(path.read_bytes()).hexdigest())
            )
        else:
            raise OfficialSweDockerError(
                f"verifier tree contains special file: {relative}"
            )
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(rows, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
    )


def inspect_official_swe_task(
    task_dir: Path,
    *,
    expected_task_id: str | None = None,
    expected_repo: str | None = None,
) -> OfficialSweTaskIdentity:
    """Statically bind the frozen public source and private grader materials."""

    task_dir = Path(task_dir).expanduser().resolve(strict=True)
    try:
        task_config = tomllib.loads((task_dir / "task.toml").read_text())
        private_config = json.loads((task_dir / "tests" / "config.json").read_text())
    except (OSError, tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
        raise OfficialSweDockerError("invalid frozen SWE task package") from exc
    verifier = task_config.get("verifier") or {}
    agent = task_config.get("agent") or {}
    task_section = task_config.get("task") or {}
    if verifier.get("environment_mode") != "separate":
        raise OfficialSweDockerError("official SWE grader must use a separate verifier")
    if agent.get("network_mode") != "no-network":
        raise OfficialSweDockerError("frozen SWE task no longer declares agent offline")
    if task_config.get("artifacts") != ["/logs/artifacts"]:
        raise OfficialSweDockerError("frozen SWE artifact contract changed")
    required = ("instance_id", "repo", "base_commit")
    if any(not isinstance(private_config.get(key), str) for key in required):
        raise OfficialSweDockerError("official SWE grader config lacks identity fields")
    instance_id = private_config["instance_id"]
    if (
        task_dir.name != instance_id
        or task_section.get("name") != f"swe-bench/{instance_id}"
    ):
        raise OfficialSweDockerError("task package/config identity disagreement")
    if expected_task_id is not None and instance_id != str(expected_task_id):
        raise OfficialSweDockerError(
            "official SWE task is not the explicitly requested task"
        )
    if expected_repo is not None and private_config["repo"] != str(expected_repo):
        raise OfficialSweDockerError(
            "official SWE repo is not the explicitly requested repo"
        )
    forbidden = ("patch", "hints_text", "problem_statement")
    if any(key in private_config for key in forbidden):
        raise OfficialSweDockerError("answer-like field remains in verifier config")
    upstream_test = (task_dir / "tests" / "upstream-test.sh").read_text()
    for imported in ("get_logs_eval", "get_eval_tests_report", "get_resolution_status"):
        if imported not in upstream_test:
            raise OfficialSweDockerError(
                f"official SWE grading import is missing: {imported}"
            )
    if f"swebench=={SWE_GRADER_VERSION}" not in upstream_test:
        raise OfficialSweDockerError("official SWE grader version pin moved")
    source_image = _dockerfile_base(task_dir / "environment" / "Dockerfile")
    verifier_image = _dockerfile_base(task_dir / "tests" / "Dockerfile")
    if source_image != verifier_image:
        raise OfficialSweDockerError("source and verifier base images disagree")
    materials = snapshot_task_materials(task_dir)
    return OfficialSweTaskIdentity(
        task_id=task_dir.name,
        instance_id=private_config["instance_id"],
        repo=private_config["repo"],
        base_commit=private_config["base_commit"],
        source_image=source_image,
        verifier_base_image=verifier_image,
        public_material_digest=materials.public_digest,
        evaluator_material_digest=materials.evaluator_digest,
        verifier_tree_digest=_regular_tree_digest(task_dir / "tests"),
        instruction_digest=instruction_digest(
            (task_dir / "instruction.md").read_text()
        ),
        transform_identity="swe-separate-verifier",
    )


def _docker(
    docker_path: Path,
    *args: str,
    timeout: float,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [str(docker_path), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            **(
                {"DOCKER_HOST": os.environ["DOCKER_HOST"]}
                if "DOCKER_HOST" in os.environ
                else {}
            ),
            **(
                {"DOCKER_CONFIG": os.environ["DOCKER_CONFIG"]}
                if "DOCKER_CONFIG" in os.environ
                else {}
            ),
        },
    )
    if check and result.returncode != 0:
        raise OfficialSweDockerError(
            f"trusted docker {' '.join(args[:3])} failed: "
            f"{(result.stderr or result.stdout).strip()[:1000]}"
        )
    return result


def _resolve_docker(docker_path: str | Path) -> Path:
    resolved = shutil.which(str(docker_path))
    if resolved is None:
        raise OfficialSweDockerError("docker CLI is unavailable")
    return Path(resolved).resolve(strict=True)


def _inspect_image(docker: Path, image: str) -> DockerImageInspection:
    try:
        return inspect_docker_image(docker, image)
    except HarborExecutionError as exc:
        raise OfficialSweDockerError(str(exc)) from exc


def _git_command(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=60,
        env=_trusted_git_env(),
    )


def _git_output(repo: Path, *args: str) -> str:
    result = _git_command(repo, *args)
    if result.returncode != 0:
        raise OfficialSweDockerError("exported runtime Git identity is invalid")
    return result.stdout.strip()


def _inspect_clean_packaged_repo(repo: Path) -> tuple[str, str]:
    """Return the packaged HEAD/tree of one clean standalone Git export."""

    repo = Path(repo).resolve(strict=True)
    git_metadata = repo / ".git"
    try:
        metadata_info = git_metadata.lstat()
    except OSError as exc:
        raise OfficialSweDockerError(
            "exported official task repo is not clean standalone Git"
        ) from exc
    if not stat.S_ISDIR(metadata_info.st_mode):
        raise OfficialSweDockerError(
            "exported official task repo is not clean standalone Git"
        )
    status = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env=_trusted_git_env(),
    )
    if status.returncode != 0 or status.stdout.strip():
        raise OfficialSweDockerError(
            "exported official task repo is not clean standalone Git"
        )
    top = _git_output(repo, "rev-parse", "--show-toplevel")
    if Path(top).resolve() != repo:
        raise OfficialSweDockerError("exported task inherited another Git repo")
    if Path(_git_output(repo, "rev-parse", "--absolute-git-dir")).resolve() != (
        git_metadata.resolve()
    ):
        raise OfficialSweDockerError(
            "exported official task repo is not standalone Git"
        )
    return (
        _git_output(repo, "rev-parse", "HEAD^{commit}"),
        _git_output(repo, "rev-parse", "HEAD^{tree}"),
    )


def _normalize_packaged_repo(repo: Path, base_commit: str) -> tuple[str, str]:
    """Reset one exported official image checkout to its frozen task base."""

    repo = Path(repo).resolve(strict=True)
    git_metadata = repo / ".git"
    try:
        metadata_info = git_metadata.lstat()
    except OSError as exc:
        raise OfficialSweDockerError(
            "exported official task repo is not standalone Git"
        ) from exc
    if not stat.S_ISDIR(metadata_info.st_mode):
        raise OfficialSweDockerError(
            "exported official task repo is not standalone Git"
        )
    if Path(_git_output(repo, "rev-parse", "--show-toplevel")).resolve() != repo:
        raise OfficialSweDockerError("exported task inherited another Git repo")
    if Path(_git_output(repo, "rev-parse", "--absolute-git-dir")).resolve() != (
        git_metadata.resolve()
    ):
        raise OfficialSweDockerError(
            "exported official task repo is not standalone Git"
        )
    commit = _git_command(repo, "cat-file", "-e", f"{base_commit}^{{commit}}")
    if commit.returncode != 0:
        raise OfficialSweDockerError(
            "official SWE base commit is absent from the packaged image"
        )
    if _git_output(repo, "rev-parse", "HEAD^{tree}") != _git_output(
        repo, "rev-parse", f"{base_commit}^{{tree}}"
    ):
        raise OfficialSweDockerError(
            "packaged SWE image tracked tree disagrees with the official base"
        )
    reset = _git_command(repo, "reset", "--hard", base_commit)
    cleaned = _git_command(repo, "clean", "-ffdx")
    if reset.returncode != 0 or cleaned.returncode != 0:
        raise OfficialSweDockerError(
            "could not normalize the packaged image to its official base commit"
        )
    head, tree = _inspect_clean_packaged_repo(repo)
    if head != base_commit:
        raise OfficialSweDockerError(
            "packaged SWE image HEAD disagrees with the official base commit"
        )
    return head, tree


def materialize_content_pinned_task_package(
    task_dir: Path,
    destination: Path,
    *,
    runtime_image_ref: str,
) -> dict[str, str]:
    """Copy one trusted task package and pin both physical Dockerfile bases."""

    source = Path(task_dir).resolve(strict=True)
    destination = Path(destination).absolute()
    if destination.exists():
        raise OfficialSweDockerError("fresh content-pinned task package already exists")
    identity = inspect_official_swe_task(source)
    if any(path.is_symlink() for path in source.rglob("*")):
        raise OfficialSweDockerError("task package source contains symlink")
    shutil.copytree(source, destination)
    for path in destination.rglob("*"):
        if path.is_symlink():
            raise OfficialSweDockerError(
                "task package materialization contains symlink"
            )
    for relative in (Path("environment/Dockerfile"), Path("tests/Dockerfile")):
        dockerfile = destination / relative
        lines = dockerfile.read_text(encoding="utf-8").splitlines(keepends=True)
        matches = [index for index, line in enumerate(lines) if _FROM_RE.match(line)]
        if len(matches) != 1:
            raise OfficialSweDockerError("task Dockerfile base is ambiguous")
        index = matches[0]
        matched = _FROM_RE.match(lines[index])
        if matched is None or matched.group(1) != identity.source_image:
            raise OfficialSweDockerError("task Dockerfile base moved before pinning")
        prefix = lines[index][: matched.start(1)]
        newline = "\n" if lines[index].endswith("\n") else ""
        lines[index] = prefix + runtime_image_ref + newline
        dockerfile.write_text("".join(lines), encoding="utf-8")
    materials = snapshot_task_materials(destination)
    return {
        "schema": "swe-content-pinned-task-package",
        "source_image": identity.source_image,
        "runtime_image_ref": runtime_image_ref,
        "public_material_digest": materials.public_digest,
        "evaluator_material_digest": materials.evaluator_digest,
        "materialization_digest": canonical_digest(
            {
                "schema": "swe-content-pinned-task-package",
                "task_id": identity.task_id,
                "source_image": identity.source_image,
                "runtime_image_ref": runtime_image_ref,
                "public_material_digest": materials.public_digest,
                "evaluator_material_digest": materials.evaluator_digest,
            }
        ),
    }


class OfficialSweTaskSource:
    """Trusted export of a fresh packaged SWE /testbed repository."""

    def __init__(
        self,
        task_dir: Path,
        *,
        docker_path: str | Path = "docker",
        expected_task_id: str | None = None,
        expected_repo: str | None = None,
    ):
        self.task_dir = Path(task_dir).expanduser().resolve(strict=True)
        self.identity = inspect_official_swe_task(
            self.task_dir,
            expected_task_id=expected_task_id,
            expected_repo=expected_repo,
        )
        self.docker = _resolve_docker(docker_path)

    def export_fresh_repo(self, destination: Path) -> OfficialSourceExport:
        destination = Path(destination).expanduser().absolute()
        if destination.exists():
            raise OfficialSweDockerError("fresh SWE destination already exists")
        destination.mkdir(mode=0o700, parents=True)

        try:
            image = resolve_immutable_docker_image(
                self.docker,
                self.identity.source_image,
                inspect=_inspect_image,
            )
        except HarborExecutionError as exc:
            raise OfficialSweDockerError(str(exc)) from exc
        repo_digests = image.repo_digests
        runtime_image_ref = image.runtime_ref

        name = "rsi-arena-swe-source-" + uuid.uuid4().hex
        created = ""
        try:
            created = _docker(
                self.docker,
                "create",
                "--name",
                name,
                "--network",
                "none",
                runtime_image_ref,
                "/bin/true",
                timeout=120,
            ).stdout.strip()
            if not created:
                raise OfficialSweDockerError("docker create returned no container ID")
            _docker(
                self.docker,
                "cp",
                f"{created}:/testbed/.",
                str(destination),
                timeout=600,
            )
        finally:
            _docker(
                self.docker,
                "rm",
                "-f",
                created or name,
                timeout=120,
                check=False,
            )

        packaged_head, packaged_tree = _normalize_packaged_repo(
            destination, self.identity.base_commit
        )
        runtime = SwePackagedRuntimeIdentity(
            source_image=self.identity.source_image,
            source_image_id=image.image_id,
            source_image_repo_digests=repo_digests,
            runtime_image_ref=runtime_image_ref,
            packaged_head=packaged_head,
            packaged_tree=packaged_tree,
        )
        return OfficialSourceExport(
            task_id=self.identity.task_id,
            repo=self.identity.repo,
            source_image=runtime.source_image,
            source_image_id=runtime.source_image_id,
            source_image_repo_digests=runtime.source_image_repo_digests,
            runtime_image_ref=runtime.runtime_image_ref,
            instruction_digest=self.identity.instruction_digest,
            task_static_evaluator_digest=self.identity.static_evaluator_digest,
            repo_head=runtime.packaged_head,
            repo_tree=runtime.packaged_tree,
            runtime_identity_digest=runtime.digest,
            exported_at=time.time(),
        )

    def revalidate_runtime_identity(
        self,
        source_export: OfficialSourceExport,
        *,
        exported_repo: Path | None = None,
    ) -> None:
        """Reinspect the configured and immutable image before trusted use."""

        if (
            source_export.task_id != self.identity.task_id
            or source_export.repo != self.identity.repo
            or source_export.source_image != self.identity.source_image
            or source_export.instruction_digest != self.identity.instruction_digest
            or source_export.task_static_evaluator_digest
            != self.identity.static_evaluator_digest
        ):
            raise OfficialSweDockerError("source export disagrees with task identity")
        runtime = source_export.runtime_identity()
        try:
            revalidate_immutable_docker_image(
                self.docker,
                ImmutableDockerImageIdentity(
                    configured_ref=runtime.source_image,
                    image_id=runtime.source_image_id,
                    repo_digests=runtime.source_image_repo_digests,
                    runtime_ref=runtime.runtime_image_ref,
                ),
                inspect=_inspect_image,
            )
        except (HarborExecutionError, OfficialSweDockerError) as exc:
            raise OfficialSweDockerError(
                "source image runtime identity moved after export"
            ) from exc
        if exported_repo is not None:
            head, tree = _inspect_clean_packaged_repo(exported_repo)
            if (head, tree) != (runtime.packaged_head, runtime.packaged_tree):
                raise OfficialSweDockerError(
                    "packaged runtime Git identity moved after export"
                )


class OfficialSweDockerGrader:
    """Build and run the frozen separate verifier against a frozen tree."""

    def __init__(
        self,
        *,
        task_dir: Path,
        docker: Path,
        grader_image: str,
        grader_image_identity: dict[str, object],
        source_export: OfficialSourceExport,
        grader_task_materialization: dict[str, str],
        build_started_at: float,
        build_finished_at: float,
        timeout_seconds: float,
    ) -> None:
        self.task_dir = Path(task_dir).resolve(strict=True)
        self.identity = inspect_official_swe_task(self.task_dir)
        self.docker = docker
        self.grader_image = grader_image
        self.grader_image_identity = dict(grader_image_identity)
        self.source_export = source_export
        self.grader_task_materialization = dict(grader_task_materialization)
        self.build_started_at = build_started_at
        self.build_finished_at = build_finished_at
        self.timeout_seconds = float(timeout_seconds)
        self._digest = canonical_digest(
            {
                "static_evaluator_digest": self.identity.static_evaluator_digest,
                "source_export": self.source_export.binding(),
                "source_export_digest": self.source_export.digest,
                "instruction_digest": self.identity.instruction_digest,
                "grader_image": grader_image,
                "grader_image_identity": self.grader_image_identity,
                "grader_task_materialization": self.grader_task_materialization,
                "timeout_seconds": self.timeout_seconds,
            }
        )

    @classmethod
    def prepare(
        cls,
        task_dir: Path,
        *,
        docker_path: str | Path = "docker",
        timeout_seconds: float | None = None,
        source_export: OfficialSourceExport,
        expected_task_id: str | None = None,
        expected_repo: str | None = None,
    ) -> OfficialSweDockerGrader:
        task_dir = Path(task_dir).expanduser().resolve(strict=True)
        identity = inspect_official_swe_task(
            task_dir,
            expected_task_id=expected_task_id,
            expected_repo=expected_repo,
        )
        if (
            source_export.task_id != identity.task_id
            or source_export.repo != identity.repo
            or source_export.source_image != identity.source_image
            or source_export.instruction_digest != identity.instruction_digest
            or source_export.task_static_evaluator_digest
            != identity.static_evaluator_digest
            or not source_export.source_image_id.startswith("sha256:")
        ):
            raise OfficialSweDockerError(
                "source export disagrees with grader task identity"
            )
        source = OfficialSweTaskSource(
            task_dir,
            docker_path=docker_path,
            expected_task_id=expected_task_id,
            expected_repo=expected_repo,
        )
        source.revalidate_runtime_identity(source_export)
        docker = source.docker
        tag = "rsi-arena-swe-grader:" + identity.static_evaluator_digest[:24]
        started = time.time()
        with tempfile.TemporaryDirectory(prefix="rsi-arena-swe-pinned-task-") as raw:
            pinned_task = Path(raw) / identity.task_id
            task_materialization = materialize_content_pinned_task_package(
                task_dir,
                pinned_task,
                runtime_image_ref=source_export.runtime_image_ref,
            )
            _docker(
                docker,
                "build",
                "--pull=false",
                "--tag",
                tag,
                "--file",
                str(pinned_task / "tests" / "Dockerfile"),
                str(pinned_task / "tests"),
                timeout=3600,
            )
        finished = time.time()
        image_identity = _inspect_image(docker, tag).as_dict()
        configured = timeout_seconds
        if configured is None:
            task_config = tomllib.loads((task_dir / "task.toml").read_text())
            configured = float(
                (task_config.get("verifier") or {}).get("timeout_sec", 3000)
            )
        return cls(
            task_dir=task_dir,
            docker=docker,
            grader_image=tag,
            grader_image_identity=image_identity,
            source_export=source_export,
            grader_task_materialization=task_materialization,
            build_started_at=started,
            build_finished_at=finished,
            timeout_seconds=float(configured),
        )

    @property
    def digest(self) -> str:
        return self._digest

    @property
    def evaluation_provenance(self) -> dict[str, object]:
        return {
            "schema": "swe-evaluation-binding",
            "task_identity": self.identity.__dict__,
            "source_export": self.source_export.binding(),
            "source_export_digest": self.source_export.digest,
            "instruction_digest": self.identity.instruction_digest,
            "static_evaluator_digest": self.identity.static_evaluator_digest,
            "grader_image": self.grader_image,
            "grader_image_identity": dict(self.grader_image_identity),
            "grader_task_materialization": dict(self.grader_task_materialization),
            "evaluator_digest": self.digest,
        }

    def grade(self, snapshot: FrozenTaskSnapshot) -> TrustedGrade:
        OfficialSweTaskSource(
            self.task_dir,
            docker_path=self.docker,
            expected_task_id=self.identity.task_id,
            expected_repo=self.identity.repo,
        ).revalidate_runtime_identity(self.source_export)
        if snapshot.task_id != self.identity.task_id:
            raise OfficialSweDockerError("snapshot task ID disagrees with grader")
        if snapshot.base_commit != self.source_export.repo_head:
            raise OfficialSweDockerError("snapshot base commit disagrees with grader")
        evidence_dir = snapshot.tree_path.parent / "official-grader"
        evidence_dir.mkdir(mode=0o700)
        with tempfile.TemporaryDirectory(
            prefix="rsi-arena-swe-grade-", dir=evidence_dir
        ) as raw:
            logs = Path(raw) / "logs"
            (logs / "artifacts").mkdir(mode=0o700, parents=True)
            (logs / "verifier").mkdir(mode=0o700)
            shutil.copyfile(
                snapshot.tree_path, logs / "artifacts" / "rsi-arena-tree.tar"
            )
            container_name = "rsi-arena-swe-grade-" + uuid.uuid4().hex
            started = time.time()
            try:
                result = _docker(
                    self.docker,
                    "run",
                    "--name",
                    container_name,
                    "--volume",
                    f"{logs}:/logs",
                    self.grader_image,
                    "bash",
                    "/tests/test.sh",
                    timeout=self.timeout_seconds + 30,
                    check=False,
                )
                finished = time.time()
            finally:
                _docker(
                    self.docker,
                    "rm",
                    "-f",
                    container_name,
                    timeout=120,
                    check=False,
                )
            stdout_path = evidence_dir / "grader.stdout.log"
            stdout_path.write_text(result.stdout + result.stderr)
            reward_path = logs / "verifier" / "reward.txt"
            report_path = logs / "verifier" / "report.json"
            if not reward_path.is_file() or not report_path.is_file():
                raise OfficialSweDockerError("official verifier omitted reward/report")
            reward_text = reward_path.read_text().strip()
            if reward_text not in {"0", "1"}:
                raise OfficialSweDockerError("official verifier reward is not binary")
            reward = float(reward_text)
            if (result.returncode == 0) != (reward == 1.0):
                raise OfficialSweDockerError("verifier exit and reward disagree")
            report = json.loads(report_path.read_text())
            instance = report.get(self.identity.instance_id)
            if not isinstance(instance, dict) or bool(instance.get("resolved")) != bool(
                reward
            ):
                raise OfficialSweDockerError("official report and reward disagree")
            copied_report = evidence_dir / "report.json"
            shutil.copyfile(report_path, copied_report)
            provenance = {
                "schema": "swe-grading-provenance",
                "task_identity": self.identity.__dict__,
                "evaluation_binding": self.evaluation_provenance,
                "source_export": self.source_export.as_dict(),
                "instruction_digest": self.identity.instruction_digest,
                "static_evaluator_digest": self.identity.static_evaluator_digest,
                "evaluator_digest": self.digest,
                "grader_image": self.grader_image,
                "grader_image_identity": self.grader_image_identity,
                "grader_build_started_at": self.build_started_at,
                "grader_build_finished_at": self.build_finished_at,
                "grading_started_at": started,
                "grading_finished_at": finished,
                "docker_return_code": result.returncode,
                "snapshot_tree_digest": snapshot.tree_digest,
                "snapshot_patch_digest": snapshot.patch_digest,
                "reward": reward,
                "report_digest": "sha256:"
                + hashlib.sha256(copied_report.read_bytes()).hexdigest(),
                "stdout_digest": "sha256:"
                + hashlib.sha256(stdout_path.read_bytes()).hexdigest(),
                "candidate_score_authoritative": False,
            }
            provenance_path = evidence_dir / "provenance.json"
            provenance_path.write_text(
                json.dumps(provenance, sort_keys=True, separators=(",", ":")) + "\n"
            )
        return TrustedGrade(
            score=reward,
            raw_metric_ref=str(provenance_path),
        )


__all__ = [
    "OFFICIAL_GRADING_IMPORTS",
    "SWE_GRADER_SOURCE_COMMIT",
    "SWE_GRADER_VERSION",
    "OfficialSourceExport",
    "OfficialSweDockerError",
    "OfficialSweDockerGrader",
    "OfficialSweTaskIdentity",
    "OfficialSweTaskSource",
    "SwePackagedRuntimeIdentity",
    "inspect_official_swe_task",
    "materialize_content_pinned_task_package",
]
