"""Audited recovery of a pre-research baseline interrupted by the refusal-cost bug.

Original source, configurations, measurements and invoices are never replaced.
Only the identified infrastructure failure gets a new attempt. Valid wrong
answers remain evidence, and no B comparison can be imported across the patch.
"""
from __future__ import annotations

import ast
from contextlib import ExitStack
from dataclasses import replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

from agentpilot.domain import stable_id
from agentpilot_arena.runtime.research_worker import write_record

BROKER = 'arena/src/rsi_arena/benchmarks/harbor_runtime/provider_broker.py'
APPARATUS = tuple(f'agentpilot/integrations/arena/src/agentpilot_arena/{b}/apparatus.py' for b in ('aime', 'tb2'))
RECOVERY_FILES = (BROKER, *APPARATUS, *(f'agentpilot/integrations/arena/src/agentpilot_arena/{f}.py'
                                    for f in ('formal', 'research_setup', 'baseline_recovery')))
OLD_SUM = 'sum(float(record.cost_usd) if record.cost_usd is not None else .1\n                        for record in settled_calls)'
NEW_SUM = 'sum(float(record.cost_usd) for record in settled_calls\n                        if record.cost_usd is not None)'
OLD_PROFILE = 'bounded-service-recovery-v1:4-attempts:180s:0.10usd-pending-hold'
NEW_PROFILE = 'bounded-service-recovery-v2:4-attempts:180s:reported-cost-stop:external-pending-holds'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_once(path, value):
    if path.exists():
        if json.loads(path.read_text()) != value:
            raise ValueError(f'Recovery record already differs: {path}')
    else:
        write_record(path, value)


def refusal_cost_bug(raw, cap):
    """Require actual recovered refusals and the precise false stop arithmetic."""
    d = raw.get('diagnostics', {})
    p = d.get('provider_diagnostics') or {}
    stops = (p.get('provider') or {}).get('local_rejection_counts') or {}
    calls = p.get('calls') or []
    if (raw.get('status') != 'apparatus_failure' or d.get('reason') != 'provider_upstream_unavailable'
            or not stops.get('settled_cost_stop') or any(v for k, v in stops.items() if k != 'settled_cost_stop')
            or p.get('worker_deadline_reached') or not calls):
        return False
    successes = {c.get('request_group') for c in calls if c.get('status') == 'completed'
                 and type(c.get('http_status')) is int and 200 <= c['http_status'] < 300
                 and c.get('provider_error_code') is None and not c.get('retryable_refusal')}
    known, pending = 0., 0
    for c in calls:
        if c.get('status') != 'completed':
            return False
        if c.get('retryable_refusal'):
            if c.get('request_group') is None or c['request_group'] not in successes:
                return False
        elif c.get('request_group') not in successes or c.get('cost_usd') is None:
            return False
        if c.get('cost_usd') is None:
            pending += 1
        else:
            known += c['cost_usd']
    return known < cap and known + .1 * pending >= cap


def recovered_plan(config_path, root, cells, identity):
    """Load the explicitly authorized attempt selection, leaving old rows intact."""
    transition = Path(config_path).with_suffix('.runtime-transition.json')
    if transition.exists():
        record=json.loads(transition.read_text())
        previous=Path(record['previous_config'])
        if (record['config_sha256'] != sha(config_path) or record['previous_config_sha256'] != sha(previous)
                or record['baseline_root'] != str(Path(root).resolve())):
            raise ValueError('Runtime transition changed its frozen configuration binding')
        # Reuse A observations with their original lineage. B measurements are
        # never relabelled by this transition and must already have settled.
        for name, expected in record['measurements'].items():
            if Path(name).name != name or sha(Path(root)/'measurements'/name) != expected:
                raise ValueError('Historical baseline measurement changed during runtime repair')
        return recovered_plan(previous,root,cells,{**identity,'config_sha256':sha(previous)})
    path = Path(config_path).with_suffix('.recovery.json')
    if not path.exists():
        return cells, identity, None
    record = json.loads(path.read_text())
    if record['config_sha256'] != identity['config_sha256'] or record['baseline_root'] != str(Path(root).resolve()):
        raise ValueError('Recovery does not match this configuration and baseline')
    if record['cells'] != identity['cells'] or record['repeats'] != identity['repeats']:
        raise ValueError('Recovery cannot change tasks, repetitions or their ordering')
    for name, digest in record['preserved_measurements'].items():
        if Path(name).name != name or sha(Path(root)/'measurements'/name) != digest:
            raise ValueError('A preserved baseline measurement changed')
    retries = record['retry_cells']
    if not set(retries) <= {c.cell_id for c in cells}:
        raise ValueError('Recovery names a cell outside this baseline')
    selected = [replace(c, cell_id=retries[c.cell_id]) if c.cell_id in retries else c for c in cells]
    if len({c.cell_id for c in selected}) != len(cells):
        raise ValueError('Duplicate recovery attempt identity')
    for old, new in retries.items():
        raw = json.loads((Path(root)/'measurements'/(stable_id('formal-baseline-cell', old)+'.json')).read_text())
        if new != old + '/recovery-' + record['id'] or not refusal_cost_bug(raw, record['cell_cost_cap']):
            raise ValueError('Recovery may only retry the identified infrastructure failure')
    original = {**identity, 'config_sha256': record['previous_config_sha256']}
    return selected, original, record


