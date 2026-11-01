"""Restore development trace identities from persisted controller evidence."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from agentpilot.domain import EvidenceTier, TraceArtifactRef, TraceCaptureCoordinates
from agentpilot.search.trace_store import TraceStoreIntegrityError

from .research_worker import artifact_payload
from .trace_capture import HARBOR_TRACE_CONTRACT


def decode_artifact(value):
    if not isinstance(value, Mapping):
        raise TraceStoreIntegrityError('Development trace artifact is not a mapping')
    fields = dict(value)
    fields['evidence_tier'] = EvidenceTier(fields['evidence_tier'])
    fields['available_at'] = datetime.fromisoformat(fields['available_at'])
    return TraceArtifactRef(**fields)


def register_result_artifacts(trace_store, result):
    """New results carry exact capture metadata, independently of memory."""
    references = result.get('trace_artifacts', ())
    if not isinstance(references, (list, tuple)):
        raise TraceStoreIntegrityError('Development trace artifacts must be a sequence')
    cases = {row['trace_ref_id']: row for row in result.get('trace_cases', ())}
    for value in references:
        artifact = decode_artifact(value)
        row = cases.get(artifact.trace_ref_id)
        if row is None or any(row.get(field) != getattr(artifact, field)
                              for field in ('program_id', 'cell_id', 'task_id', 'replicate')):
            raise TraceStoreIntegrityError('Development trace artifact differs from its case')
        trace_store.register_artifact(artifact)


def _receipt(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise TraceStoreIntegrityError('Development trace recovery needs a trusted cell receipt')
    return json.loads(path.read_text())


def restore_portfolio_trace_artifacts(trace_store, store, journal_root, *, persist=True):
    """Hydrate completed A tickets; legacy timestamps are explicitly conservative.

    Legacy receipts omitted capture time. Their enclosing ticket's recorded
    completion time proves publication by then, and is never called capture time.
    Original ticket results and normalized bodies remain untouched.
    """
    root = Path(journal_root)
    recovered = []
    for ticket in store.tickets():
        if ticket.kind not in {'experiment', 'measure'} or not isinstance(ticket.result, Mapping):
            continue
        measurements = (ticket.result.get('development_measurements', ())
                        if ticket.kind == 'experiment'
                        else ({'measurement_id': ticket.id, 'result': ticket.result},))
        for measured in measurements:
            result = measured.get('result')
            if not isinstance(result, Mapping):
                continue
            register_result_artifacts(trace_store, result)
            complete_refs = {value['trace_ref_id'] for value in result.get('trace_artifacts', ())}
            for row in result.get('trace_cases', ()):
                ref = row['trace_ref_id']
                if ref in complete_refs:
                    continue
                identity = measured['measurement_id']
                folder = root / hashlib.sha256(identity.encode()).hexdigest()[:24]
                stem = hashlib.sha256(f"{row['program_id']}\0{row['cell_id']}".encode()).hexdigest()
                if root.is_symlink() or folder.is_symlink():
                    raise TraceStoreIntegrityError('Development trace receipt directory is a symlink')
                request_path = folder / (stem + '.request.json')
                result_path = folder / (stem + '.result.json')
                request, receipt = _receipt(request_path), _receipt(result_path)
                cell, diagnostics = request.get('cell', {}), receipt.get('diagnostics', {})
                capture = diagnostics.get('trace_capture', {})
                if (request.get('program_id') != row['program_id']
                        or receipt.get('program_id') != row['program_id']
                        or receipt.get('cell_id') != row['cell_id']
                        or any(cell.get(field) != row.get(field) for field in ('cell_id', 'task_id', 'replicate'))
                        or any(diagnostics.get(field) != row.get(field) for field in ('task_id', 'replicate'))
                        or capture.get('trace_ref_id') != ref
                        or capture.get('status') not in {'complete', 'partial'}):
                    raise TraceStoreIntegrityError('Development trace coordinates differ from the saved measurement')
                if capture.get('artifact_ref') is not None:
                    artifact = decode_artifact(capture['artifact_ref'])
                    provenance = {'time_basis': 'original_capture_metadata'}
                else:
                    if ticket.finished_at is None:
                        raise TraceStoreIntegrityError('Legacy trace recovery requires a completed ticket timestamp')
                    trace = trace_store.get(ref)
                    payload = trace_store.get_bytes(ref)
                    if trace.trace_contract != HARBOR_TRACE_CONTRACT:
                        raise TraceStoreIntegrityError('Legacy recovery requires a known Harbor trace format')
                    if capture.get('byte_count') != len(payload) or capture.get('event_count') != len(trace.events):
                        raise TraceStoreIntegrityError('Development capture sizes differ from normalized evidence')
                    published_at = datetime.fromtimestamp(ticket.finished_at, timezone.utc)
                    artifact = TraceArtifactRef.create(
                        coordinates=TraceCaptureCoordinates(
                            program_id=row['program_id'], commit_sha=row['program_id'],
                            tree_sha=diagnostics['program_tree'], cell_id=row['cell_id'],
                            task_id=row['task_id'], replicate=row['replicate'], available_at=published_at),
                        format='harbor-agent-trajectory', byte_count=len(payload),
                        event_count=len(trace.events), trace_contract=trace.trace_contract)
                    provenance = {'time_basis': 'enclosing_ticket_finished_at',
                                  'capture_time_known': False, 'ticket_id': ticket.id,
                                  'published_no_later_than': published_at.isoformat()}
                if artifact.trace_ref_id != ref or any(row.get(field) != getattr(artifact, field)
                        for field in ('program_id', 'cell_id', 'task_id', 'replicate')):
                    raise TraceStoreIntegrityError('Recovered development trace identity changed')
                trace_store.register_artifact(artifact)
                record = {'artifact_ref': artifact_payload(artifact), 'provenance': provenance,
                          'measurement_id': identity,
                          'request_sha256': hashlib.sha256(request_path.read_bytes()).hexdigest(),
                          'receipt_sha256': hashlib.sha256(result_path.read_bytes()).hexdigest()}
                if persist:
                    store.put_document('development-trace-recovery/' + ref, record)
                recovered.append(ref)
    return recovered
