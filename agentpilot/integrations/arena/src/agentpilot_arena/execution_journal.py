"""Durable task receipts and bounded IO recovery outside the measurement apparatus.

Only filesystem operations are retried. A dispatched solver without a receipt
is never dispatched again by this module. The original evaluator still owns
the plan, scoring and admission decision.
"""
from __future__ import annotations

from contextlib import nullcontext
import copy
from dataclasses import asdict
import errno
import hashlib
import os
from pathlib import Path
import time
import traceback

from agentpilot.evaluation import CellMeasurement, MeasurementStatus
from agentpilot_arena.admission_accounting import read
from agentpilot_arena.admission_recovery import once
from agentpilot_arena.runtime.research_admission import (
    AdmissionRecoveryRequired, _measurement, _measurement_payload,
)
from agentpilot_arena.runtime.research_worker import write_record


def retry_io(operation):
    for attempt in range(4):
        try:
            return operation()
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EINTR, errno.EAGAIN,
                                   errno.EIO, errno.ESTALE, errno.ETIMEDOUT} or attempt == 3:
                raise
            time.sleep((.05, .2, .5)[attempt])


def exception_record(error):
    """Private traceback without frame locals or credential literals."""
    message = ''.join(traceback.format_exception(error))[-24000:]
    for key, value in os.environ.items():
        if value and len(value) >= 8 and any(word in key.upper() for word in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD')):
            message = message.replace(value, '[redacted]')
    return {'exception_type': type(error).__name__, 'traceback': message, 'at': time.time()}


def cell_identity(program_id, cell):
    return {'program_id': program_id, 'cell': asdict(cell)}


class ReceiptExecutor:
    """Write a second receipt before returning a physical result to its caller."""
    def __init__(self, executor, root, *, before_dispatch=None):
        self.executor, self.root = executor, Path(root)
        self.before_dispatch = before_dispatch

    def folder(self, program_id, cell):
        # Coordinates include repeat/seed; a receipt can never cross a task.
        from agentpilot_arena.admission_accounting import digest
        return self.root / digest(cell_identity(program_id, cell))

    def measure(self, *, program_id, cell):
        from agentpilot_arena.task_scheduling import process_identity
        folder = self.folder(program_id, cell)
        identity = cell_identity(program_id, cell)
        retry_io(lambda: once(folder/'request.json', identity))
        if retry_io(lambda: (folder/'result.json').exists()):
            saved = retry_io(lambda: read(folder/'result.json'))
            if saved['request'] != identity:
                raise ValueError('Execution receipt changed coordinates')
            return _measurement(saved['measurement'])
        if retry_io(lambda: (folder/'dispatched.json').exists()):
            raise AdmissionRecoveryRequired('Dispatched task has no durable receipt; inspect execution journal')
        pool = getattr(self.executor, 'pool', None)
        physical = self.executor.executor if pool is not None else self.executor
        slot = (pool.slot(program_id=program_id, cell_id=cell.cell_id,
                          measurement_seed=cell.measurement_seed,
                          scope=getattr(self.executor, 'scope', None)) if pool else nullcontext())
        with slot:
            if self.before_dispatch is not None:
                self.before_dispatch(program_id, cell)
            retry_io(lambda: once(folder/'dispatched.json', {
                'request': identity, 'pid': os.getpid(),
                'process_identity': process_identity(os.getpid()), 'at': time.time()}))
            try:
                result = physical.measure(program_id=program_id, cell=cell)
            except OSError as error:
                # Preserve uncertainty and costs. This is never a scored zero.
                retry_io(lambda: write_record(folder/'error.private.json', exception_record(error)))
                result = CellMeasurement(cell.cell_id, program_id, MeasurementStatus.APPARATUS_FAILURE,
                    None, None, {'reason': 'host_io_failed', 'exception_type': type(error).__name__,
                                 'execution_journal': str(folder.resolve())})
            except BaseException as error:
                retry_io(lambda: write_record(folder/'error.private.json', exception_record(error)))
                raise
            if result.program_id != program_id or result.cell_id != cell.cell_id:
                raise ValueError('Physical result changed execution coordinates')
            # Never retry physical.measure if publishing this result fails.
            retry_io(lambda: once(folder/'result.json', {
                'request': identity, 'measurement': _measurement_payload(result)}))
            return result


class ResilientAdmission:
    """Reuse durable receipts after an evaluator-side IO failure."""
    def __init__(self, evaluator):
        self.evaluator = getattr(evaluator, 'evaluator', evaluator)

    def __getattr__(self, name):
        return getattr(self.evaluator, name)

    def admit(self, *, ticket_id, **arguments):
        evaluator = copy.copy(self.evaluator)
        folder = Path(evaluator.root)/hashlib.sha256(ticket_id.encode()).hexdigest()[:32]
        executor = evaluator.executor
        if hasattr(executor, 'scoped'):
            executor = executor.scoped('admit/'+ticket_id)
        receipts = ReceiptExecutor(executor, folder/'execution-journal')
        evaluator.executor = receipts
        for attempt in range(4):
            try:
                self.restore(folder, receipts)
                return evaluator.admit(ticket_id=ticket_id, **arguments)
            except Exception as error:
                retry_io(lambda: write_record(folder/'resume-error.private.json', exception_record(error)))
                cause = error
                while cause is not None and not isinstance(cause, OSError):
                    cause = cause.__cause__
                if cause is None or attempt == 3:
                    raise
                time.sleep(.2)

    @staticmethod
    def restore(folder, receipts):
        from agentpilot.evaluation import EvaluationCell
        for started in retry_io(lambda: list((folder/'cells').glob('*.started.json'))):
            result = started.with_name(started.name.replace('.started.json', '.result.json'))
            if retry_io(result.exists):
                continue
            request = retry_io(lambda: read(started))
            cell = EvaluationCell(**request['cell'])
            source = receipts.folder(request['program_id'], cell)
            if retry_io(lambda: (source/'result.json').exists()):
                value = retry_io(lambda: read(source/'result.json'))
                if value['request'] != cell_identity(request['program_id'], cell):
                    raise ValueError('Receipt identity does not match admission dispatch')
                retry_io(lambda: once(result, {'execution_id': request['execution_id'],
                                               'measurement': value['measurement']}))
            elif (source/'request.json').exists() and not (source/'dispatched.json').exists():
                # The call only queued. Preserve the marker before allowing
                # the evaluator to enqueue it again; no inference was issued.
                retry_io(lambda: once(source/'queued-started.json', request))
                retry_io(started.unlink)

    def recover(self, ticket):
        arguments = ticket['arguments'] if isinstance(ticket, dict) else ticket.arguments
        identity = ticket['id'] if isinstance(ticket, dict) else ticket.id
        return self.admit(ticket_id=identity, candidate=arguments['candidate'], control=arguments['control'])
