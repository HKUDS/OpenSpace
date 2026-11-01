"""Completion-order execution for durable research tickets."""

from __future__ import annotations

import math
import threading
import fcntl
import hashlib
import time
from pathlib import Path
from collections import deque
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol

from .models import Ticket
from .store import ResearchStore, external_worker_accounting


class ResearchBackend(Protocol):
    def capabilities(self) -> Mapping[str, Any]: ...

    def prepare(self, kind: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def execute(self, ticket: Ticket) -> Mapping[str, Any]: ...


class ResearchCoordinator:
    """Dispatch queued tickets while keeping all durable decisions in the store."""

    def __init__(
        self,
        store: ResearchStore,
        backend: ResearchBackend,
        max_workers: int = 3,
        *,
        autostart: bool = True,
        policy=None,
    ):
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1:
            raise ValueError("max_workers must be a positive integer")
        if not isinstance(autostart, bool):
            raise TypeError("autostart must be a boolean")
        self.store = store
        self.backend = backend
        self.policy = policy
        self._max_workers = max_workers
        self._waiting: dict[str, bool] = {}
        self._pump_lock = threading.RLock()
        self._policy_error = None
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="research")
        self._condition = threading.Condition()
        self._completed: deque[Ticket] = deque()
        self._scheduled: dict[str, object] = {}
        self._owner = threading.local()
        self._reported: set[tuple[str, str]] = set()
        self._closed = False
        self._accepting = True
        self._locks = Path(store.path).resolve().parent / '.research-ticket-locks' / store.namespace
        self._locks.mkdir(parents=True, exist_ok=True)
        capabilities = backend.capabilities() if hasattr(backend, "capabilities") else {}
        self.capabilities = dict(capabilities)
        self.supported_operations = tuple(self.capabilities)
        if autostart:
            self.resume()

    def submit(
        self,
        kind: str,
        arguments: Mapping[str, Any],
        *,
        idempotency_key: str,
        track_id: str | None = None,
        label: str = "",
    ) -> Ticket:
        if not self._accepting:
            raise RuntimeError("coordinator is shut down")
        if self._policy_error is not None:
            raise self._policy_error
        if self.supported_operations and kind not in self.capabilities:
            raise ValueError(f"unsupported research operation {kind!r}")
        prepared = self.backend.prepare(kind, arguments)
        if not isinstance(prepared, Mapping):
            raise TypeError("backend.prepare must return a mapping")
        prepared_arguments = prepared.get("arguments", arguments)
        reservation = prepared.get("reservation_usd")
        if reservation is None:
            raise ValueError("backend.prepare must return reservation_usd")
        selected_track = track_id if track_id is not None else prepared.get("track_id")
        selected_label = label if label else prepared.get("label", "")
        ticket = self.store.submit(
            kind, prepared_arguments, reservation, idempotency_key,
            track_id=selected_track, label=selected_label,
            followup=prepared.get('followup'),
        )
        try:
            if self.policy is not None:
                self.policy.register(ticket)
            if ticket.status == "queued":
                self._schedule(ticket.id, recovery=False)
        except Exception as error:
            if self.policy is None:
                raise
            # The ticket already exists. Return its identity, then surface the
            # strategy error from poll; never invite a second submission for an
            # operation whose reservation has already been committed.
            self._policy_error = error
            with self._condition:
                self._waiting.setdefault(ticket.id, False)
        return self.store.get_ticket(ticket.id)

    def _schedule(self, ticket_id: str, *, recovery: bool) -> bool:
        if self.policy is not None:
            with self._condition:
                if self._closed or ticket_id in self._scheduled or ticket_id in self._waiting:
                    return False
                self._waiting[ticket_id] = recovery
            self._pump()
            return True
        return self._dispatch(ticket_id, recovery=recovery)

    def _dispatch(self, ticket_id: str, *, recovery: bool) -> bool:
        with self._condition:
            if self._closed or ticket_id in self._scheduled:
                return False
            token = object()
            self._scheduled[ticket_id] = token
        try:
            self._executor.submit(self._run_recovery if recovery else self._run, ticket_id, token)
        except BaseException:
            self._unmark(ticket_id, token)
            raise
        return True

    def _pump(self):
        if self.policy is None or self._policy_error is not None:
            return
        with self._pump_lock:
            while True:
                with self._condition:
                    if self._closed or len(self._scheduled) >= self._max_workers or not self._waiting:
                        return
                    pending = dict(self._waiting)
                tickets = [self.store.get_ticket(identity) for identity in pending]
                immediate = [t for t in tickets if pending[t.id] or t.kind != 'build']
                try:
                    selected = immediate[0].id if immediate else self.policy.choose(tickets)
                except BaseException as error:
                    self._policy_error = error
                    with self._condition:
                        self._condition.notify_all()
                    raise
                with self._condition:
                    recovery = self._waiting.pop(selected)
                self._dispatch(selected, recovery=recovery)

    def _publish(self, ticket: Ticket) -> None:
        # Schedule dependent work before removing the parent from the active
        # set, so shutdown cannot mistake this transition for an empty pool.
        try:
            self._advance()
        finally:
            with self._condition:
                self._unmark(ticket.id)
                self._completed.append(ticket)
                self._condition.notify_all()
            self._pump()

    def _advance(self):
        if self._closed:
            return
        for ticket in self.store.activate_followups():
            self._schedule(ticket.id, recovery=False)

    def _unmark(self, ticket_id, token=None):
        token = token if token is not None else getattr(self._owner, 'token', None)
        with self._condition:
            if token is not None and self._scheduled.get(ticket_id) is token:
                self._scheduled.pop(ticket_id)
            self._condition.notify_all()

    def _run(self, ticket_id: str, token) -> None:
        self._owner.token = token
        try:
            self._execute_owned(ticket_id, recovery=False)
        finally:
            self._unmark(ticket_id, token)

    def _run_recovery(self, ticket_id: str, token) -> None:
        self._owner.token = token
        try:
            self._execute_owned(ticket_id, recovery=True)
        finally:
            self._unmark(ticket_id, token)

    def _execute_owned(self, ticket_id, *, recovery):
        lock_path = self._locks / hashlib.sha256(ticket_id.encode()).hexdigest()
        with lock_path.open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self._unmark(ticket_id)
                return
            ticket = self.store.get_ticket(ticket_id) if recovery else self.store.claim(ticket_id)
            if ticket is None:
                self._unmark(ticket_id)
                return
            if recovery and ticket.status not in {'running', 'accounting_unknown'}:
                self._publish(ticket)
                return
            operation = getattr(self.backend, 'recover' if recovery else 'execute', None)
            try:
                raw = self.policy.reuse(ticket, recovery=recovery) if self.policy is not None else None
                if raw is None:
                    raw = operation(ticket) if callable(operation) else None
                if raw is None and recovery:
                    self._publish(self.store.mark_accounting_unknown(ticket_id, 'Recovery has no settled result; reservation retained.'))
                    return
                if not isinstance(raw, Mapping):
                    raise TypeError('backend result must be a mapping')
                result = dict(raw)
                cost = result.pop('cost_usd', None)
                if cost is None and not (ticket.kind in {'build', 'experiment'} and external_worker_accounting(result)):
                    settled = self.store.mark_accounting_unknown(ticket_id, 'backend result omitted cost_usd')
                elif result.pop('status', 'succeeded') == 'failed':
                    error = result.pop('error', None) or 'backend reported failure'
                    settled = self.store.mark_failed(ticket_id, str(error), cost, result)
                else:
                    settled = self.store.complete(ticket_id, result, cost)
            except Exception as error:
                settled = self.store.mark_accounting_unknown(ticket_id, f'{type(error).__name__}: {error}')
            self._publish(settled)

    def resume(self) -> list[Ticket]:
        """Schedule replay-safe queued work; running work is never redispatched."""
        scheduled: list[Ticket] = []
        self._advance()
        if self.policy is not None:
            for ticket in self.store.tickets(status="queued"):
                self.policy.register(ticket)
                with self._condition:
                    if ticket.id not in self._scheduled and ticket.id not in self._waiting:
                        self._waiting[ticket.id] = False
                        scheduled.append(ticket)
            self._pump()
            return scheduled
        for ticket in self.store.tickets(status="queued"):
            if self._schedule(ticket.id, recovery=False):
                scheduled.append(ticket)
        return scheduled

    def recover_running(self) -> list[Ticket]:
        """Ask the backend to reconcile dispatched work without executing it again."""
        if not callable(getattr(self.backend, "recover", None)):
            return []
        scheduled: list[Ticket] = []
        for status in ("running", "accounting_unknown"):
            for ticket in self.store.tickets(status=status):
                if self._schedule(ticket.id, recovery=True):
                    scheduled.append(ticket)
        return scheduled

    @property
    def active_ticket_ids(self) -> frozenset[str]:
        """Snapshot tickets currently owned by this coordinator's executor."""

        with self._condition:
            return frozenset((*self._scheduled, *self._waiting))

    def poll(self) -> list[Ticket]:
        if self._policy_error is not None:
            raise self._policy_error
        reconcile = getattr(self.backend, 'reconcile', None)
        if callable(reconcile):
            reconcile()
        if self.policy is not None:
            self.policy.sync()
        self._advance()
        self._pump()
        with self._condition:
            completed = list(self._completed)
            self._completed.clear()
        # Settlement and the in-memory notification are separate steps. Reading
        # the durable terminal states closes that gap and also handles restart.
        latest = {ticket.id: ticket for ticket in completed}
        latest.update({ticket.id: ticket for ticket in self.store.tickets()
                       if ticket.status in {'succeeded', 'failed', 'accounting_unknown'}})
        for ticket_id in self.store.held_build_notifications():
            latest.pop(ticket_id, None)
        with self._condition:
            fresh = [ticket for ticket in latest.values() if (ticket.id, ticket.status) not in self._reported]
            self._reported.update((ticket.id, ticket.status) for ticket in fresh)
        return fresh

    def wait(self, timeout: float = 60.0) -> list[Ticket]:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise TypeError("timeout must be a number from 0 through 60 seconds")
        if not math.isfinite(float(timeout)):
            raise ValueError("timeout must be a number from 0 through 60 seconds")
        if timeout < 0 or timeout > 60:
            raise ValueError("timeout must be a number from 0 through 60 seconds")
        deadline = time.monotonic() + float(timeout)
        while True:
            ready = self.poll()
            if ready:
                return ready
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            with self._condition:
                self._condition.wait(remaining)

    def shutdown(self, wait: bool = False) -> None:
        with self._condition:
            if self._closed:
                return
            self._accepting = False
        if wait and self._policy_error is None:
            self.resume()
            while True:
                self._advance()
                with self._condition:
                    if not self._scheduled and not self._waiting:
                        break
                    self._condition.wait(.1)
        elif wait:
            with self._condition:
                while self._scheduled:
                    self._condition.wait(.1)
        with self._condition:
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=not wait)
