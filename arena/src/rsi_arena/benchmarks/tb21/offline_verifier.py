"""TB2.1-specific Harbor verifier backed by a source-built offline closure.

Harbor constructs this verifier only after its agent phase.  The verifier
copies the live ``/app`` tree into a trusted, cell-scoped snapshot, runs the
task's original ``test.sh`` in the task-bound verifier-only image with Docker
networking disabled, and admits a reward only with a complete CTRF artifact.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any

from rsi_arena.run_layout import file_digest

from .tasks import TB21TaskSnapshot

try:  # Harbor is deliberately not an AgentPilot core dependency.
    from harbor.agents.base import BaseAgent as _BaseAgent
    from harbor.models.verifier.result import VerifierResult as _VerifierResult
    from harbor.verifier.base import BaseVerifier as _BaseVerifier
except ModuleNotFoundError as exc:  # pragma: no cover - depends on Harbor env.
    if not exc.name or not (exc.name == "harbor" or exc.name.startswith("harbor.")):
        raise

    class _BaseVerifier:  # type: ignore[no-redef]
        def __init__(self, **kwargs: Any) -> None:
            for name, value in kwargs.items():
                setattr(self, name, value)

    class _BaseAgent:  # type: ignore[no-redef]
        def __init__(self, **kwargs: Any) -> None:
            for name, value in kwargs.items():
                setattr(self, name, value)

    class _VerifierResult:  # type: ignore[no-redef]
        def __init__(self, *, rewards: Mapping[str, float]) -> None:
            self.rewards = dict(rewards)


OFFLINE_VERIFIER_IMPORT_PATH = (
    "rsi_arena.benchmarks.tb21.offline_verifier:TB21OfflineVerifier"
)
AUTHORITY_SCHEMA = "tb21-offline-verifier-authority"
PROVENANCE_SCHEMA = "tb21-harbor-offline-verifier"
SNAPSHOT_SCHEMA = "tb21-canonical-app-tree"
RESHARD_TASK_ID = "reshard-c4-data"
SAM_TASK_ID = "sam-cell-seg"
AUTHORITY_PLATFORM = "linux/amd64"
SYNTHETIC_AGENT_IMPORT_PATH = (
    "rsi_arena.benchmarks.tb21.offline_verifier:TB21SyntheticNoModelAgent"
)
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")
_TASK_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")
_RUN_ID_RE = re.compile(r"tb21-harbor-cell-[0-9a-f]{32}")
_BUILD_CACHE_NAMES = frozenset(
    {
        "NO_PROXY",
        "PIP_INDEX_URL",
        "PIP_TRUSTED_HOST",
        "UV_INDEX_URL",
        "UV_INSECURE_HOST",
        "UV_INSTALLER_GHE_BASE_URL",
        "UV_PYTHON_INSTALL_MIRROR",
        "http_proxy",
        "no_proxy",
    }
)
_OFFLINE_ENV = MappingProxyType(
    {
        "UV_OFFLINE": "1",
        "HF_HUB_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "http_proxy": "http://127.0.0.1:9",
        "https_proxy": "http://127.0.0.1:9",
    }
)


def _task_offline_env(task_id: str, verifier_env: Mapping[str, str]) -> dict[str, str]:
    """Apply task-specific settings required by the locked offline closure."""

    resolved = dict(verifier_env)
    if task_id == SAM_TASK_ID:
        # MobileSAM's legacy setup.py is already backed by locked wheels and a
        # pinned local git bundle. Avoid a second isolated build environment,
        # which would try to resolve its build backend while networking is off.
        resolved["UV_NO_BUILD_ISOLATION"] = "1"
    return resolved


class TB21OfflineVerifierError(RuntimeError):
    """Fail-closed verifier infrastructure/authority error."""


class TB21SyntheticNoModelAgent(_BaseAgent):
    """No-model agent used only by the public reshard closure smoke."""

    SUPPORTS_WINDOWS = False

    def __init__(self, *, task_id: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if task_id != RESHARD_TASK_ID:
            raise ValueError("synthetic reshard agent task is not authorized")
        self.task_id = task_id

    @staticmethod
    def name() -> str:
        return "rsi-arena-tb21-reshard-no-model"

    def version(self) -> str:
        # Harbor's BaseAgent contract requires a package-style version string.
        return "1.0.0"

    async def setup(self, environment: Any) -> None:
        return None

    async def run(self, instruction: str, environment: Any, context: Any) -> None:
        del instruction, context
        with __import__("tempfile").TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {
                "SYNTHETIC-CANDIDATE.txt": (
                    "Fresh dynamic verifier-overlay proof candidate.\n"
                ),
                "pyproject.toml": (
                    '[project]\nname = "tb21-reshard-c4-synthetic-candidate"\n'
                    'version = "0.0.0"\nrequires-python = ">=3.13"\n'
                    'dependencies = ["typing-extensions==4.15.0"]\n'
                ),
                "compress.py": (
                    "import pathlib, sys, tarfile\n"
                    "source, output = map(pathlib.Path, sys.argv[1:3])\n"
                    "output.mkdir(parents=True, exist_ok=True)\n"
                    "files = sorted(path for path in source.iterdir() if path.is_file())\n"
                    "for index in range(0, len(files), 20):\n"
                    "    archive_number = index // 20\n"
                    "    folder = output / f'group-{archive_number // 25:03d}'\n"
                    "    folder.mkdir(exist_ok=True)\n"
                    "    archive = folder / f'chunk-{archive_number:05d}.tar.gz'\n"
                    "    with tarfile.open(archive, 'w:gz') as stream:\n"
                    "        for path in files[index:index + 20]:\n"
                    "            stream.add(path, arcname=path.name)\n"
                ),
                "decompress.py": (
                    "import pathlib, shutil, sys, tarfile, tempfile\n"
                    "root = pathlib.Path(sys.argv[1])\n"
                    "temporary = pathlib.Path(tempfile.mkdtemp(prefix='tb21-reshard-'))\n"
                    "for archive in sorted(root.rglob('*.tar.gz')):\n"
                    "    with tarfile.open(archive, 'r:gz') as stream:\n"
                    "        members = stream.getmembers()\n"
                    "        assert all('/' not in member.name and member.isfile() for member in members)\n"
                    "        stream.extractall(temporary, members=members, filter='data')\n"
                    "for child in list(root.iterdir()):\n"
                    "    shutil.rmtree(child) if child.is_dir() else child.unlink()\n"
                    "for path in temporary.iterdir():\n"
                    "    shutil.move(str(path), root / path.name)\n"
                    "temporary.rmdir()\n"
                ),
            }
            for name, content in files.items():
                (root / name).write_text(content, "utf-8")
            await environment.upload_dir(root, "/app")


@dataclass(frozen=True)
class OfflineTaskBinding:
    task_id: str
    family: str
    closure_image_id: str
    closure_base_image_id: str
    dynamic_candidate_uv: bool
    profile: str
    test_digest: str
    task_material_digest: str
    build_input_digest: str


@dataclass(frozen=True)
class OfflineClosureAuthority:
    path: Path
    digest: str
    content_digest: str
    lock_digest: str
    snapshot_digest: str | None
    source_commit: str
    source_tree: str
    tasks_tree: str
    closure_digest: str
    tasks: Mapping[str, OfflineTaskBinding]
    stage: str
    payload: Mapping[str, Any]

    def binding(self, task_id: str) -> OfflineTaskBinding:
        try:
            return self.tasks[task_id]
        except KeyError as exc:
            raise TB21OfflineVerifierError(
                f"offline closure has no task binding: {task_id}"
            ) from exc


def _require_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise TB21OfflineVerifierError(f"{label} is not a sha256 identity")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _canonical_digest(value: Mapping[str, object]) -> str:
    try:
        encoded = json.dumps(
            dict(value),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TB21OfflineVerifierError(
            "offline closure authority is noncanonical"
        ) from exc
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _is_utc_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)


def decode_offline_closure_authority(
    authority_path: Path,
    *,
    require_verified: bool = True,
) -> OfflineClosureAuthority:
    """Decode the sole builder authority at its built/verifying/verified stage."""

    try:
        path = Path(authority_path).resolve(strict=True)
        payload = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TB21OfflineVerifierError(
            "offline closure authority is unavailable"
        ) from exc
    if not isinstance(payload, Mapping):
        raise TB21OfflineVerifierError("offline closure authority is malformed")
    raw_tasks = payload.get("tasks")
    task_source = payload.get("task_source")
    verification = payload.get("verification")
    authority_task_ids = (
        tuple(sorted(raw_tasks)) if isinstance(raw_tasks, Mapping) else ()
    )
    claimed_content_digest = payload.get("authority_digest")
    digest_input = dict(payload)
    digest_input.pop("authority_digest", None)
    if (
        set(payload)
        != {
            "schema",
            "generated_at",
            "task_source",
            "lock_digest",
            "platform",
            "closure_digest",
            "tasks",
            "verification",
            "authority_digest",
        }
        or payload.get("schema") != AUTHORITY_SCHEMA
        or not _is_utc_timestamp(payload.get("generated_at"))
        or payload.get("platform") != AUTHORITY_PLATFORM
        or not isinstance(raw_tasks, Mapping)
        or not authority_task_ids
        or any(
            not isinstance(task_id, str) or _TASK_ID_RE.fullmatch(task_id) is None
            for task_id in authority_task_ids
        )
        or not isinstance(task_source, Mapping)
        or set(task_source) != {"repository", "commit", "repository_tree", "tasks_tree"}
        or task_source.get("repository") != "harbor-framework/terminal-bench-2-1"
        or any(
            not isinstance(task_source.get(name), str)
            or re.fullmatch(r"[0-9a-f]{40}", str(task_source[name])) is None
            for name in ("commit", "repository_tree", "tasks_tree")
        )
        or not isinstance(verification, Mapping)
        or not isinstance(claimed_content_digest, str)
        or _canonical_digest(digest_input) != claimed_content_digest
    ):
        raise TB21OfflineVerifierError("offline closure authority is noncanonical")

    lock_digest = _require_sha256(payload.get("lock_digest"), "closure lock digest")
    closure_digest = _require_sha256(payload.get("closure_digest"), "closure digest")
    tasks: dict[str, OfflineTaskBinding] = {}
    closure_input: dict[str, object] = {}
    evidence_input: dict[str, object] = {}
    for task_id in authority_task_ids:
        raw = raw_tasks[task_id]
        if not isinstance(raw, Mapping):
            raise TB21OfflineVerifierError(
                f"offline task binding is malformed: {task_id}"
            )
        smoke = raw.get("offline_smoke")
        dynamic = raw.get("dynamic_candidate_uv")
        expected_task_fields = {
            "family",
            "profile",
            "task_material_digest",
            "test_digest",
            "build_input_digest",
            "configured_base_image",
            "closure_base_image_id",
            "closure_image_id",
            "dynamic_candidate_uv",
            "artifact_set_digest",
            "offline_smoke",
        }
        if (
            set(raw) != expected_task_fields
            or not isinstance(raw.get("family"), str)
            or not raw.get("family")
            or not isinstance(raw.get("profile"), str)
            or not raw.get("profile")
            or not isinstance(dynamic, bool)
            or dynamic != (task_id == RESHARD_TASK_ID)
            or not isinstance(raw.get("task_material_digest"), str)
            or re.fullmatch(r"[0-9a-f]{64}", str(raw.get("task_material_digest")))
            is None
            or not isinstance(raw.get("configured_base_image"), str)
            or not raw.get("configured_base_image")
            or any(
                not isinstance(raw.get(name), str)
                or _SHA256_RE.fullmatch(str(raw[name])) is None
                for name in (
                    "test_digest",
                    "build_input_digest",
                    "closure_base_image_id",
                    "closure_image_id",
                    "artifact_set_digest",
                )
            )
        ):
            raise TB21OfflineVerifierError(f"offline task binding changed: {task_id}")
        if smoke is not None:
            _validate_smoke_binding(task_id, raw, smoke)
            evidence_input[task_id] = dict(smoke)
        tasks[task_id] = OfflineTaskBinding(
            task_id=task_id,
            family=str(raw["family"]),
            closure_image_id=str(raw["closure_image_id"]),
            closure_base_image_id=str(raw["closure_base_image_id"]),
            dynamic_candidate_uv=dynamic,
            profile=str(raw["profile"]),
            test_digest=str(raw["test_digest"]),
            task_material_digest=str(raw["task_material_digest"]),
            build_input_digest=str(raw["build_input_digest"]),
        )
        closure_input[task_id] = {
            "base": raw["closure_base_image_id"],
            "closure": raw["closure_image_id"],
            "build_input": raw["build_input_digest"],
        }

    if closure_digest != _canonical_digest(closure_input):
        raise TB21OfflineVerifierError("offline closure authority digest changed")
    verified_task_ids = tuple(sorted(evidence_input))
    if not verified_task_ids:
        stage = "built"
        expected_verification: dict[str, object] = {
            "status": "built",
            "network_mode": "none",
            "task_ids": [],
        }
    elif verified_task_ids == authority_task_ids:
        stage = "verified"
        expected_verification = {
            "status": "verified",
            "network_mode": "none",
            "task_ids": list(authority_task_ids),
            "evidence_digest": _canonical_digest(evidence_input),
        }
    else:
        stage = "verifying"
        expected_verification = {
            "status": "verifying",
            "network_mode": "none",
            "task_ids": list(verified_task_ids),
            "evidence_digest": _canonical_digest(evidence_input),
        }
    if dict(verification) != expected_verification:
        raise TB21OfflineVerifierError(
            "offline closure authority verification stage is noncanonical"
        )
    if require_verified and stage != "verified":
        raise TB21OfflineVerifierError("offline closure authority is not verified")
    return OfflineClosureAuthority(
        path=path,
        digest=_sha256_file(path),
        content_digest=claimed_content_digest,
        lock_digest=lock_digest,
        snapshot_digest=None,
        source_commit=str(task_source["commit"]),
        source_tree=str(task_source["repository_tree"]),
        tasks_tree=str(task_source["tasks_tree"]),
        closure_digest=closure_digest,
        tasks=MappingProxyType(tasks),
        stage=stage,
        payload=MappingProxyType(dict(payload)),
    )


def _validate_smoke_binding(
    task_id: str, raw: Mapping[str, object], smoke: object
) -> None:
    count_fields = ("tests", "passed", "failed", "pending", "skipped", "other")
    timing_fields = ("start", "stop")
    expected_smoke_fields = {
        "status",
        "verified_at",
        "network_mode",
        "base_image_id",
        "closure_image_id",
        "candidate_snapshot",
        "ctrf",
        "ctrf_sha256",
        "reward",
        "reward_sha256",
        "provenance_sha256",
        "original_test_digest",
        "build_input_digest",
    }
    if not isinstance(smoke, Mapping):
        raise TB21OfflineVerifierError(f"offline task smoke binding changed: {task_id}")
    ctrf = smoke.get("ctrf")
    candidate = smoke.get("candidate_snapshot")
    reward = smoke.get("reward")
    invalid = (
        set(smoke) != expected_smoke_fields
        or smoke.get("status") != "verified"
        or not _is_utc_timestamp(smoke.get("verified_at"))
        or smoke.get("network_mode") != "none"
        or smoke.get("base_image_id") != raw.get("closure_base_image_id")
        or smoke.get("closure_image_id") != raw.get("closure_image_id")
        or smoke.get("original_test_digest") != raw.get("test_digest")
        or smoke.get("build_input_digest") != raw.get("build_input_digest")
        or any(
            not isinstance(smoke.get(name), str)
            or _SHA256_RE.fullmatch(str(smoke[name])) is None
            for name in ("ctrf_sha256", "reward_sha256", "provenance_sha256")
        )
        or not isinstance(candidate, Mapping)
        or set(candidate) != {"schema", "sha256", "entries"}
        or candidate.get("schema") != SNAPSHOT_SCHEMA
        or not isinstance(candidate.get("sha256"), str)
        or _SHA256_RE.fullmatch(str(candidate.get("sha256"))) is None
        or not isinstance(candidate.get("entries"), int)
        or isinstance(candidate.get("entries"), bool)
        or int(candidate.get("entries", -1)) < 0
        or not isinstance(ctrf, Mapping)
        or set(ctrf) != set(count_fields + timing_fields)
        or any(
            not isinstance(ctrf.get(name), int)
            or isinstance(ctrf.get(name), bool)
            or int(ctrf[name]) < 0
            for name in count_fields
        )
        or any(
            not isinstance(ctrf.get(name), (int, float))
            or isinstance(ctrf.get(name), bool)
            or (isinstance(ctrf[name], float) and not math.isfinite(ctrf[name]))
            or ctrf[name] < 0
            for name in timing_fields
        )
        or ctrf["stop"] < ctrf["start"]
        or ctrf.get("tests") != sum(int(ctrf[name]) for name in count_fields[1:])
        or ctrf.get("tests") == 0
        or not isinstance(reward, (int, float))
        or isinstance(reward, bool)
        or not math.isfinite(float(reward))
        or not 0.0 <= float(reward) <= 1.0
        or (int(ctrf["failed"]) > 0 and float(reward) != 0.0)
        or (int(ctrf["passed"]) == int(ctrf["tests"]) and float(reward) != 1.0)
    )
    if invalid:
        raise TB21OfflineVerifierError(f"offline task smoke binding changed: {task_id}")


def load_offline_closure_authority(
    snapshot: TB21TaskSnapshot,
    *,
    authority_path: Path,
) -> OfflineClosureAuthority:
    """Bind the canonical verified authority to this run-owned task snapshot."""

    authority = decode_offline_closure_authority(authority_path)
    snapshot_task_ids = frozenset(str(item["task_id"]) for item in snapshot.tasks)
    if (
        not snapshot_task_ids
        or not snapshot_task_ids.issubset(authority.tasks)
        or authority.source_commit != snapshot.commit
        or authority.source_tree != snapshot.repository_tree
        or authority.tasks_tree != snapshot.tasks_tree
    ):
        raise TB21OfflineVerifierError(
            "offline closure authority does not match the frozen task slice"
        )
    tasks: dict[str, OfflineTaskBinding] = {}
    for task_id in sorted(snapshot_task_ids):
        binding = authority.tasks[task_id]
        pinned = snapshot.pinned_task_material(task_id)
        test_path = snapshot.tasks_root / task_id / "tests" / "test.sh"
        if (
            binding.task_material_digest != pinned.tree_digest
            or binding.test_digest != _sha256_file(test_path)
        ):
            raise TB21OfflineVerifierError(f"offline task binding changed: {task_id}")
        tasks[task_id] = binding
    return replace(
        authority,
        snapshot_digest=snapshot.snapshot_digest,
        tasks=MappingProxyType(tasks),
    )


def offline_verifier_launch_kwargs(
    *,
    authority: OfflineClosureAuthority,
    source_commit: str,
    task_id: str,
    run_id: str,
    expected_agent_image_id: str,
    shared_run_root: Path,
    tests_path: Path,
    docker: Path,
    docker_host: str,
    verifier_timeout_seconds: float,
) -> Mapping[str, str]:
    """Bind one Harbor launch to the frozen offline-verifier authority."""

    if (
        authority.snapshot_digest is None
        or authority.source_commit != source_commit
        or task_id not in authority.tasks
    ):
        raise TB21OfflineVerifierError(
            "offline verifier authority disagrees with the task snapshot"
        )
    if not _RUN_ID_RE.fullmatch(run_id):
        raise TB21OfflineVerifierError("offline verifier run identity is invalid")
    _require_sha256(expected_agent_image_id, "agent image ID")
    root = Path(shared_run_root)
    if not root.is_absolute() or root == Path("/"):
        raise TB21OfflineVerifierError(
            "offline verifier shared root must be narrow and absolute"
        )
    tests = Path(tests_path).resolve(strict=True)
    test_script = tests / "test.sh"
    if not test_script.is_file():
        raise TB21OfflineVerifierError("offline verifier test.sh is unavailable")
    docker_path = Path(docker)
    if not docker_path.is_absolute() or not docker_path.is_file():
        raise TB21OfflineVerifierError("offline verifier Docker path is invalid")
    if not isinstance(docker_host, str):
        raise TB21OfflineVerifierError("offline verifier Docker host is invalid")
    timeout = float(verifier_timeout_seconds)
    if not math.isfinite(timeout) or timeout <= 0.0:
        raise TB21OfflineVerifierError("offline verifier timeout is invalid")

    binding = authority.binding(task_id)
    if file_digest(test_script) != binding.test_digest:
        raise TB21OfflineVerifierError("offline verifier test.sh changed after locking")
    return MappingProxyType(
        {
            "task_id": task_id,
            "run_id": run_id,
            "snapshot_digest": authority.snapshot_digest,
            "source_commit": source_commit,
            "source_tree": authority.source_tree,
            "tasks_tree": authority.tasks_tree,
            "authority_digest": authority.digest,
            "authority_content_digest": authority.content_digest,
            "lock_digest": authority.lock_digest,
            "closure_digest": authority.closure_digest,
            "build_input_digest": binding.build_input_digest,
            "task_material_digest": binding.task_material_digest,
            "closure_image_id": binding.closure_image_id,
            "closure_base_image_id": binding.closure_base_image_id,
            "expected_agent_image_id": expected_agent_image_id,
            "dynamic_candidate_uv": (
                "true" if binding.dynamic_candidate_uv else "false"
            ),
            "shared_run_root": str(root / task_id / run_id),
            "tests_path": str(tests),
            "tests_digest": file_digest(test_script),
            "docker": str(docker_path),
            "docker_host": docker_host.strip(),
            "verifier_timeout_seconds": str(timeout),
        }
    )


def canonical_app_identity(root: Path) -> Mapping[str, object]:
    """Hash every path, type, mode, symlink target, and regular-file content."""

    resolved = root.resolve(strict=True)
    if not resolved.is_dir():
        raise TB21OfflineVerifierError("candidate /app snapshot is not a directory")
    digest = hashlib.sha256()
    count = 0
    for path in sorted(
        resolved.rglob("*"), key=lambda item: item.relative_to(resolved).as_posix()
    ):
        relative = path.relative_to(resolved).as_posix()
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISREG(info.st_mode):
            record: dict[str, object] = {
                "path": relative,
                "kind": "file",
                "mode": mode,
                "bytes": info.st_size,
                "sha256": _sha256_file(path),
            }
        elif stat.S_ISDIR(info.st_mode):
            record = {"path": relative, "kind": "directory", "mode": mode}
        elif stat.S_ISLNK(info.st_mode):
            record = {
                "path": relative,
                "kind": "symlink",
                "mode": mode,
                "target": os.readlink(path),
            }
        else:
            raise TB21OfflineVerifierError(
                f"candidate /app contains a special file: {relative}"
            )
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        count += 1
    return MappingProxyType(
        {
            "schema": SNAPSHOT_SCHEMA,
            "sha256": "sha256:" + digest.hexdigest(),
            "entries": count,
        }
    )


def _same_app_tree(left: Path, right: Path) -> bool:
    """Compare two stopped candidate trees directly, including file bytes."""

    return canonical_app_identity(left) == canonical_app_identity(right)


def _docker_command(docker: str, docker_host: str, *args: str) -> tuple[str, ...]:
    if docker_host:
        return (docker, "--host", docker_host, *args)
    return (docker, *args)


def _run(
    command: tuple[str, ...], *, timeout: float = 900.0, check: bool = True
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command, capture_output=True, text=True, timeout=timeout, check=False
    )
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[-1000:]
        raise TB21OfflineVerifierError(
            f"offline verifier command failed ({completed.returncode}): {detail}"
        )
    return completed


def inspect_exact_image(docker: str, docker_host: str, image_id: str) -> str:
    _require_sha256(image_id, "Docker image ID")
    completed = _run(
        _docker_command(
            docker, docker_host, "image", "inspect", image_id, "--format", "{{.Id}}"
        ),
        timeout=60.0,
    )
    observed = completed.stdout.strip()
    if observed != image_id:
        raise TB21OfflineVerifierError(
            f"offline verifier image changed: expected {image_id}, observed {observed}"
        )
    return observed


def _python_script_shell(script: str) -> str:
    """Run a verifier helper with the Python entry point available in the image."""

    quoted_script = shlex.quote("exec(" + repr(script) + ")")
    return (
        'arena_python="$(command -v python || command -v python3 || true)"; '
        'if test -z "$arena_python" && command -v uv >/dev/null 2>&1; then '
        'arena_python="$(uv python find 2>/dev/null || true)"; fi; '
        'test -n "$arena_python" || { '
        'echo "offline verifier Python interpreter is unavailable" >&2; exit 127; }; '
        '"$arena_python" -c ' + quoted_script
    )


def _render_reshard_dockerfile() -> str:
    provenance_script = "\n".join(
        (
            "import json,pathlib,subprocess,tomllib",
            "r=pathlib.Path('/app'); p=r/'pyproject.toml'",
            "assert p.is_file() and (r/'.venv').is_dir()",
            "project=tomllib.loads(p.read_text())",
            "payload={'command':['uv','venv','--python','3.13','.venv','&&','uv','pip','install','--python','.venv/bin/python','-r','pyproject.toml'],'declared_dependencies':project.get('project',{}).get('dependencies',[]),'installed_distributions':subprocess.run(['uv','pip','freeze','--python','/app/.venv/bin/python'],check=True,capture_output=True,text=True).stdout.splitlines()}",
            "o=pathlib.Path('/opt/tb21-candidate-uv'); o.mkdir(parents=True); (o/'provenance.json').write_text(json.dumps(payload,sort_keys=True))",
        )
    )
    return "\n".join(
        (
            "ARG BASE_IMAGE",
            "FROM ${BASE_IMAGE}",
            'SHELL ["/bin/bash", "-o", "pipefail", "-c"]',
            "WORKDIR /app",
            "RUN rm -rf /app && mkdir -p /app",
            "COPY candidate-app-snapshot/ /app/",
            "RUN test -f /app/pyproject.toml",
            "RUN uv venv --python 3.13 /app/.venv && UV_OFFLINE=1 uv pip install --python /app/.venv/bin/python -r /app/pyproject.toml",
            "RUN " + _python_script_shell(provenance_script),
            "ENV UV_OFFLINE=1 HF_HUB_OFFLINE=1 HF_DATASETS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1",
            "",
        )
    )


def _prepare_dynamic_reshard_image(
    *,
    docker: str,
    docker_host: str,
    binding: OfflineTaskBinding,
    run_root: Path,
    verifier_env: Mapping[str, str],
) -> tuple[str, Mapping[str, object]]:
    # The public closure is self-contained. Candidate resolution deliberately
    # ignores external cache endpoints and uses only locked image content.
    _ = verifier_env
    inspect_exact_image(docker, docker_host, binding.closure_image_id)
    suffix = re.sub(r"[^a-zA-Z0-9_.-]", "-", run_root.name)[-24:]
    base_ref = f"rsi-arena/tb21-reshard-base:{suffix}"
    image_ref = f"rsi-arena/tb21-reshard-closure:{suffix}"
    _run(
        _docker_command(docker, docker_host, "tag", binding.closure_image_id, base_ref)
    )
    observed_base_id = _run(
        _docker_command(
            docker, docker_host, "image", "inspect", base_ref, "--format", "{{.Id}}"
        ),
        timeout=60.0,
    ).stdout.strip()
    if observed_base_id != binding.closure_image_id:
        raise TB21OfflineVerifierError("reshard local base tag changed authority")
    dockerfile = run_root / "Dockerfile.verifier-closure"
    dockerfile.write_text(_render_reshard_dockerfile(), "utf-8")
    command = list(
        _docker_command(
            docker,
            docker_host,
            "build",
            "--pull=false",
            "--network=none",
            "--tag",
            image_ref,
            "--build-arg",
            f"BASE_IMAGE={base_ref}",
        )
    )
    command.extend(("--file", str(dockerfile), str(run_root)))
    _run(tuple(command), timeout=1800.0)
    image_id = _run(
        _docker_command(
            docker, docker_host, "image", "inspect", image_ref, "--format", "{{.Id}}"
        ),
        timeout=60.0,
    ).stdout.strip()
    _require_sha256(image_id, "dynamic reshard verifier image")
    probe = _run(
        _docker_command(
            docker,
            docker_host,
            "run",
            "--rm",
            "--pull=never",
            "--network",
            "none",
            "--env",
            "UV_OFFLINE=1",
            image_id,
            "/bin/bash",
            "-c",
            "uv pip check --python /app/.venv/bin/python >/dev/null && cat /opt/tb21-candidate-uv/provenance.json",
        ),
        timeout=600.0,
    )
    try:
        provenance = json.loads(probe.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise TB21OfflineVerifierError(
            "reshard dependency provenance is invalid"
        ) from exc
    return image_id, MappingProxyType(
        {
            **dict(provenance),
            "preparation_mode": "fresh-build",
        }
    )


def load_offline_ctrf_counts(path: Path) -> Mapping[str, int]:
    """Load the complete native-integer CTRF summary used for scoring."""

    names = ("tests", "passed", "failed", "pending", "skipped", "other")
    try:
        payload = json.loads(path.read_text("utf-8"))
        summary = payload["results"]["summary"]
        counts = {key: summary[key] for key in names}
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise TB21OfflineVerifierError("offline verifier CTRF is invalid") from exc
    if (
        any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in counts.values()
        )
        or counts["tests"] <= 0
        or counts["tests"]
        != sum(
            counts[key] for key in ("passed", "failed", "pending", "skipped", "other")
        )
    ):
        raise TB21OfflineVerifierError("offline verifier CTRF summary is incomplete")
    return MappingProxyType(counts)


def _read_reward(path: Path) -> float:
    try:
        value = float(path.read_text("utf-8").strip())
    except (OSError, ValueError) as exc:
        raise TB21OfflineVerifierError("offline verifier reward is invalid") from exc
    if not 0.0 <= value <= 1.0:
        raise TB21OfflineVerifierError("offline verifier reward is out of range")
    return value


def _run_offline_container(
    *,
    docker: str,
    docker_host: str,
    image_id: str,
    task_id: str,
    run_id: str,
    tests_path: Path,
    snapshot_path: Path,
    logs_path: Path,
    dynamic_candidate_uv: bool,
    timeout_seconds: float,
    task_verifier_env: Mapping[str, str] | None = None,
) -> tuple[float, Mapping[str, int], str]:
    inspect_exact_image(docker, docker_host, image_id)
    container_name = f"tb21-offline-{task_id[:40]}-{run_id[-16:]}"
    verifier_app_path: Path | None = None
    if not dynamic_candidate_uv:
        # Mount a writable host copy over /app instead of deleting the base
        # image's application layer. Some task images contain very large /app
        # trees that cannot be reliably whiteouted by nested fuse-overlayfs.
        verifier_app_path = snapshot_path.parent / "verifier-app"
        shutil.copytree(snapshot_path, verifier_app_path, symlinks=True)
        if not _same_app_tree(snapshot_path, verifier_app_path):
            raise TB21OfflineVerifierError(
                "offline verifier working copy changed candidate identity"
            )
    shell = "cd /app && exec /bin/bash /tests/test.sh"
    command = list(
        _docker_command(
            docker,
            docker_host,
            "run",
            "--rm",
            "--pull=never",
            "--name",
            container_name,
            "--network",
            "none",
            "--workdir",
            "/app",
            "--mount",
            f"type=bind,src={snapshot_path},dst=/candidate-snapshot,readonly",
            "--mount",
            f"type=bind,src={tests_path},dst=/tests,readonly",
            "--mount",
            f"type=bind,src={logs_path},dst=/logs/verifier",
        )
    )
    if verifier_app_path is not None:
        command.extend(
            (
                "--mount",
                f"type=bind,src={verifier_app_path},dst=/app",
            )
        )
    runtime_env = {
        **dict(task_verifier_env or {}),
        **dict(_OFFLINE_ENV),
    }
    for name, value in sorted(runtime_env.items()):
        command.extend(("--env", f"{name}={value}"))
    command.extend(("--entrypoint", "/bin/bash", image_id, "-c", shell))
    try:
        completed = _run(tuple(command), timeout=timeout_seconds, check=False)
    except subprocess.TimeoutExpired as exc:
        _run(
            _docker_command(docker, docker_host, "rm", "--force", container_name),
            timeout=60.0,
            check=False,
        )
        raise TB21OfflineVerifierError("offline verifier container timed out") from exc
    (logs_path / "test-stdout.txt").write_text(
        completed.stdout + completed.stderr, "utf-8"
    )
    if completed.returncode != 0:
        raise TB21OfflineVerifierError(
            f"offline verifier container exited {completed.returncode}"
        )
    ctrf = load_offline_ctrf_counts(logs_path / "ctrf.json")
    reward = _read_reward(logs_path / "reward.txt")
    return reward, ctrf, image_id


class TB21OfflineVerifier(_BaseVerifier):
    """Harbor post-agent verifier for exactly the pinned TB2.1 closure."""

    def __init__(
        self,
        *,
        task_id: str,
        run_id: str,
        snapshot_digest: str,
        source_commit: str,
        source_tree: str,
        tasks_tree: str,
        authority_digest: str,
        authority_content_digest: str,
        lock_digest: str,
        closure_digest: str,
        build_input_digest: str,
        task_material_digest: str,
        closure_image_id: str,
        closure_base_image_id: str,
        expected_agent_image_id: str,
        dynamic_candidate_uv: str | bool,
        shared_run_root: str,
        tests_path: str,
        tests_digest: str,
        docker: str,
        docker_host: str,
        verifier_timeout_seconds: str | float,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.task_id = task_id
        self.run_id = run_id
        self.snapshot_digest = _require_sha256(snapshot_digest, "TB2 snapshot digest")
        self.source_commit = source_commit
        self.source_tree = source_tree
        self.tasks_tree = tasks_tree
        if (
            not re.fullmatch(r"[0-9a-f]{40}", self.source_commit)
            or not re.fullmatch(r"[0-9a-f]{40}", self.source_tree)
            or not re.fullmatch(r"[0-9a-f]{40}", self.tasks_tree)
        ):
            raise TB21OfflineVerifierError("task source tree identity is invalid")
        self.authority_digest = _require_sha256(
            authority_digest, "closure authority digest"
        )
        self.authority_content_digest = _require_sha256(
            authority_content_digest, "closure authority content digest"
        )
        self.lock_digest = _require_sha256(lock_digest, "closure lock digest")
        self.closure_digest = _require_sha256(closure_digest, "closure digest")
        self.build_input_digest = _require_sha256(
            build_input_digest, "closure build input digest"
        )
        self.task_material_digest = _require_sha256(
            "sha256:" + task_material_digest, "task material digest"
        )[len("sha256:") :]
        self.closure_image_id = _require_sha256(closure_image_id, "closure image ID")
        self.closure_base_image_id = _require_sha256(
            closure_base_image_id, "closure base image ID"
        )
        self.expected_agent_image_id = _require_sha256(
            expected_agent_image_id, "agent image ID"
        )
        if self.expected_agent_image_id != self.closure_base_image_id:
            raise TB21OfflineVerifierError(
                "agent image does not match the content-locked closure base"
            )
        if isinstance(dynamic_candidate_uv, bool):
            resolved_dynamic_candidate_uv = dynamic_candidate_uv
        elif dynamic_candidate_uv in {"true", "false"}:
            resolved_dynamic_candidate_uv = dynamic_candidate_uv == "true"
        else:
            raise TB21OfflineVerifierError("dynamic candidate flag is invalid")
        self.dynamic_candidate_uv = resolved_dynamic_candidate_uv
        if self.dynamic_candidate_uv != (task_id == RESHARD_TASK_ID):
            raise TB21OfflineVerifierError("dynamic candidate binding changed task")
        if not _RUN_ID_RE.fullmatch(run_id):
            raise TB21OfflineVerifierError("offline verifier run identity is invalid")
        self.shared_run_root = Path(shared_run_root)
        self.tests_path = Path(tests_path)
        self.tests_digest = _require_sha256(tests_digest, "original test.sh digest")
        self.docker = docker
        self.docker_host = docker_host
        self.verifier_timeout_seconds = float(verifier_timeout_seconds)

    async def verify(self) -> _VerifierResult:
        if (
            not self.shared_run_root.is_absolute()
            or self.shared_run_root.exists()
            or file_digest(self.tests_path / "test.sh") != self.tests_digest
        ):
            raise TB21OfflineVerifierError("offline verifier runtime binding drifted")
        try:
            # Killing the host-side agent worker does not stop background jobs
            # that it launched inside Harbor's main task container.  Stop that
            # container before copying /app so the independent verifier scores
            # one stable post-agent snapshot, including after agent timeout.
            await self.environment.stop_service("main")
        except Exception as exc:
            raise TB21OfflineVerifierError(
                "live agent environment could not be quiesced before verification"
            ) from exc
        self.shared_run_root.mkdir(mode=0o700, parents=True, exist_ok=False)
        snapshot_path = self.shared_run_root / "candidate-app-snapshot"
        after_path = self.shared_run_root / "candidate-app-after-verifier"
        logs_path = self.shared_run_root / "offline-logs"
        snapshot_path.mkdir(mode=0o700)
        logs_path.mkdir(mode=0o2770)
        logs_path.chmod(0o2770)
        await self.environment.download_dir("/app", snapshot_path)
        before = canonical_app_identity(snapshot_path)
        merged_verifier_env = {
            **dict(getattr(self, "override_env", {}) or {}),
            **dict(self.verifier_env or {}),
        }
        build_cache_env = {
            name: merged_verifier_env[name]
            for name in _BUILD_CACHE_NAMES
            if name in merged_verifier_env
        }
        task_verifier_env = _task_offline_env(
            self.task_id,
            {
                name: value
                for name, value in merged_verifier_env.items()
                if name not in _BUILD_CACHE_NAMES
            },
        )
        dependency_provenance: Mapping[str, object] | None = None
        image_id = self.closure_image_id
        if self.dynamic_candidate_uv:
            image_id, dependency_provenance = await asyncio.to_thread(
                _prepare_dynamic_reshard_image,
                docker=self.docker,
                docker_host=self.docker_host,
                binding=OfflineTaskBinding(
                    task_id=self.task_id,
                    family="candidate-uv",
                    closure_image_id=self.closure_image_id,
                    closure_base_image_id=self.closure_base_image_id,
                    dynamic_candidate_uv=True,
                    profile="candidate-uv",
                    test_digest=self.tests_digest,
                    task_material_digest=self.task_material_digest,
                    build_input_digest=self.build_input_digest,
                ),
                run_root=self.shared_run_root,
                verifier_env=build_cache_env,
            )
        reward, ctrf, image_id = await asyncio.to_thread(
            _run_offline_container,
            docker=self.docker,
            docker_host=self.docker_host,
            image_id=image_id,
            task_id=self.task_id,
            run_id=self.run_id,
            tests_path=self.tests_path,
            snapshot_path=snapshot_path,
            logs_path=logs_path,
            dynamic_candidate_uv=self.dynamic_candidate_uv,
            timeout_seconds=self.verifier_timeout_seconds,
            task_verifier_env=task_verifier_env,
        )
        after_path.mkdir(mode=0o700)
        await self.environment.download_dir("/app", after_path)
        after = canonical_app_identity(after_path)
        if not _same_app_tree(snapshot_path, after_path):
            raise TB21OfflineVerifierError(
                "live agent /app changed during verification"
            )
        self.trial_paths.verifier_dir.mkdir(parents=True, exist_ok=True)
        for name in ("ctrf.json", "reward.txt", "test-stdout.txt"):
            shutil.copy2(logs_path / name, self.trial_paths.verifier_dir / name)
        provenance = {
            "schema": PROVENANCE_SCHEMA,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "snapshot_digest": self.snapshot_digest,
            "source_commit": self.source_commit,
            "source_tree": self.source_tree,
            "tasks_tree": self.tasks_tree,
            "authority_digest": self.authority_digest,
            "authority_content_digest": self.authority_content_digest,
            "lock_digest": self.lock_digest,
            "closure_digest": self.closure_digest,
            "build_input_digest": self.build_input_digest,
            "task_material_digest": self.task_material_digest,
            "expected_agent_image_id": self.expected_agent_image_id,
            "closure_base_image_id": self.closure_base_image_id,
            "closure_image_id": image_id,
            "dynamic_candidate_uv": self.dynamic_candidate_uv,
            "candidate_snapshot": dict(before),
            "candidate_snapshot_after_verifier": dict(after),
            "candidate_snapshot_unchanged": True,
            "candidate_snapshot_has_dot_venv": (snapshot_path / ".venv").exists(),
            "candidate_snapshot_has_uv_lock": (snapshot_path / "uv.lock").exists(),
            "candidate_snapshot_after_has_dot_venv": (after_path / ".venv").exists(),
            "candidate_snapshot_after_has_uv_lock": (after_path / "uv.lock").exists(),
            "agent_environment_dependency_preparation": False,
            "network_mode": "none",
            "offline_env": dict(_OFFLINE_ENV),
            "task_verifier_env": task_verifier_env,
            "original_test_script": str(self.tests_path / "test.sh"),
            "original_test_script_digest": self.tests_digest,
            "ctrf": dict(ctrf),
            "reward": reward,
            **(
                {"dependency_preparation": dict(dependency_provenance)}
                if dependency_provenance is not None
                else {}
            ),
        }
        (self.trial_paths.verifier_dir / "offline-verifier-provenance.json").write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n", "utf-8"
        )
        return _VerifierResult(rewards={"reward": reward})


__all__ = [
    "AUTHORITY_SCHEMA",
    "OFFLINE_VERIFIER_IMPORT_PATH",
    "OfflineClosureAuthority",
    "OfflineTaskBinding",
    "TB21OfflineVerifier",
    "TB21OfflineVerifierError",
    "PROVENANCE_SCHEMA",
    "SNAPSHOT_SCHEMA",
    "canonical_app_identity",
    "decode_offline_closure_authority",
    "load_offline_closure_authority",
    "load_offline_ctrf_counts",
    "offline_verifier_launch_kwargs",
]
