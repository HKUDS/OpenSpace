"""Arena evaluation scheduling for the vendored Shinka search loop.

Adapted by RSI Arena: the upstream subprocess/Slurm evaluator modes are
replaced by one injected, same-process Arena evaluator.  Proposal concurrency,
the native job monitor and the rest of the search loop remain upstream-owned.
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Protocol, Tuple


class ArenaCandidateEvaluator(Protocol):
    """The only evaluator boundary used by this derivative."""

    def submit(
        self, candidate_path: Path, *, occurrence_key: str
    ) -> Dict[str, Any] | None: ...

    def post_persist(
        self, occurrence_key: str, *, native_state_reference: str
    ) -> None: ...

    def close(self) -> None: ...


@dataclass
class JobConfig:
    """Arena-only native job configuration."""

    evaluator: ArenaCandidateEvaluator = field(repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {"evaluator": "injected-arena-evaluator"}


ArenaJobConfig = JobConfig


class ArenaJobHandle:
    """Future-like handle understood by the unmodified native job monitor."""

    def __init__(
        self,
        occurrence_key: str,
        future: Future[Dict[str, Any] | None],
    ) -> None:
        self.occurrence_key = occurrence_key
        self._future = future

    def __str__(self) -> str:
        return self.occurrence_key

    def done(self) -> bool:
        return self._future.done()

    def result(self) -> Dict[str, Any] | None:
        return self._future.result()

    def cancel(self) -> bool:
        return self._future.cancel()


class JobScheduler:
    """Route Shinka's existing evaluator jobs directly into Arena."""

    def __init__(
        self,
        job_type: str,
        config: JobConfig,
        verbose: bool = False,
        max_workers: int = 4,
    ) -> None:
        if job_type != "arena":
            raise ValueError("the vendored Shinka derivative requires job_type='arena'")
        if not isinstance(config, JobConfig):
            raise TypeError("the vendored Shinka derivative requires ArenaJobConfig")
        self.job_type = job_type
        self.config = config
        self.verbose = verbose
        self.executor = ThreadPoolExecutor(max_workers=max_workers)

    @staticmethod
    def occurrence_key(results_dir: str) -> str:
        generation = Path(results_dir).parent.name
        if not generation.startswith("gen_") or not generation[4:].isdigit():
            raise ValueError(
                "Shinka Arena results directory lacks a generation identity"
            )
        return f"generation-{generation[4:]}"

    def _submit(self, exec_fname: str, results_dir: str) -> ArenaJobHandle:
        key = self.occurrence_key(results_dir)
        future = self.executor.submit(
            self.config.evaluator.submit,
            Path(exec_fname),
            occurrence_key=key,
        )
        return ArenaJobHandle(key, future)

    def run(
        self, exec_fname: str, results_dir: str
    ) -> Tuple[Dict[str, Any] | None, float]:
        started = time.time()
        handle = self._submit(exec_fname, results_dir)
        return handle.result(), time.time() - started

    def submit_async(self, exec_fname: str, results_dir: str) -> ArenaJobHandle:
        return self._submit(exec_fname, results_dir)

    def check_job_status(self, job: Any) -> bool:
        return isinstance(job.job_id, ArenaJobHandle) and not job.job_id.done()

    def get_job_results(
        self, job_id: ArenaJobHandle, _results_dir: str
    ) -> Dict[str, Any] | None:
        if not isinstance(job_id, ArenaJobHandle):
            return None
        return job_id.result()

    async def submit_async_nonblocking(
        self, exec_fname: str, results_dir: str
    ) -> ArenaJobHandle:
        return self._submit(exec_fname, results_dir)

    async def check_job_status_async(self, job: Any) -> bool:
        return self.check_job_status(job)

    async def get_job_results_async(
        self, job_id: ArenaJobHandle, results_dir: str
    ) -> Dict[str, Any] | None:
        return await asyncio.wrap_future(job_id._future)

    async def batch_check_status_async(self, jobs: list[Any]) -> list[bool]:
        return [self.check_job_status(job) for job in jobs]

    async def cancel_job_async(self, job_id: ArenaJobHandle) -> bool:
        return isinstance(job_id, ArenaJobHandle) and job_id.cancel()

    def post_persist(
        self,
        occurrence: ArenaJobHandle | str,
        *,
        native_state_reference: str,
    ) -> None:
        key = (
            occurrence.occurrence_key
            if isinstance(occurrence, ArenaJobHandle)
            else str(occurrence)
        )
        self.config.evaluator.post_persist(
            key, native_state_reference=native_state_reference
        )

    def post_persist_results_dir(
        self, results_dir: str, *, native_state_reference: str
    ) -> None:
        self.post_persist(
            self.occurrence_key(results_dir),
            native_state_reference=native_state_reference,
        )

    def shutdown(self) -> None:
        self.config.evaluator.close()
        self.executor.shutdown(wait=True, cancel_futures=True)


__all__ = [
    "ArenaCandidateEvaluator",
    "ArenaJobConfig",
    "ArenaJobHandle",
    "JobConfig",
    "JobScheduler",
]
