"""Local-DIND Harbor binding for BrowseComp-Plus and isolated Terminus-2."""

from __future__ import annotations

import json
import math
import os
import secrets
import shutil
import stat
import subprocess
import uuid
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlparse

from rsi_arena.benchmarks.harbor_runtime.agent_bridge import (
    AGENT_IMPORT_PATH,
    candidate_owned_runtime_failure_marker,
    candidate_runtime_failure_from_context,
    candidate_runtime_failure_reason,
    harbor_agent_bridge_identity,
    harbor_cli_agent_args,
    load_hmac_key_file,
)
from rsi_arena.benchmarks.harbor_runtime.execution import (
    HarborExecutionError,
    cleanup_scoped_compose_cell,
    harbor_controller_identity,
    remove_credential_handle,
    resolve_docker_executable,
    resolve_immutable_docker_image,
    stage_credential_handle,
    terminate_process_group_and_cleanup,
)
from rsi_arena.benchmarks.harbor_runtime.provenance import (
    ArtifactEnvelopeExpectation,
    ResultProvenanceError,
    load_validated_trial_result,
)
from rsi_arena.contracts import canonical_digest
from rsi_arena.evaluation_gateway import GatewayCandidate, clean_standalone_git_identity

from .runtime import _TrustedInnerAgentTerminal
from .search_cli import SEARCH_SOCKET
from .usage import ModelUsage

_CANONICAL_WORKDIR = "/workspace"
_FINAL_ANSWER_SOURCE = f"{_CANONICAL_WORKDIR}/final-answer.txt"
_FINAL_ANSWER_PAYLOAD = "final-answer.txt"
_SEARCH_CLI_TARGET = "/usr/local/bin/browsecomp-search"
_PROVIDER_BASE_URL_ENV = "RSI_ARENA_BROWSECOMP_PROVIDER_BASE_URL"
_BASE_ENV_NAMES = ("PATH", "LANG", "DOCKER_HOST", "DOCKER_CONFIG")


