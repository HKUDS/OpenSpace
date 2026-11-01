"""Read-only monitoring for a multi-benchmark research experiment directory."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def snapshot(root):
    root = Path(root).resolve()
    manifest = read_json(root / 'experiment.json', {'runs': []})
    runs = []
    for entry in manifest.get('runs', []):
        output = (root / entry['output']).resolve()
        if not output.is_relative_to(root):
            continue
        row = {k: entry.get(k) for k in ('id', 'benchmark', 'method', 'status', 'blockers')}
        row.update(status=entry.get('status', 'not_started'), tickets=[], calls=0, spent=0., reserved=0.,
                   accepted=0, feedback=0, round=0, proposer_turn=0, unknown=0)
        row['result'] = read_json(output / 'session-result.json', {})
        if row['result'].get('status'):
            row['status'] = row['result']['status']
        row['provider_blocker'] = row['result'].get('blocker')
        database = output / 'portfolio.sqlite3'
        if database.is_file():
            try:
                db = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=1)
                db.row_factory = sqlite3.Row
                try:
                    db.execute('BEGIN')
                    tickets = [dict(t) for t in db.execute("SELECT id,track_id,kind,label,status,cost_usd,reservation_usd,artifact_id,created_at,finished_at,substr(json_extract(result_json,'$.summary'),1,1200) AS summary,json_extract(result_json,'$.admission_fate') AS admission FROM tickets ORDER BY created_at DESC")]
                    costs = [dict(c) for c in db.execute('SELECT state,cost_usd,reservation_usd,label FROM external_costs')]
                    row['spent'] = sum(t['cost_usd'] or 0. for t in tickets) + sum(c['cost_usd'] or 0. for c in costs)
                    row['reserved'] = sum(t['reservation_usd'] for t in tickets if t['status'] in {'queued','running','accounting_unknown'}) + sum(c['reservation_usd'] for c in costs if c['state'] != 'settled')
                    mode=db.execute("SELECT value_json FROM settings WHERE key='budget_mode'").fetchone()
                    row['budget_mode']=json.loads(mode[0]) if mode else 'reserved'
                    if row['budget_mode']=='actual':
                        row['reserved']=0.
                    row['externally_billed_reservation_usd'] = sum(
                        t['reservation_usd'] for t in tickets
                        if t['status'] in {'succeeded', 'failed'} and t['cost_usd'] is None)
                    row['reserved'] += row['externally_billed_reservation_usd']
                    row['unknown'] = sum(t['status'] == 'accounting_unknown' for t in tickets) + sum(c['state'] == 'accounting_unknown' for c in costs)
                    row['calls'] = len(costs)
                    row['active'] = sum(t['status'] in {'queued', 'running'} for t in tickets)
                    if row['active']:
                        row['status'] = 'running'
                    for i, t in enumerate(tickets):
                        if i >= 80 and t['status'] not in {'queued','running','accounting_unknown'}:
                            continue
                        t['summary'] = t['summary'] or ''
                        row['tickets'].append(t)
                    from .research_view import comparison
                    admitted = [comparison(dict(t)) for t in db.execute("SELECT * FROM tickets WHERE kind='admit'")]
                    row['accepted'] = len({t['candidate_id'] for t in admitted if t['accepted']})
                    row['feedback'] = db.execute("SELECT count(*) FROM policy_events WHERE kind='feedback'").fetchone()[0]
                    contexts = list(db.execute("SELECT key,value_json FROM checkpoints WHERE key LIKE 'portfolio-session/%'"))
                    row['round'] = len(contexts)
                    for _, value in contexts:
                        state = json.loads(value)
                        row['proposer_turn'] += state.get('turn', 0)
                    row['policy_memory'] = read_json(output / 'policy-memory-view.json', {}).get('content', '')
                    memory = db.execute("SELECT value_json FROM checkpoints WHERE key='research-policy/memory'").fetchone()
                    if memory:
                        row['policy_memory'] = json.loads(memory[0]).get('content', '')
                finally:
                    db.close()
            except (sqlite3.Error, ValueError, KeyError) as error:
                row['read_status'] = type(error).__name__
        process = read_json(output / 'process.json', {})
        if process.get('returncode') is not None and process['returncode'] != 0:
            row['status'] = 'failed'
        elif process.get('returncode') == 0 and row.get('active'):
            row['status'] = 'paused'
        elif process.get('pid') and process.get('returncode') is None:
            try:
                os.kill(int(process['pid']), 0)
            except ProcessLookupError:
                row['status'] = 'unresolved'
            except PermissionError:
                pass
            else:
                row['status'] = 'running'
        from .research_progress import admission_progress
        progress = admission_progress(output, row['tickets'], pool=root/entry['benchmark']/'task-pool')
        for ticket in row['tickets']:
            if ticket['id'] in progress:
                ticket['progress'] = progress[ticket['id']]
                if ticket['progress'].get('resuming'):
                    row['status'] = 'recovering'
        if row['result'].get('status') == 'provider_blocked':
            row['status'] = 'provider_blocked'
        if (output/'PAUSE').exists():
            row['status'] = 'draining' if any(p.get('running') for p in progress.values()) else 'paused'
            row['pause'] = read_json(output/'PAUSE', {})
        runs.append(row)
    baselines = []
    for bench in manifest.get('benchmarks', []):
        path = root / bench / 'baseline' / 'progress.json'
        baseline={'benchmark': bench, **read_json(path, {'status': 'not_started', 'completed': 0, 'total': None})}
        replacements = [read_json(p,{}) for p in (path.parent/'execution-recovery').glob('*.json')]
        if replacements:
            from agentpilot.domain import stable_id
            selected = [read_json(path.parent/'measurements'/(stable_id('formal-baseline-cell',r['replacement_id'])+'.json'),{})
                        for r in replacements if r.get('replacement_id')]
            baseline['recovery'] = {'original_invalid':len(replacements), 'planned':len(selected),
                'completed':sum(bool(r) for r in selected), 'valid':sum(r.get('status')=='valid' for r in selected)}
        billing=read_json(path.parent/'billing.json',{})
        if billing:
            baseline.update(spent=billing.get('known_cost_usd'),reserved=billing.get('held_usd'),
                accounting_status=billing.get('status'),pending_cost_cells=billing.get('pending_cells',0),
                pending_cost_calls=billing.get('pending_calls',0))
        if (root/'PAUSE').is_file() and baseline.get('status') == 'running':
            baseline['status'] = 'draining'
        baselines.append(baseline)
    return {'updated_at': time.time(), 'name': manifest.get('name', root.name),
            'runs': runs, 'baselines': baselines, 'settings': manifest.get('settings', {}),
            'blockers': manifest.get('blockers', [])}


def serve(root, host='127.0.0.1', port=18915):
    if host not in {'127.0.0.1', 'localhost', '::1'}:
        raise ValueError('Research monitor binds to loopback only')
    from .server import serve as serve_inspector
    return serve_inspector(research_root=root, host=host, port=port)
