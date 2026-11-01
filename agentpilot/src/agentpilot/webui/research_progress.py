"""Read-only aggregate B progress; never expose task coordinates or partial scores."""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from agentpilot.persistence.sqlite import _loads


def _json(path):
    return json.loads(path.read_text())


def _process_identity(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return None if fields[0] == 'Z' else fields[19]
    except (OSError, IndexError):
        return None


def slot_owners(pool):
    """Use kernel locks, not started markers, for legacy processes."""
    owners = {}
    try:
        slots = {}
        for path in pool.glob('[0-9]*.lock'):
            info = path.stat()
            slots[(os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)] = path.name
        for line in Path('/proc/locks').read_text().splitlines():
            parts = line.split()
            if len(parts) < 6 or parts[1] != 'FLOCK':
                continue
            major, minor, inode = parts[5].split(':')
            key = (int(major, 16), int(minor, 16), int(inode))
            if key in slots:
                pid = int(parts[4]); owners[pid] = owners.get(pid, 0)+1
        return owners
    except (OSError, ValueError):
        return None


@lru_cache(maxsize=512)
def _journal(folder_name, plan_stamp, binding_stamp, cells_stamp):
    folder = Path(folder_name)
    plan = _loads((folder/'frozen-plan.private.json').read_text())['action_plan'].plan
    binding = _json(folder/'binding.private.json')
    ticket = binding['ticket_id']
    roles = {}
    unfinished = []
    last_result = None
    for role in ('control', 'candidate'):
        commit = binding[role]['commit_sha']
        side = hashlib.sha256(commit.encode()).hexdigest()[:16]
        complete = started = invalid = 0
        for ordinal, cell in enumerate(plan.cells):
            stem = hashlib.sha256(f'{ticket}/cell-{ordinal}/{side}'.encode()).hexdigest()
            result = folder/'cells'/(stem+'.result.json')
            start = folder/'cells'/(stem+'.started.json')
            if result.is_file():
                complete += 1
                invalid += _json(result)['measurement']['status'] != 'valid'
                last_result = max(last_result or 0, result.stat().st_mtime)
            elif start.is_file():
                started += 1
                unfinished.append((role, commit, cell.cell_id, str(cell.measurement_seed)))
        roles[role] = {'completed':complete, 'total':len(plan.cells), 'started_without_result':started, 'invalid':invalid}
    return roles, unfinished, last_result


def admission_progress(output, tickets, *, pool=None):
    output = Path(output).resolve()
    pool = Path(pool) if pool is not None else output.parent/'task-pool'
    process_path = output/'process.json'
    process = _json(process_path) if process_path.is_file() else {}
    pid = process.get('pid')
    alive = process.get('returncode') is None and _process_identity(pid) is not None
    active_jobs = [t for t in tickets if t.get('status') == 'running']
    single_admission = len(active_jobs) == 1 and active_jobs[0].get('kind') == 'admit'
    held = slot_owners(pool)
    try:
        capacity = _json(pool/'capacity.json')['capacity']
    except (OSError, ValueError, KeyError):
        capacity = None
    queue = []
    scheduler_available = False
    database = pool/'queue.sqlite3'
    if database.is_file():
        try:
            with sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True, timeout=.2) as db:
                db.row_factory = sqlite3.Row
                queue = [dict(row) for row in db.execute(
                    "SELECT * FROM requests WHERE owner=? AND state IN ('queued','running','abandoned')",
                    (str(output),))]
                scheduler_available = True
        except sqlite3.Error:
            pass
    values = {}
    for ticket in tickets:
        if ticket.get('kind') != 'admit':
            continue
        folder = output/'confirmation-private'/hashlib.sha256(ticket['id'].encode()).hexdigest()[:32]
        try:
            plan, binding, cells = (folder/'frozen-plan.private.json', folder/'binding.private.json', folder/'cells')
            roles, unfinished, last_result = _journal(str(folder), plan.stat().st_mtime_ns,
                binding.stat().st_mtime_ns, cells.stat().st_mtime_ns)
            roles = {k:dict(v) for k,v in roles.items()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            values[ticket['id']] = {'available':False, 'reason':'Measurement plan or journal not yet available'}
            continue
        completed = sum(r['completed'] for r in roles.values())
        total = sum(r['total'] for r in roles.values())
        running = waiting = interrupted = 0
        scoped = [r for r in queue if r.get('scope') == 'admit/'+ticket['id']]
        resume_path = folder/'resumption/status.json'
        resumed = _json(resume_path) if resume_path.is_file() else {}
        resuming = (resumed.get('status') == 'running' and resumed.get('process_identity') is not None
                    and _process_identity(resumed.get('pid')) == resumed['process_identity'])
        ticket_alive = alive or resuming
        relevant = scoped or (queue if single_admission else [])
        quality = 'scheduler' if relevant else 'legacy-slots'
        if ticket.get('status') != 'running' and not resuming:
            interrupted = len(unfinished)
            quality = 'settled' if completed == total else 'inactive'
        elif completed == total:
            quality = 'settled'
        elif (not relevant and scheduler_available and alive and held is not None
              and held.get(pid, 0) == sum(r['state']=='running' and r['pid']==pid
                  and r['process_identity']==_process_identity(pid) for r in queue)):
            quality = 'scheduler'  # Every held slot is accounted for by other tickets.
        elif relevant:
            unmatched = False
            for role, commit, cell_id, seed in unfinished:
                match = [row for row in relevant if row['program_id'] == commit and row['cell_id'] == cell_id
                         and str(row['measurement_seed']) == seed]
                row = max(match, key=lambda r:r['id']) if match else None
                if row is None and ticket_alive:
                    unmatched = True
                elif row is not None and _process_identity(row['pid']) == row['process_identity']:
                    if row['state'] == 'running':
                        running += 1; roles[role]['running'] = roles[role].get('running', 0)+1
                    elif row['state'] == 'queued':
                        waiting += 1
                    else:
                        interrupted += 1
                else:
                    interrupted += 1
            if unmatched:
                if held is not None and single_admission:
                    running = min(len(unfinished)-interrupted, held.get(pid, 0))
                    waiting = len(unfinished)-interrupted-running
                    quality = 'legacy-slots'
                else:
                    running = waiting = None
                    quality = 'owner-only'
        elif not ticket_alive:
            interrupted = len(unfinished)
            quality = 'inactive'
        elif held is not None and single_admission:
            running = min(len(unfinished), held.get(pid, 0))
            waiting = len(unfinished)-running
            # Legacy slot records identify the owner, not the exact cell/side.
            if len({r[0] for r in unfinished}) == 1:
                roles[unfinished[0][0]]['running'] = running
        else:
            running = waiting = None
            quality = 'owner-only'
        for role in roles.values():
            role.pop('started_without_result')
        queued = max(0, total-completed-running-interrupted) if running is not None else None
        values[ticket['id']] = {'available':True, 'completed':completed, 'total':total,
            'running':running, 'queued':queued, 'waiting_for_slot':waiting, 'needs_recovery':interrupted,
            'last_result_at':last_result, 'versions':roles, 'telemetry':quality,
            'shared_capacity':capacity, 'owner_slots':held.get(pid, 0) if held is not None else None}
        values[ticket['id']]['invalid_originals'] = sum(r['invalid'] for r in roles.values())
        if resuming:
            values[ticket['id']]['resuming'] = True
        if quality == 'inactive':
            values[ticket['id']]['pending'] = queued
            values[ticket['id']]['queued'] = 0
        error_path = folder/'resume-error.private.json'
        if not error_path.exists():
            error_path = folder/'execution-error.private.json'
        if quality == 'inactive' and error_path.is_file():
            values[ticket['id']]['error_type'] = _json(error_path).get('exception_type')
        recovery = folder/'recovery/status.json'
        if recovery.is_file():
            saved = _json(recovery)
            values[ticket['id']]['recovery'] = {k:saved[k] for k in (
                'planned','completed','recovered','exhausted','running','queued','needs_inspection','protocol') if k in saved}
    return values