class BrowseCompHarborSetupError(RuntimeError):
    """The configured local Harbor/DIND boundary is unavailable."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


class BrowseCompHarborRuntimeError(RuntimeError):
    """One trusted BrowseComp Harbor cell failed."""


def _positive_seconds(value: object, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise ValueError(f"{label} must be positive")
    return float(value)


def _private_key(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileExistsError:
        return
    try:
        os.write(descriptor, secrets.token_hex(32).encode("ascii") + b"\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _candidate_identity(candidate_source: object) -> tuple[Path, str]:
    if isinstance(candidate_source, GatewayCandidate):
        root = Path(candidate_source.checkout_path).resolve(strict=True)
        commit, tree = clean_standalone_git_identity(root)
        if commit != candidate_source.commit_sha or tree != candidate_source.tree_sha:
            raise BrowseCompHarborRuntimeError(
                "BrowseComp gateway candidate identity changed"
            )
        return root, tree
    if isinstance(candidate_source, (str, os.PathLike)):
        root = Path(candidate_source).resolve(strict=True)
        _commit, tree = clean_standalone_git_identity(root)
        return root, tree
    raise TypeError("BrowseComp Terminus requires a frozen candidate checkout")


def _candidate_failure(exc: Exception) -> _TrustedInnerAgentTerminal:
    marker = candidate_owned_runtime_failure_marker(exc)
    assert marker is not None
    return _TrustedInnerAgentTerminal(
        candidate=MappingProxyType(
            {
                "status": "failed",
                "terminal_reason": candidate_runtime_failure_reason(marker),
                "result": (),
            }
        ),
        candidate_runtime_failure=marker,
    )


def _local_dind_socket(value: object) -> Path:
    if not isinstance(value, str):
        raise BrowseCompHarborSetupError(
            "local_dind_required",
            "BrowseComp Terminus requires DOCKER_HOST to name a local Unix socket",
        )
    parsed = urlparse(value)
    if (
        parsed.scheme != "unix"
        or parsed.netloc
        or parsed.params
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        raise BrowseCompHarborSetupError(
            "local_dind_required",
            "BrowseComp Terminus currently supports only a local DIND Unix socket",
        )
    try:
        socket_path = Path(parsed.path).resolve(strict=True)
    except OSError as exc:
        raise BrowseCompHarborSetupError(
            "local_dind_required",
            "BrowseComp DIND Unix socket is unavailable",
        ) from exc
    if not socket_path.is_socket():
        raise BrowseCompHarborSetupError(
            "local_dind_required", "BrowseComp DIND Unix socket is unavailable"
        )
    return socket_path


def _trial_directory(job_dir: Path) -> Path:
    try:
        trials = tuple(
            child
            for child in job_dir.iterdir()
            if child.is_dir()
            and not child.is_symlink()
            and (child / "result.json").is_file()
        )
    except OSError as exc:
        raise BrowseCompHarborRuntimeError(
            "BrowseComp Harbor job artifacts are unavailable"
        ) from exc
    if len(trials) != 1:
        raise BrowseCompHarborRuntimeError(
            "BrowseComp Harbor trial artifacts are ambiguous"
        )
    return trials[0]


def _agent_timed_out(trial: Mapping[str, object]) -> bool:
    exception_info = trial.get("exception_info")
    return (
        isinstance(exception_info, Mapping)
        and exception_info.get("exception_type") == "AgentTimeoutError"
    )


def _validated_trial_terminal(
    trial: Mapping[str, object],
    *,
    trial_dir: Path,
    provider: str = "unknown",
    model: str = "unknown",
) -> _TrustedInnerAgentTerminal:
    agent_result = trial.get("agent_result")
    model_usage = _trial_model_usage(
        agent_result,
        provider=provider,
        model=model,
    )
    candidate_failure = candidate_runtime_failure_from_context(agent_result)
    if candidate_failure is not None:
        return _TrustedInnerAgentTerminal(
            candidate={
                "status": "failed",
                "terminal_reason": candidate_runtime_failure_reason(candidate_failure),
                "result": [],
            },
            trial_artifact_root=trial_dir,
            candidate_runtime_failure=candidate_failure,
            model_usage=model_usage,
        )

    agent_timeout = _agent_timed_out(trial)
    envelope_path = trial_dir / "artifacts/logs/artifacts" / "rsi-arena-envelope.json"
    try:
        envelope_path.lstat()
        envelope_present = True
    except FileNotFoundError:
        envelope_present = False
    if agent_timeout and not envelope_present:
        return _TrustedInnerAgentTerminal(
            candidate={
                "status": "failed",
                "terminal_reason": "candidate_incomplete:agent_timeout",
                "result": [],
            },
            trial_artifact_root=trial_dir,
            model_usage=model_usage,
        )

    # load_validated_trial_result already authenticated a present envelope.
    answer = (trial_dir / "artifacts/logs/artifacts" / _FINAL_ANSWER_PAYLOAD).read_text(
        encoding="utf-8"
    )
    return _TrustedInnerAgentTerminal(
        candidate={
            "status": "completed",
            "terminal_reason": (
                "completed_after_agent_timeout" if agent_timeout else "completed"
            ),
            "result": [{"type": "output_text", "output": answer}],
        },
        trial_artifact_root=trial_dir,
        model_usage=model_usage,
    )


def _trial_model_usage(
    value: object,
    *,
    provider: str,
    model: str,
) -> ModelUsage | None:
    if not isinstance(value, Mapping):
        return None

    def token(name: str) -> int | None:
        raw = value.get(name)
        if raw is None:
            return None
        if (
            isinstance(raw, bool)
            or not isinstance(raw, (int, float))
            or not math.isfinite(float(raw))
            or float(raw) < 0.0
            or not float(raw).is_integer()
        ):
            raise BrowseCompHarborRuntimeError(
                "BrowseComp Harbor model usage is invalid"
            )
        return int(raw)

    raw_cost = value.get("cost_usd")
    if raw_cost is not None and (
        isinstance(raw_cost, bool)
        or not isinstance(raw_cost, (int, float))
        or not math.isfinite(float(raw_cost))
        or float(raw_cost) < 0.0
    ):
        raise BrowseCompHarborRuntimeError("BrowseComp Harbor model cost is invalid")
    usage = ModelUsage(
        role="solver-agent",
        provider=provider,
        model=model,
        input_tokens=token("n_input_tokens"),
        output_tokens=token("n_output_tokens"),
        cache_read_tokens=token("n_cache_tokens"),
        cost_usd=float(raw_cost) if raw_cost is not None else None,
    )
    if all(
        item is None
        for item in (
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_read_tokens,
            usage.cost_usd,
        )
    ):
        return None
    return usage


def _materialize_task(
    task_dir: Path,
    *,
    task_image: str,
    instruction: str,
    agent_timeout_seconds: float,
) -> None:
    task_dir.mkdir(mode=0o700, parents=True)
    (task_dir / "environment").mkdir(mode=0o700)
    tests = task_dir / "tests"
    tests.mkdir(mode=0o700)
    (task_dir / "instruction.md").write_text(instruction, encoding="utf-8")
    (task_dir / "task.toml").write_text(
        "\n".join(
            (
                'schema_version = "1.1"',
                'artifacts = ["/logs/artifacts"]',
                "",
                "[task]",
                'name = "browsecomp-plus/task"',
                'description = "Answer one BrowseComp-Plus query with local search."',
                "",
                "[verifier]",
                "timeout_sec = 120.0",
                'environment_mode = "separate"',
                "",
                "[agent]",
                f"timeout_sec = {agent_timeout_seconds}",
                "",
                "[environment]",
                "build_timeout_sec = 600.0",
                "cpus = 1",
                "memory_mb = 4096",
                "storage_mb = 4096",
                "gpus = 0",
                'network_mode = "no-network"',
                "mcp_servers = []",
                f"workdir = {json.dumps(_CANONICAL_WORKDIR)}",
                f"docker_image = {json.dumps(task_image)}",
                "",
                "[verifier.env]",
                "",
                "[environment.env]",
                "",
                "[solution.env]",
                "",
            )
        ),
        encoding="utf-8",
    )
    verifier = tests / "test.sh"
    verifier.write_text(
        """#!/bin/bash
