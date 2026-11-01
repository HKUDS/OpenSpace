"""Lease-managed execution of one fully assembled local batch."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from agentpilot.app.batch_loop import BatchLoop
from agentpilot.app.bindings import AssemblyError
from agentpilot.app.contracts import BatchResult
from agentpilot.domain import PolicyMethod, utc_now
from agentpilot.persistence import (
    RunBatchLease,
    RunBatchLeaseManager,
    RunLeaseLost,
)


class _LeaseHeartbeat:
    """Renew one exact lease claim while a local batch call is active."""

    def __init__(
        self,
        *,
        manager: RunBatchLeaseManager,
        lease: RunBatchLease,
        ttl: timedelta,
        interval_seconds: float,
    ) -> None:
        self.manager = manager
        self.lease = lease
        self.ttl = ttl
        self.interval_seconds = interval_seconds
        self.error: BaseException | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"agentpilot-lease-{lease.run_id}-{lease.batch_id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.lease = self.manager.renew(
                    self.lease, now=utc_now(), ttl=self.ttl
                )
            except BaseException as exc:
                self.error = exc
                self._stop.set()
                return

    def finish(self) -> None:
        self._stop.set()
        self._thread.join()


@dataclass
class BatchRuntime:
    """One fully bound run/batch loop; no environment placeholders remain."""

    loop: BatchLoop
    run_id: str
    batch_id: str
    method: PolicyMethod
    lease_manager: RunBatchLeaseManager
    lease: RunBatchLease
    lease_ttl: timedelta
    lease_renew_interval_seconds: float
    _used: bool = False

    def run(self, *, rounds: int) -> BatchResult:
        return self._managed(
            lambda: self.loop.run(
                run_id=self.run_id, batch_id=self.batch_id, rounds=rounds
            )
        )

    def resume(self) -> BatchResult:
        return self._managed(lambda: self.loop.resume(batch_id=self.batch_id))

    def _managed(self, operation: Callable[[], BatchResult]) -> BatchResult:
        if self._used:
            raise AssemblyError("a bound batch runtime can be invoked only once")
        self._used = True
        try:
            self.lease = self.lease_manager.renew(
                self.lease, now=utc_now(), ttl=self.lease_ttl
            )
        except RunLeaseLost as exc:
            raise AssemblyError(
                "bound run lease expired or was superseded before execution"
            ) from exc
        heartbeat = _LeaseHeartbeat(
            manager=self.lease_manager,
            lease=self.lease,
            ttl=self.lease_ttl,
            interval_seconds=self.lease_renew_interval_seconds,
        )
        heartbeat.start()
        try:
            result = operation()
        except BaseException:
            heartbeat.finish()
            self._release_after_failure(heartbeat.lease)
            raise
        heartbeat.finish()
        if heartbeat.error is not None:
            self._release_after_failure(heartbeat.lease)
            raise AssemblyError("run lease renewal failed") from heartbeat.error
        try:
            self.lease_manager.release(heartbeat.lease, now=utc_now())
        except RunLeaseLost:
            pass
        return result

    def _release_after_failure(self, lease: RunBatchLease) -> None:
        try:
            self.lease_manager.release(lease, now=utc_now())
        except RunLeaseLost:
            pass


__all__ = ["BatchRuntime"]
