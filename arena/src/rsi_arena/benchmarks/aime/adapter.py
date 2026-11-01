"""BenchAdapter: Harbor/Terminus-2 (candidate checkout) × AIME 2024+2025.

Problems are rendered into Harbor task directories, and the evolved
checkout's own ``harbor`` runs each one in local Docker. The shared Harbor
adapter owns those mechanics; this module owns only AIME identity and tasks:

- task content is pinned by the committed snapshot digest, not by a tasks
  repo SHA (the task tree is *generated* from the snapshot at adapter
  creation, deterministically and idempotently);
- the judge is a strict exact-match check on the trusted bridge artifact
  ``/logs/artifacts/rsi-arena-answer.txt`` in a fresh separate verifier —
  deterministic and without an LLM judge;
- agent containers are offline (``allow_internet = false``).

Clusters are ``aime-<year>/<difficulty>`` so the A/B/C split and wave
stratification spread across both years and difficulty bands.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from rsi_arena.benchmarks.aime.tasks import (
    SNAPSHOT_PATH,
    build_task_dirs,
    content_digest,
    difficulty_of,
    load_snapshot,
)
from rsi_arena.benchmarks.harbor_runtime.adapter import HarborBench
from rsi_arena.benchmarks.harbor_runtime.materials import task_tree_digest
from rsi_arena.benchmarks.harbor_runtime.verifier_settings import VerifierSettings
from rsi_arena.benchmarks.task_records import restrict_tasks
from rsi_arena.contracts import BenchIdentity, TaskRef, canonical_digest

_AGENT_TIMEOUT_SEC = 1200.0
_VERIFIER_TIMEOUT_SEC = 120.0
_BUILD_TIMEOUT_SEC = 600.0


def _to_task_records(
    problems: list[dict], *, agent_timeout_seconds: float
) -> list[dict]:
    """Build Harbor task records; ``category`` carries the year so the
    category×level cluster stratifies over both year and difficulty."""
    return [
        {
            "task_id": p["task_id"],
            "level": difficulty_of(p["number"]),
            "category": f"aime-{p['year']}",
            "tags": ["aime", f"contest-{p['contest'].lower()}"],
            "agent_timeout_sec": agent_timeout_seconds,
            "verifier_timeout_sec": _VERIFIER_TIMEOUT_SEC,
            "build_timeout_sec": _BUILD_TIMEOUT_SEC,
            "allow_internet": False,
            "expected_answer": int(p["answer"]),
        }
        for p in problems
    ]


class AimeBench(HarborBench):
    def __init__(
        self,
        *,
        snapshot_pin: str,
        expected_answers: Mapping[str, int],
        **kwargs,
    ):
        super().__init__(benchmark_id="aime", **kwargs)
        self._snapshot_pin = snapshot_pin
        task_ids = {task.task_id for task in self.list_tasks()}
        answers = dict(expected_answers)
        if set(answers) != task_ids or any(
            isinstance(answer, bool)
            or not isinstance(answer, int)
            or not 0 <= answer <= 999
            for answer in answers.values()
        ):
            raise ValueError("AIME expected-answer authority differs from tasks")
        self._expected_answers = answers

    def identity(self) -> BenchIdentity:
        agent_digest = canonical_digest(self._execution_identity_payload())
        return BenchIdentity(
            name="aime-2024-2025",
            judge_identity="aime-exact-match@"
            + canonical_digest(
                {
                    "snapshot": self._snapshot_pin,
                    "tasks": self._tasks_digest,
                    "materials": self._materials_digest,
                    "agent": agent_digest,
                }
            ),
        )

    def isolation_capabilities(self):
        capabilities = super().isolation_capabilities()
        return replace(
            capabilities,
            # The fresh verifier parses one strict 0--999 integer.  Unlike a
            # source-tree artifact, it never executes candidate-owned code.
            non_executable_artifact_verifier=self._physical_isolation,
        )

    def _artifact_reward_matches(
        self,
        *,
        out_dir: Path,
        task: TaskRef,
        payload_name: str,
        reward: float,
    ) -> bool:
        payloads = tuple(
            (out_dir / "cell").glob(f"*/artifacts/logs/artifacts/{payload_name}")
        )
        if len(payloads) != 1 or not payloads[0].is_file():
            return False
        try:
            raw = payloads[0].read_bytes()
            answer = int(raw.rstrip(b"\n"), 10)
            expected = self._expected_answers[task.task_id]
        except (KeyError, OSError, TypeError, ValueError):
            return False
        expected_reward = answer == expected
        return (float(reward) >= self._pass_reward_threshold) is expected_reward


def create_adapter(
    *,
    tasks_root: str,
    artifacts_root: str = "aime-artifacts",
    snapshot_path: str = "",
    years: list[int] = (),
    levels: list[str] = (),
    include_task_ids: list[str] = (),
    exclude_task_ids: list[str] = (),
    max_tasks: int = 0,
    subset_seed: int = 0,
    model: str = "openrouter/qwen/qwen3.7-plus",
    reasoning_effort: str | None = None,
    reasoning_enabled: bool | None = None,
    temperature: float | None = None,
    agent_ref: str,
    agent_config: dict,
    env_passthrough: list[str] = (),
    docker_host: str = "",
    docker_path: str = "docker",
    harbor_max_retries: int = 0,
    worker_timeout_seconds: float = _AGENT_TIMEOUT_SEC,
    cleanup_grace_seconds: float = 30.0,
    uv_path: str = "uv",
    physical_isolation: bool = False,
    trusted_harbor_root: str = "",
    bridge_bwrap_path: str = "bwrap",
    materialize_selected_only: bool = False,
    agent_timeout_seconds: float = _AGENT_TIMEOUT_SEC,
    pre_rendered_material_digest: str = "",
    answer_artifact_source: str = "/app/answer.txt",
    provider_name: str | None = None,
    provider_base_url: str | None = None,
    provider_base_url_env_name: str | None = None,
    credential_env_name: str | None = None,
) -> AimeBench:
    root = Path(os.path.expanduser(tasks_root))
    snapshot_file = (
        Path(os.path.expanduser(snapshot_path)) if snapshot_path else SNAPSHOT_PATH
    )
    problems = load_snapshot(snapshot_file)
    if years:
        problems = [p for p in problems if p["year"] in set(years)]
    if (
        isinstance(agent_timeout_seconds, bool)
        or not isinstance(agent_timeout_seconds, (int, float))
        or not 0 < float(agent_timeout_seconds) <= 86_400
    ):
        raise ValueError("agent_timeout_seconds must be in (0, 86400]")
    resolved_agent_timeout = float(agent_timeout_seconds)
    pin = content_digest(problems, agent_timeout_sec=resolved_agent_timeout)
    tasks = restrict_tasks(
        _to_task_records(problems, agent_timeout_seconds=resolved_agent_timeout),
        levels=tuple(levels),
        include_task_ids=tuple(include_task_ids),
        exclude_task_ids=tuple(exclude_task_ids),
        max_tasks=max_tasks,
        seed=subset_seed,
    )
    if not tasks:
        raise ValueError("restricted AIME subset is empty; loosen the filters")
    if pre_rendered_material_digest:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", pre_rendered_material_digest):
            raise ValueError("pre_rendered_material_digest must be a sha256 digest")
        actual = "sha256:" + canonical_digest(
            [
                (problem["task_id"], task_tree_digest(root / problem["task_id"]))
                for problem in sorted(problems, key=lambda item: item["task_id"])
            ]
        )
        if actual != pre_rendered_material_digest:
            raise ValueError("pre-rendered AIME task material changed")
        pin = actual
    else:
        rendered_problems = problems
        if materialize_selected_only:
            selected = {task["task_id"] for task in tasks}
            rendered_problems = [
                problem for problem in problems if problem["task_id"] in selected
            ]
        build_task_dirs(
            rendered_problems, root, agent_timeout_sec=resolved_agent_timeout
        )
    selected_task_ids = {task["task_id"] for task in tasks}
    return AimeBench(
        snapshot_pin=pin,
        expected_answers={
            problem["task_id"]: int(problem["answer"])
            for problem in problems
            if problem["task_id"] in selected_task_ids
        },
        tasks=tasks,
        tasks_root=root,
        artifacts_root=Path(artifacts_root),
        model=model,
        reasoning_effort=reasoning_effort,
        reasoning_enabled=reasoning_enabled,
        temperature=temperature,
        agent_ref=agent_ref,
        agent_config=agent_config,
        env_passthrough=tuple(env_passthrough),
        docker_host=docker_host,
        docker_path=docker_path,
        harbor_max_retries=harbor_max_retries,
        worker_timeout_seconds=worker_timeout_seconds,
        cleanup_grace_seconds=cleanup_grace_seconds,
        uv_path=uv_path,
        pass_reward_threshold=1.0,
        physical_isolation=physical_isolation,
        trusted_harbor_root=(
            Path(os.path.expanduser(trusted_harbor_root))
            if trusted_harbor_root
            else None
        ),
        bridge_bwrap_path=bridge_bwrap_path,
        artifact_codec="integer_answer" if physical_isolation else None,
        artifact_source=answer_artifact_source if physical_isolation else None,
        # Harbor has already copied the candidate-authored answer into its
        # artifact convention directory when the trusted bridge exports its
        # signed copy. Keep the signed payload on a distinct write-once path.
        artifact_payload_name=(
            "rsi-arena-signed-answer.txt" if physical_isolation else None
        ),
        provider_name=provider_name,
        provider_base_url=provider_base_url,
        provider_base_url_env_name=provider_base_url_env_name,
        credential_env_name=credential_env_name,
        verifier_settings=VerifierSettings.from_mapping(
            {"timeout_multiplier": 1.0, "task_adaptations": {}}
        ),
    )