def build_configs(previous_path, destination, result_path):
    """Run in the new frozen interpreter so apparatus hashes describe its code."""
    from agentpilot.app import load_app_config
    from agentpilot_arena.research_setup import apparatus, toml_text
    from agentpilot_arena.runtime.research_environment import ResearchEnvironment
    previous = json.loads(Path(previous_path).read_text())
    runs = []
    for run in previous['runs']:
        raw = tomllib.loads(Path(run['config']).read_text())
        new = Path(destination)/'config'/(run['id']+'.toml')
        new.parent.mkdir(parents=True, exist_ok=True)
        # The only setting that changes is the identity of the repaired apparatus.
        digest = apparatus(ResearchEnvironment(load_app_config(run['config'])))
        raw['environment']['settings']['root_seed']['apparatus_digest'] = digest
        content = toml_text(raw)
        if new.exists() and new.read_text() != content:
            raise ValueError('Staged recovery config changed')
        new.write_text(content)
        ResearchEnvironment(load_app_config(new), validate=True)
        runs.append({**run, 'config':str(new), 'config_sha256':sha(new)})
    write_once(Path(result_path), runs)


def repair_baseline(args):
    from agentpilot_arena.formal import inventory, load_manifest
    root = args.output.resolve()
    previous = load_manifest(args)
    with ExitStack() as stack:
        for bench in previous['benchmarks']:
            for file in (root/bench/'driver.lock', root/bench/'baseline/collection.lock'):
                file.parent.mkdir(parents=True, exist_ok=True)
                handle = stack.enter_context(file.open('a'))
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if list(root.glob('*/*/portfolio.sqlite3')) or list(root.glob('*/baseline/packet/manifest.json')):
            raise ValueError('Recovery is limited to baseline collection before research or B comparisons')
        if previous.get('baseline_repair'):
            print(json.dumps({'status':'already_repaired', 'id':previous['baseline_repair']['id']}))
            return 0
        old_source = Path(previous['source'])
        old_broker = (old_source/BROKER).read_text()
        new_broker = (args.workspace/BROKER).read_text()
        if old_broker.count(OLD_SUM) != 1 or ast.dump(ast.parse(old_broker.replace(OLD_SUM, NEW_SUM))) != ast.dump(ast.parse(new_broker)):
            raise ValueError('Broker repair must only remove the invented refusal charges')
        for name in APPARATUS:
            expected = (old_source/name).read_text().replace(OLD_PROFILE, NEW_PROFILE)
            if ast.dump(ast.parse(expected)) != ast.dump(ast.parse((args.workspace/name).read_text())):
                raise ValueError('Apparatus repair must only version the cost-stop policy')
        changes = {name:sha(args.workspace/name) for name in RECOVERY_FILES}
        identity = hashlib.sha256(json.dumps(changes, sort_keys=True).encode()).hexdigest()[:12]
        audit = root/'baseline-repairs'/identity
        source = root/('source-repair-'+identity)
        expected = {**previous['source_sha256'], **changes}
        if not source.exists():
            shutil.copytree(old_source, source, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo'))
            for name in RECOVERY_FILES:
                shutil.copyfile(args.workspace/name, source/name)
        if inventory(source) != expected:
            raise ValueError('Frozen repair contains unexpected source changes')
        write_once(audit/'previous-experiment.json', previous)
        # A clean interpreter is necessary: importing the overlay into this
        # process would retain old module identities in sys.modules.
        code = ('import sys; from pathlib import Path; root=Path(sys.argv[1]); '
                'sys.path[:0]=[str(root/p) for p in ("agentpilot/src","agentpilot/integrations/arena/src","arena/src")]; '
                'from agentpilot_arena.baseline_recovery import build_configs; build_configs(*sys.argv[2:])')
        env = dict(os.environ)
        if previous.get('host_tmpdir'):
            env['RSI_ARENA_HOST_TMPDIR'] = previous['host_tmpdir']
        subprocess.run([sys.executable, '-c', code, str(source), str(audit/'previous-experiment.json'),
                        str(audit), str(audit/'runs.json')], check=True, cwd=args.workspace, env=env)
        runs = json.loads((audit/'runs.json').read_text())
        records = {}
        for bench in previous['benchmarks']:
            baseline = root/bench/'baseline'
            old_run = next(r for r in previous['runs'] if r['benchmark']==bench and r['method']=='memory')
            new_run = next(r for r in runs if r['id']==old_run['id'])
            plan_path = root/bench/'launch-plan.json'
            if not plan_path.exists():
                continue
            plan = json.loads(plan_path.read_text())
            raw_config = tomllib.loads(Path(old_run['config']).read_text())
            cap = raw_config['environment']['settings']['cell_runtime']['settled_cost_stop_usd']
            from agentpilot.research import ResearchStore
            store = ResearchStore(baseline/'accounting.sqlite3', budget_usd=plan['budget_usd'])
            old_plan = store.load_document('baseline-plan')
            if not old_plan or old_plan['config_sha256'] != old_run['config_sha256']:
                raise ValueError('Saved baseline plan differs from the original configuration')
            preserved, retries, saved_ids = {}, {}, set()
            for file in sorted((baseline/'measurements').glob('*.json')):
                raw = json.loads(file.read_text())
                if raw['cell_id'] not in old_plan['cells'] or file.stem != stable_id('formal-baseline-cell', raw['cell_id']):
                    raise ValueError('Unexpected baseline measurement identity')
                saved_ids.add(file.stem)
                preserved[file.name] = sha(file)
                if raw['status'] != 'valid':
                    if not refusal_cost_bug(raw, cap):
                        raise ValueError(f'Unrelated infrastructure failure requires separate review: {raw["cell_id"]}')
                    retries[raw['cell_id']] = raw['cell_id']+'/recovery-'+identity
            if any(p.stem not in saved_ids for p in (baseline/'started').glob('*.json')):
                raise ValueError('An unfinished dispatch has no receipt; settle it before recovery')
            record = {'id':identity, 'baseline_root':str(baseline), 'cells':old_plan['cells'],
                      'repeats':old_plan['repeats'], 'cell_cost_cap':cap, 'retry_cells':retries,
                      'previous_config_sha256':old_plan['config_sha256'], 'config_sha256':new_run['config_sha256'],
                      'preserved_measurements':preserved}
            write_once(Path(new_run['config']).with_suffix('.recovery.json'), record)
            records[bench] = record
        update = {**previous, 'source':str(source), 'source_sha256':expected, 'runs':runs,
                  'baseline_repair':{'id':identity, 'audit':str(audit), 'previous_source':str(old_source),
                    'changed_files':list(RECOVERY_FILES), 'reason':'Remove invented refusal charges from the settled-cost stop',
                    'evidence_reuse':'Pre-research visible-A observations retain their original source and config lineage; no B comparisons reused'}}
        write_once(audit/'recovery.json', {'benchmarks':records, 'manifest':update})
        # Commit only after all source/config/evidence checks pass. Until here
        # the original manifest, baseline and launch path are still usable.
        write_record(root/'experiment.json', update)
    print(json.dumps({'status':'repaired', 'id':identity, 'paid_calls':0,
                      'retry_cells':{b:r['retry_cells'] for b,r in records.items()}}))
    return 0