set -euo pipefail
mkdir -p /logs/verifier
if test -s /logs/artifacts/final-answer.txt; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
""",
        encoding="utf-8",
    )
    verifier.chmod(0o700)


def _task_instruction(query: str) -> str:
    return (
        "Answer the following BrowseComp-Plus query. The only retrieval tool is "
        '`browsecomp-search "QUERY"`, which returns JSON. Cite supporting numeric '
        "docids as `[123]`. Your final response must contain exactly these three "
        "labeled lines: `Explanation: ...`, `Exact Answer: ...`, and "
        "`Confidence: 0-100%`. When finished, write that complete response as UTF-8 "
        f"to `{_FINAL_ANSWER_SOURCE}`.\n\n"
        f"Query:\n{query}\n"
    )


def _search_mounts(endpoint: Path, cli: Path) -> str:
    return json.dumps(
        [
            {
                "type": "bind",
                "source": str(endpoint),
                "target": SEARCH_SOCKET,
                "read_only": True,
            },
            {
                "type": "bind",
                "source": str(cli),
                "target": _SEARCH_CLI_TARGET,
                "read_only": True,
            },
        ],
        separators=(",", ":"),
    )


class BrowseCompHarborInnerAgentFactory:
    """Build one search-only Harbor task for each frozen candidate and query."""

    def __init__(
        self,
        *,
        control_root: Path,
        trusted_harbor_root: Path,
        dind_bind_root: Path,
        task_image: str,
        model_runtime: Mapping[str, object],
        agent_config: Mapping[str, object],
        credential_env_name: str,
        provider_base_url: str,
        docker_path: str | Path = "docker",
        bwrap_path: str | Path = "bwrap",
        worker_timeout_seconds: float = 14_400.0,
        harbor_wall_timeout_seconds: float = 15_300.0,
        cleanup_grace_seconds: float = 30.0,
    ) -> None:
        model = model_runtime.get("model")
        provider = model_runtime.get("provider")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("BrowseComp Terminus requires agent_model.model")
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("BrowseComp Terminus requires agent_model.provider")
        if not isinstance(task_image, str) or not task_image.strip():
            raise ValueError("BrowseComp Harbor task_image must be non-empty")
        if (
            not credential_env_name.isidentifier()
            or credential_env_name.upper() != credential_env_name
        ):
            raise ValueError("BrowseComp Harbor credential_env_name is invalid")
        parsed_provider = urlparse(provider_base_url)
        provider_host = parsed_provider.hostname
        if (
            provider_host is None
            or parsed_provider.scheme not in {"http", "https"}
            or parsed_provider.username is not None
            or parsed_provider.password is not None
            or parsed_provider.fragment
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in provider_base_url
            )
        ):
            raise ValueError("BrowseComp Harbor provider_base_url is invalid")

        self.control_root = Path(control_root).expanduser().absolute()
        self.control_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.trusted_harbor_root = Path(trusted_harbor_root).expanduser().absolute()
        supplied_bind_root = Path(dind_bind_root).expanduser().absolute()
        try:
            bind_info = supplied_bind_root.lstat()
            self.dind_bind_root = supplied_bind_root.resolve(strict=True)
        except OSError as exc:
            raise BrowseCompHarborSetupError(
                "dind_bind_root_unavailable",
                "BrowseComp DIND bind root must already exist",
            ) from exc
        if stat.S_ISLNK(bind_info.st_mode) or not stat.S_ISDIR(bind_info.st_mode):
            raise BrowseCompHarborSetupError(
                "dind_bind_root_unavailable",
                "BrowseComp DIND bind root must be a real directory",
            )
        self.bind_staging_root = self.dind_bind_root / ".rsi-arena-browsecomp-runtime"
        self.bind_staging_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        staging_info = self.bind_staging_root.lstat()
        if stat.S_ISLNK(staging_info.st_mode) or not stat.S_ISDIR(staging_info.st_mode):
            raise BrowseCompHarborSetupError(
                "dind_bind_root_unavailable",
                "BrowseComp runtime root must be a real directory",
            )
        self.task_image = task_image.strip()
        self.model_runtime = MappingProxyType(dict(model_runtime))
        self.agent_config = MappingProxyType(dict(agent_config))
        self.credential_env_name = credential_env_name
        self.provider_base_url = provider_base_url
        self.provider_host = provider_host
        self.docker_path = docker_path
        self.bwrap_path = bwrap_path
        self.worker_timeout_seconds = _positive_seconds(
            worker_timeout_seconds, "BrowseComp candidate worker timeout"
        )
        self.harbor_wall_timeout_seconds = _positive_seconds(
            harbor_wall_timeout_seconds, "BrowseComp Harbor wall timeout"
        )
        self.cleanup_grace_seconds = _positive_seconds(
            cleanup_grace_seconds, "BrowseComp cleanup grace timeout"
        )
        if self.harbor_wall_timeout_seconds <= self.worker_timeout_seconds:
            raise ValueError(
                "BrowseComp Harbor wall timeout must exceed the worker timeout"
            )
        self.bridge_identity = harbor_agent_bridge_identity()
        self.search_cli = Path(__file__).with_name("search_cli.py").resolve(strict=True)
        self.key_path = self.control_root / "browsecomp-harbor-envelope.key"
        _private_key(self.key_path)
        self.hmac_key = load_hmac_key_file(self.key_path)

    @property
    def search_socket_parent(self) -> Path:
        return self.bind_staging_root / "search"

    @property
    def enforces_task_wall_timeout(self) -> bool:
        return True

    def preflight_resource_identity(self) -> Mapping[str, object]:
        """Resolve the exact Harbor controller, DIND socket, and task image."""

        try:
            controller = harbor_controller_identity(self.trusted_harbor_root)
            _local_dind_socket(os.environ.get("DOCKER_HOST"))
            docker = resolve_docker_executable(self.docker_path)
            image = resolve_immutable_docker_image(docker, self.task_image)
        except (HarborExecutionError, OSError) as exc:
            raise BrowseCompHarborSetupError(
                "harbor_preflight_failed",
                "BrowseComp Harbor controller, Docker daemon, or task image is unavailable",
            ) from exc
        self.task_image = image.runtime_ref
        return MappingProxyType(
            {
                "kind": "terminus-2-harbor",
                "controller": controller.as_dict(),
                "task_image": image.as_dict(),
                "bridge_sha256": self.bridge_identity,
                "search_cli_sha256": "sha256:"
                + canonical_digest(self.search_cli.read_text(encoding="utf-8")),
                "model_runtime_sha256": "sha256:"
                + canonical_digest(dict(self.model_runtime)),
                "agent_config_sha256": "sha256:"
                + canonical_digest(dict(self.agent_config)),
                "provider_base_url": self.provider_base_url,
                "credential_env_name": self.credential_env_name,
                "worker_timeout_seconds": self.worker_timeout_seconds,
                "harbor_wall_timeout_seconds": self.harbor_wall_timeout_seconds,
                "cleanup_grace_seconds": self.cleanup_grace_seconds,
            }
        )

    def _require_bind_path(self, path: Path, label: str) -> Path:
        try:
            resolved = Path(path).resolve(strict=True)
            resolved.relative_to(self.dind_bind_root)
        except (OSError, ValueError) as exc:
            raise BrowseCompHarborSetupError(
                "dind_path_unshared",
                f"{label} is not below the daemon-visible DIND bind root",
            ) from exc
        return resolved

    def _stage_search_cli(self, cell_root: Path) -> Path:
        mount_root = cell_root / "mounts"
        mount_root.mkdir(mode=0o700)
        staged = mount_root / "browsecomp-search"
        shutil.copyfile(self.search_cli, staged)
        staged.chmod(0o500)
        return self._require_bind_path(staged, "BrowseComp search CLI")

    def __call__(self, candidate_source: object):
        candidate, candidate_tree = _candidate_identity(candidate_source)
        terminus_source = candidate / "src/harbor/agents/terminus_2"
        if not terminus_source.is_dir():
            failure = _candidate_failure(
                FileNotFoundError("candidate Terminus-2 source is unavailable")
            )

            def missing_candidate_source(
                **_kwargs: object,
            ) -> _TrustedInnerAgentTerminal:
                return failure

            return missing_candidate_source

        def entrypoint(*, context, model_runtime, config):
            if dict(model_runtime) != dict(self.model_runtime) or dict(config) != dict(
                self.agent_config
            ):
                raise BrowseCompHarborRuntimeError(
                    "BrowseComp runtime inputs changed after construction"
                )
            endpoint = getattr(context.environment, "search_endpoint", None)
            if not isinstance(endpoint, str):
                raise BrowseCompHarborRuntimeError(
                    "BrowseComp search endpoint is unavailable"
                )
            return self._run_task(
                candidate=candidate,
                candidate_tree=candidate_tree,
                query=context.instruction,
                search_endpoint=Path(endpoint),
            )

        return entrypoint

    def _run_task(
        self,
        *,
        candidate: Path,
        candidate_tree: str,
        query: str,
        search_endpoint: Path,
    ) -> _TrustedInnerAgentTerminal:
        credential = os.environ.get(self.credential_env_name)
        if credential is None:
            raise BrowseCompHarborSetupError(
                "credential_unavailable",
                "BrowseComp Terminus provider credential is unavailable",
            )
        endpoint = self._require_bind_path(search_endpoint, "task-local search socket")
        if not endpoint.is_socket():
            raise BrowseCompHarborSetupError(
                "search_socket_unavailable",
                "BrowseComp task-local search socket is unavailable",
            )
        try:
            harbor_root = self.trusted_harbor_root.resolve(strict=True)
            candidate = candidate.resolve(strict=True)
        except OSError as exc:
            raise BrowseCompHarborSetupError(
                "harbor_input_unavailable",
                "trusted Harbor root or candidate checkout is unavailable",
            ) from exc
        harbor_bin = harbor_root / ".venv/bin/harbor"
        if not harbor_bin.is_file() or not os.access(harbor_bin, os.X_OK):
            raise BrowseCompHarborSetupError(
                "harbor_unavailable", "trusted Harbor executable is unavailable"
            )
        try:
            _local_dind_socket(os.environ.get("DOCKER_HOST"))
            docker = resolve_docker_executable(self.docker_path)
        except HarborExecutionError as exc:
            raise BrowseCompHarborSetupError(
                "docker_unavailable", "local DIND Docker CLI is unavailable"
            ) from exc

        cell_id = "browsecomp-harbor-cell-" + uuid.uuid4().hex
        cell_root = self.bind_staging_root / "cells" / cell_id
        task_dir = cell_root / "task"
        jobs_dir = cell_root / "harbor"
        for child in (cell_root, cell_root / "home", cell_root / "tmp"):
            child.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._require_bind_path(cell_root, "BrowseComp Harbor cell")
        cli = self._stage_search_cli(cell_root)
        instruction = _task_instruction(query)
        _materialize_task(
            task_dir,
            task_image=self.task_image,
            instruction=instruction,
            agent_timeout_seconds=(
                self.worker_timeout_seconds + self.cleanup_grace_seconds
            ),
        )
        task_public_digest = "sha256:" + canonical_digest(
            {"benchmark": "browsecomp_plus", "instruction": instruction}
        )
        nonce = "browsecomp-cell-" + uuid.uuid4().hex
        marker = cell_root / ".provenance-start"
        marker.write_text(uuid.uuid4().hex, encoding="ascii")
        not_before_ns = marker.stat().st_mtime_ns
        credential_handle = stage_credential_handle(cell_root, credential)
        mounts = _search_mounts(endpoint, cli)
        command = [
            str(harbor_bin),
            "run",
            "-p",
            str(task_dir),
            "-m",
            str(self.model_runtime["model"]),
            "--jobs-dir",
            str(jobs_dir),
            "--job-name",
            "cell",
            "-n",
            "1",
            "-k",
            "1",
            "--max-retries",
            "0",
            "--delete",
            "--agent-timeout-multiplier",
            "1.0",
            "--verifier-timeout-multiplier",
            "1.0",
            "--agent-setup-timeout-multiplier",
            "1.0",
            "--environment-build-timeout-multiplier",
            "1.0",
            "-q",
            "-y",
        ]
        try:
            command += harbor_cli_agent_args(
                agent_ref="builtin:terminus-2",
                agent_config=self.agent_config,
                benchmark_id="browsecomp_plus",
                candidate_checkout=candidate,
                trusted_harbor_root=harbor_root,
                codec="text_answer",
                source=_FINAL_ANSWER_SOURCE,
                nonce=nonce,
                candidate_tree_digest=candidate_tree,
                task_public_digest=task_public_digest,
                bridge_identity_digest=self.bridge_identity,
                envelope_key_file=self.key_path,
                worker_timeout_seconds=self.worker_timeout_seconds,
                cleanup_grace_seconds=self.cleanup_grace_seconds,
                bwrap_path=self.bwrap_path,
                credential_handle=credential_handle,
                credential_env_name=self.credential_env_name,
                provider_name=str(self.model_runtime["provider"]),
                provider_base_url=self.provider_base_url,
                provider_base_url_env_name=_PROVIDER_BASE_URL_ENV,
                reasoning_effort=(
                    str(self.model_runtime["reasoning_effort"])
                    if self.model_runtime.get("reasoning_effort") is not None
                    else None
                ),
                temperature=(
                    float(self.model_runtime["temperature"])
                    if self.model_runtime.get("temperature") is not None
                    else None
                ),
            )
        except BaseException:
            remove_credential_handle(credential_handle)
            raise
        command += ["--allow-agent-host", self.provider_host, "--mounts", mounts]
        process: subprocess.Popen[bytes] | None = None
        primary: BaseException | None = None
        cleanup_error: Exception | None = None
        try:
            harbor_log = cell_root / "harbor.stdout.log"
            with harbor_log.open("wb") as output:
                process = subprocess.Popen(
                    command,
                    cwd=cell_root,
                    env=self._subprocess_env(cell_root),
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                try:
                    process.wait(timeout=self.harbor_wall_timeout_seconds)
                except subprocess.TimeoutExpired as exc:
                    primary = BrowseCompHarborRuntimeError(
                        "BrowseComp Harbor task exceeded its wall timeout"
                    )
                    primary.__cause__ = exc
        except BaseException as exc:  # noqa: BLE001 - cleanup must follow every exit
            primary = exc
        finally:
            remove_credential_handle(credential_handle)
            try:
                if process is not None and process.returncode is None:
                    terminate_process_group_and_cleanup(
                        process,
                        docker=docker,
                        cell_root=cell_root,
                        cleanup_grace_seconds=self.cleanup_grace_seconds,
                    )
                else:
                    cleanup_scoped_compose_cell(
                        docker,
                        cell_root,
                        cleanup_grace_seconds=self.cleanup_grace_seconds,
                    )
            except Exception as exc:  # noqa: BLE001 - preserve the primary failure
                cleanup_error = exc
        if cleanup_error is not None:
            raise BrowseCompHarborRuntimeError(
                "BrowseComp Harbor cell cleanup failed"
            ) from cleanup_error
        if primary is not None:
            if isinstance(primary, BaseException) and not isinstance(
                primary, Exception
            ):
                raise primary
            raise BrowseCompHarborRuntimeError(
                "BrowseComp Harbor task failed"
            ) from primary
        if process is None:
            raise BrowseCompHarborRuntimeError("BrowseComp Harbor task did not start")

        expectation = ArtifactEnvelopeExpectation(
            nonce=nonce,
            codec="text_answer",
            payload_name=_FINAL_ANSWER_PAYLOAD,
            candidate_tree_digest=candidate_tree,
            task_public_digest=task_public_digest,
            bridge_identity_digest=self.bridge_identity,
            hmac_key=self.hmac_key,
        )
        try:
            trial = load_validated_trial_result(
                jobs_dir / "cell",
                expected_task_path=task_dir,
                expected_task_name="browsecomp-plus/task",
                expected_agent_name=AGENT_IMPORT_PATH,
                expected_model=str(self.model_runtime["model"]),
                not_before_ns=not_before_ns,
                require_separate_verifier=True,
                artifact_expectation=expectation,
            )
            if process.returncode != 0 and not _agent_timed_out(trial):
                raise BrowseCompHarborRuntimeError(
                    "BrowseComp Harbor task exited nonzero"
                )
            trial_dir = _trial_directory(jobs_dir / "cell")
            return _validated_trial_terminal(
                trial,
                trial_dir=trial_dir,
                provider=str(self.model_runtime["provider"]),
                model=str(self.model_runtime["model"]),
            )
        except (OSError, UnicodeDecodeError, ResultProvenanceError) as exc:
            raise BrowseCompHarborRuntimeError(
                "BrowseComp Harbor result provenance is invalid"
            ) from exc

    @staticmethod
    def _subprocess_env(cell_root: Path) -> dict[str, str]:
        environment = {
            name: os.environ[name] for name in _BASE_ENV_NAMES if name in os.environ
        }
        environment.update(
            {
                "HOME": str(cell_root / "home"),
                "TMPDIR": str(cell_root / "tmp"),
                "PYTHONPATH": str(Path(__file__).resolve().parents[3]),
                "PYTHONNOUSERSITE": "1",
            }
        )
        return environment

    def close(self) -> None:
        return None


__all__ = [
    "BrowseCompHarborInnerAgentFactory",
    "BrowseCompHarborRuntimeError",
    "BrowseCompHarborSetupError",
]
