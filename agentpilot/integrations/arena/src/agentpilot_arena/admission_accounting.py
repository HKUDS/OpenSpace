"""Deliver completed B evidence while retaining unresolved invoices separately.

This layer never executes a task or alters the frozen admission verdict. It
also supports an external reconciler for a running, older research driver.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
import time

from agentpilot.persistence.sqlite import _loads
from agentpilot_arena.baseline_accounting import BaselineAccounting, money
from agentpilot_arena.runtime.research_admission import _artifact


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def read(path):
    if path.is_symlink():
        raise ValueError('Accounting evidence cannot be a symlink')
    return json.loads(path.read_text())


class AdmissionAccounting:
    def __init__(self, store, root, *, model, hold_usd, lookup=None, clock=time.time):
        if money(hold_usd) is None or hold_usd <= 0:
            raise ValueError('Unresolved invoices require a positive finite hold')
        self.store, self.root, self.hold, self.clock = store, Path(root), hold_usd, clock
        self.receipts = BaselineAccounting(self.root, budget=None, model=model,
            hold_usd=hold_usd, lookup=lookup, clock=clock, store=store)
        self._lock = threading.RLock()
        self._next_refresh = 0.

    def folder(self, ticket_id):
        return self.root/hashlib.sha256(ticket_id.encode()).hexdigest()[:32]

    def _record(self, ticket, result):
        folder = self.folder(ticket.id)
        public = read(folder/'result.public.json')
        if public != result:
            raise ValueError('Admission accounting must use the original saved verdict')
        binding = read(folder/'binding.private.json')
        expected = {'ticket_id':ticket.id,
                    **{k:_artifact(ticket.arguments[k], k) for k in ('candidate', 'control')}}
        if binding != expected:
            raise ValueError('Admission accounting artifact binding changed')
        plan = _loads((folder/'frozen-plan.private.json').read_text())['action_plan'].plan
        commits = [binding[k]['commit_sha'] for k in ('control', 'candidate')]
        if commits[0] == commits[1]:
            raise ValueError('Admission accounting requires distinct comparison sides')
        proof = result.get('policy_evidence') or {}
        if (proof.get('control_commit') != commits[0] or proof.get('candidate_commit') != commits[1]
                or result.get('evidence_tier') != 'adaptive_b'):
            raise ValueError('Completed admission lacks its frozen comparison identity')
        known, pending, measurements = [], [], {}
        expected_files = set()
        for ordinal, cell in enumerate(plan.cells):
            for commit in commits:
                side = hashlib.sha256(commit.encode()).hexdigest()[:16]
                execution = f'{ticket.id}/cell-{ordinal}/{side}'
                stem = hashlib.sha256(execution.encode()).hexdigest()
                path = folder/'cells'/(stem+'.result.json')
                started = read(folder/'cells'/(stem+'.started.json'))
                saved = read(path)
                raw = saved['measurement']
                if (started['execution_id'] != execution or saved['execution_id'] != execution
                        or started['program_id'] != commit or started['cell']['cell_id'] != cell.cell_id
                        or raw['program_id'] != commit or raw['cell_id'] != cell.cell_id):
                    raise ValueError('Admission measurement changed frozen coordinates')
                expected_files.add(path.name)
                measurements[path.name] = digest(saved)
                diagnostics = raw.get('diagnostics') or {}
                snapshot = diagnostics.get('provider_diagnostics') or {}
                aggregate = money(diagnostics.get('provider_cost_usd'))
                if aggregate is None:
                    aggregate = money((snapshot.get('accounting') or {}).get('cost_usd'))
                if aggregate is not None:
                    known.append(aggregate)
                    continue
                calls = snapshot.get('calls') or []
                if not calls:
                    # A host failure may precede receipt publication. Retain an
                    # explicit unknown liability instead of blocking the other
                    # completed measurements or inventing a zero charge.
                    key = 'admission-invoice/'+hashlib.sha256(f'{execution}/missing'.encode()).hexdigest()
                    pending.append({'key':key, 'execution_id':execution, 'file':path.name,
                                    'ordinal':None, 'call':None})
                    continue
                seen = set()
                for call in calls:
                    number = call.get('ordinal')
                    if type(number) is not int or number < 1 or number in seen:
                        raise ValueError('Invalid or duplicated provider-call ordinal')
                    seen.add(number)
                    cost = money(call.get('cost_usd'))
                    if call.get('cost_usd') is not None and cost is None:
                        raise ValueError('Invalid reported provider cost')
                    if cost is not None:
                        known.append(cost)
                    else:
                        key = 'admission-invoice/'+hashlib.sha256(f'{execution}/{number}'.encode()).hexdigest()
                        pending.append({'key':key, 'execution_id':execution, 'file':path.name,
                                        'ordinal':number, 'call':call})
        if {p.name for p in (folder/'cells').glob('*.result.json')} != expected_files:
            raise ValueError('Admission journal contains measurements outside the frozen plan')
        return {'ticket_id':ticket.id, 'model':self.receipts.model, 'hold_usd':self.hold,
                'verdict_sha256':digest(result), 'measurements':measurements,
                'known_cost_usd':math.fsum(known), 'pending':pending}

    def prepare(self, ticket, result):
        if ticket.kind != 'admit' or result.get('cost_usd') is not None:
            return result
        with self._lock:
            record = self._record(ticket, result)
            key = 'admission-accounting/'+ticket.id
            encoded = json.dumps(record, sort_keys=True, separators=(',', ':'))
            now = self.clock()
            # These requests already ran under the admission reservation. Book
            # every liability even if it exceeds the scheduling estimate; do
            # not let a budget check erase an incurred expense or task result.
            with self.store._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current = db.execute('SELECT status FROM tickets WHERE id=?', (ticket.id,)).fetchone()
                if current is None or current['status'] not in {'running', 'accounting_unknown', 'succeeded'}:
                    raise ValueError('Only a dispatched admission can book these invoices')
                prior = db.execute('SELECT value_json FROM documents WHERE key=?', (key,)).fetchone()
                if prior is not None and prior[0] != encoded:
                    raise ValueError('Immutable admission accounting record changed')
                db.execute('INSERT OR IGNORE INTO documents VALUES (?,?)', (key, encoded))
                for item in record['pending']:
                    db.execute("INSERT OR IGNORE INTO external_costs VALUES (?,?,'accounting_unknown',?,NULL,?,?,?)",
                        (item['key'], f'Unresolved invoice for admission {ticket.id}', self.hold,
                         'Provider receipt pending; retained separately from completed evidence', now, now))
                db.commit()
            # The immutable ticket books only its known subtotal. Later charges
            # belong to the separately reserved expenses, never this subtotal.
            return {**result, 'cost_usd':record['known_cost_usd'], 'accounting':{
                'basis':'Reported charges at completion; unresolved invoices are separate expenses',
                'pending_invoice_keys':[p['key'] for p in record['pending']]}}

    def settle_completed(self, transform=None):
        """Repair only terminal legacy tickets; never touch a running evaluator."""
        repaired = []
        for ticket in self.store.tickets(status='accounting_unknown'):
            if ticket.kind != 'admit' or not (self.folder(ticket.id)/'result.public.json').is_file():
                continue
            locks = Path(self.store.path).parent/'.research-ticket-locks'/self.store.namespace
            locks.mkdir(parents=True, exist_ok=True)
            with (locks/hashlib.sha256(ticket.id.encode()).hexdigest()).open('a') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                ticket = self.store.get_ticket(ticket.id)
                if ticket.status != 'accounting_unknown':
                    continue
                result = self.prepare(ticket, read(self.folder(ticket.id)/'result.public.json'))
                if transform is not None:
                    result = transform(ticket, result)
                    if result is None:
                        continue
                cost = result.pop('cost_usd')
                self.store.complete(ticket.id, result, cost)
                repaired.append(ticket.id)
        return repaired

    def refresh(self, *, query=True, limit=1):
        with self._lock:
            if query and self.clock() < self._next_refresh:
                return
            self._next_refresh = self.clock()+30
            with self.store._connect() as db:
                records = [json.loads(row[0]) for row in db.execute(
                    "SELECT value_json FROM documents WHERE key LIKE 'admission-accounting/%'")]
            lookups = 0
            for record in records:
                folder = self.folder(record['ticket_id'])
                for item in record['pending']:
                    expense = self.store._external_cost(item['key'])
                    if expense['state'] == 'settled':
                        continue
                    saved = read(folder/'cells'/item['file'])
                    if digest(saved) != record['measurements'][item['file']]:
                        raise ValueError('Saved admission receipt changed during reconciliation')
                    if item['call'] is None:
                        continue  # No provider identity: never guess an invoice.
                    call = self.receipts._late_call(saved['measurement'], item['call'])
                    cost = money(call.get('cost_usd'))
                    if cost is None:
                        identity = call.get('generation_id') or call.get('response_id')
                        attempt = self.store.load_checkpoint('baseline-generation/'+str(identity), {})
                        can_query = query and lookups < limit and self.clock() >= attempt.get('retry_at', 0)
                        cost, _ = self.receipts._generation(call, query=can_query, cell_id=item['execution_id'])
                        lookups += int(can_query)
                    if cost is not None:
                        self.store.settle_external(item['key'], cost)


class AccountedResearchBackend:
    def __init__(self, backend, accounting, recovery=None):
        self.backend, self.accounting, self.recovery = backend, accounting, recovery

    def __getattr__(self, name):
        return getattr(self.backend, name)

    def execute(self, ticket):
        return self._finish(ticket, self.backend.execute(ticket))

    def recover(self, ticket):
        return self._finish(ticket, self.backend.recover(ticket))

    def _finish(self, ticket, raw):
        result = self.accounting.prepare(ticket, raw)
        if self.recovery is not None and ticket.kind == 'admit' and raw.get('evidence_tier') == 'adaptive_b':
            self.recovery.run(ticket)
            result = self.recovery.deliver(ticket, result)
            if result is None:
                raise RuntimeError('Infrastructure replacement has not settled; original evidence is retained')
        return result

    def reconcile(self):
        self.accounting.refresh()
