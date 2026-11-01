"""Formal multi-benchmark research launcher with explicit operator budgets."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import fcntl
import hashlib
import json
import math
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from contextlib import ExitStack

from agentpilot.app import load_app_config
from agentpilot.app.cli import _load_env_file
from agentpilot_arena.research_setup import prepare_config, collect_baseline, BaselinePaused
from agentpilot_arena.runtime.research_environment import ResearchEnvironment
from agentpilot_arena.runtime.research_worker import write_record

SOURCES = ('agentpilot/src', 'agentpilot/integrations/arena/src', 'arena/src')
WRAPPERS = ('agentpilot/experiments/run_formal_research.py', 'agentpilot/experiments/run_research_portfolio.py')
ACCOUNTING_FILES = ('agentpilot/integrations/arena/src/agentpilot_arena/formal.py',
                    'agentpilot/integrations/arena/src/agentpilot_arena/research_setup.py',
                    'agentpilot/integrations/arena/src/agentpilot_arena/baseline_accounting.py')


def validate_provider_policy(manifest):
    """An older frozen launcher cannot bypass today's operator exclusions."""
    from rsi_arena.benchmarks.harbor_runtime.provider_broker import provider_policy, provider_routes
    current=provider_policy()
    if (not set(current['excluded_providers']) <= set(manifest.get('excluded_providers', [])) or
            any(not set(routes) <= set(manifest.get('model_excluded_providers',{}).get(model,[]))
                for model,routes in current['model_excluded_providers'].items())):
        raise ValueError('This frozen experiment predates the current provider exclusions; prepare a new experiment')
    for key,model in (('proposer_provider_only','qwen/qwen3.8-27b'),('policy_provider_only','z-ai/glm-5.3-flash')):
        provider_routes(manifest.get(key),model)
    for run in manifest.get('runs', []):
        if not run.get('config_sha256'):
            continue
        raw=tomllib.loads(Path(run['config']).read_text())
        runtime=raw['environment']['settings']['action_runtime']
        for role in ('program', 'codex'):
            provider_routes(runtime[role].get('provider_only'),runtime[role].get('model_name') or runtime[role].get('model'))


def upgrade_accounting(args):
    """Version the scheduler without changing any frozen measurement code."""
    data=load_manifest(args)
    root=args.output
    with ExitStack() as stack:
        for bench in data['benchmarks']:
            (root/bench).mkdir(exist_ok=True)
            lock=stack.enter_context((root/bench/'driver.lock').open('a'))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            baseline=root/bench/'baseline'
            if baseline.exists():
                lock=stack.enter_context((baseline/'collection.lock').open('a'))
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if list(root.glob('*/*/portfolio.sqlite3')):
            raise ValueError('Accounting upgrade is limited to runs that have not dispatched research policies')
        selected={name:hashlib.sha256((args.workspace/name).read_bytes()).hexdigest() for name in ACCOUNTING_FILES}
        if all(data['source_sha256'].get(name)==digest for name,digest in selected.items()):
            return 0
        identity=hashlib.sha256(json.dumps(selected,sort_keys=True).encode()).hexdigest()[:12]
        destination=root/('source-accounting-'+identity)
        if not destination.exists():
            shutil.copytree(data['source'],destination,ignore=shutil.ignore_patterns('__pycache__','*.pyc','*.pyo'))
            for name in ACCOUNTING_FILES:
                shutil.copyfile(args.workspace/name,destination/name)
        expected={**data['source_sha256'],**selected}
        if inventory(destination)!=expected:
            raise ValueError('Accounting overlay changed files outside its three-file allowlist')
        audit=root/'accounting-upgrades'/identity
        audit.mkdir(parents=True,exist_ok=True)
        if not (audit/'previous-experiment.json').exists():
            write_record(audit/'previous-experiment.json',data)
        updated={**data,'source':str(destination),'source_sha256':expected,
                 'accounting_upgrade':{'id':identity,'previous_source':data['source'],'changed_files':list(ACCOUNTING_FILES)}}
        write_record(root/'experiment.json',updated)
    print(json.dumps({'status':'upgraded','output':str(root),'measurement_code':'unchanged','paid_calls':0}))
    return 0


def reconcile_baselines(args, manifest):
    from agentpilot_arena.baseline_accounting import accountant
    _load_env_file(args.env_file or args.workspace/'.env')
    result=[]
    for bench in args.benchmarks:
        plan_path=args.output/bench/'launch-plan.json'
        if not plan_path.exists():
            continue
        with (args.output/bench/'driver.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            plan=json.loads(plan_path.read_text())
            run=next(r for r in manifest['runs'] if r['benchmark']==bench and r['method']=='memory')
            root=args.output/bench/'baseline'
            billing=accountant(run['config'],root,budget=plan['budget_usd'],cell_allowance=plan['cell_allowance_usd']).refresh(query=True)
            path=root/'progress.json'
            if path.exists():
                progress=json.loads(path.read_text())
                if progress.get('invalid',0)==0 and progress.get('status')=='unresolved' and 'cost' in str(progress.get('error','')):
                    progress.update(status='paused',error='Ready to resume remaining tasks; saved results are retained')
                progress.update(spent=billing['known_cost_usd'],reserved=billing['held_usd'],accounting_status=billing['status'],
                    pending_cost_cells=billing['pending_cells'],pending_cost_calls=billing['pending_calls'])
                write_record(path,progress)
            result.append({'benchmark':bench,**{k:v for k,v in billing.items() if k!='cells'}})
    print(json.dumps({'status':'reconciled','benchmarks':result,'paid_calls':0},indent=2))
    return 0


def inventory(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for name in (*SOURCES, *WRAPPERS)
            for p in ([root / name] if (root / name).is_file() else sorted((root / name).rglob('*')))
            if p.is_file() and '__pycache__' not in p.parts and p.suffix not in {'.pyc', '.pyo'}}


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=('prepare', 'preflight', 'run', 'resume', 'pause', 'reconcile', 'upgrade-accounting', 'repair-baseline'))
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--workspace', type=Path, default=Path.cwd())
    p.add_argument('--benchmarks', nargs='+', choices=('aime','tb21'), default=['aime','tb21'])
    p.add_argument('--methods', nargs='+', choices=('memory','ucb'), default=['memory'])
    p.add_argument('--rounds', type=int)
    p.add_argument('--budget-usd', type=float, help='Scheduling budget per benchmark including its shared fresh baseline; remaining budget is divided across policies, and in-flight calls settle at actual cost')
    p.add_argument('--env-file', type=Path)
    p.add_argument('--aime-config', type=Path)
    p.add_argument('--tb21-config', type=Path)
    p.add_argument('--ucb-calibration', type=Path)
    p.add_argument('--docker-host')
    p.add_argument('--host-tmpdir', type=Path, help='Short host directory for broker and RPC sockets; frozen during prepare')
    p.add_argument('--tb-agent-timeout-seconds',type=int,help='Explicit experimental solver deadline per TB task')
    p.add_argument('--aime-api-timeout-seconds',type=float,help='Per-request AIME API timeout for prepare (default 600); independent of task deadlines')
    p.add_argument('--tb-api-timeout-seconds',type=float,help='Per-request TB API timeout for prepare (default 1200); independent of task deadlines')
    p.add_argument('--provider-selection',type=Path,help='Measured primary/fallback endpoints and output limits, frozen during prepare')
    p.add_argument('--cell-allowance-usd', type=float, default=.25)
    p.add_argument('--call-allowance-usd', type=float, default=.5)
    p.add_argument('--turns', type=int, default=64)
    p.add_argument('--parallel', type=int, default=2)
    p.add_argument('--aime-task-concurrency', type=int, default=4)
    p.add_argument('--tb21-task-concurrency', type=int, default=2)
    p.add_argument('--baseline-repeats', type=int, help='Runs per A task: defaults to 1 for new studies; resume reuses the recorded census')
    p.add_argument('--_frozen', action='store_true', help=argparse.SUPPRESS)
    p.add_argument('--refresh-source', action='store_true', help='Refresh an unstarted preparation only')
    return p


def freeze_calibration(output, requested=None):
    """Validate and pin the authority inside an unstarted preparation."""
    from agentpilot_arena.runtime.semantic_calibration import load_formal_semantic_calibration_authority
    target = output / 'config' / 'semantic-calibration.json'
    candidate = Path(requested) if requested is not None else next(
        (p for p in (output / 'calibration' / 'accepted.bundle.json', target) if p.is_file()), None)
    if candidate is None:
        return None
    authority = load_formal_semantic_calibration_authority(candidate.resolve(), benchmark='research')
    target.parent.mkdir(parents=True, exist_ok=True)
    if authority.source != target.resolve():
        shutil.copyfile(authority.source, target)
    return {'path': str(target.resolve()), 'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
            'bundle_id': authority.report.bundle_id,
            'protocol': authority.bundle.selection.threshold_selection_protocol}


def prepare(args):
    root, output = args.workspace.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    source = output / 'source'
    if not args._frozen:
        started = [path for bench in ('aime','tb21') for path in (output / bench / 'baseline' / 'started').glob('*.json')]
        started += [output / bench / method / 'portfolio.sqlite3' for bench in ('aime','tb21') for method in ('memory','ucb')
                    if (output / bench / method / 'portfolio.sqlite3').exists()]
        if started:
            raise ValueError('An experiment that has dispatched work cannot replace its preparation; use resume or a new output directory')
        if source.exists() and args.refresh_source:
            shutil.rmtree(source)
        if not source.exists():
            for name in SOURCES:
                shutil.copytree(root / name, source / name, ignore=shutil.ignore_patterns('__pycache__','*.pyc','*.pyo'))
            for name in WRAPPERS:
                (source / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(root / name, source / name)
        argv = [sys.executable, str(source / WRAPPERS[0]), 'prepare', '--_frozen', '--output', str(output), '--workspace', str(root)]
        for name in ('aime_config','tb21_config','ucb_calibration','docker_host','host_tmpdir','tb_agent_timeout_seconds','provider_selection','aime_api_timeout_seconds','tb_api_timeout_seconds'):
            value = getattr(args, name)
            if value is not None:
                argv += ['--' + name.replace('_','-'), str(value)]
        return subprocess.run(argv, cwd=root, check=False).returncode
    sources = {
        'aime': args.aime_config,
        'tb21': args.tb21_config,
    }
    manifest = {'name': 'AgentPilot · AIME & Terminal-Bench · formal', 'benchmarks': ['aime','tb21'],
        'source': str(source), 'source_sha256': inventory(source), 'runs': [], 'blockers': [],
        'settings': {'proposer': 'qwen/qwen3.8-27b · xhigh', 'worker': 'z-ai/glm-5.3-flash · high',
                     'aime_solver': 'qwen/qwen3.5-9b · high', 'tb21_solver': 'z-ai/glm-5.3-flash · high'}}
    from rsi_arena.benchmarks.harbor_runtime.provider_broker import HOST_TMPDIR_ENV, provider_policy
    manifest.update(provider_policy())
    host_tmpdir = Path(args.host_tmpdir or os.environ.get(HOST_TMPDIR_ENV) or tempfile.gettempdir()).resolve()
    host_tmpdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest['host_tmpdir'] = str(host_tmpdir)
    routes=None
    if args.provider_selection:
        routes=json.loads(args.provider_selection.read_text())
        route_path=output/'config/provider-selection.json'
        write_record(route_path,routes)
        manifest['provider_selection']={'path':str(route_path),'sha256':hashlib.sha256(route_path.read_bytes()).hexdigest()}
        proposer_route=routes['models']['qwen/qwen3.8-27b']
        policy_route=routes['models']['z-ai/glm-5.3-flash']
        manifest['proposer_provider_only']=','.join([proposer_route['primary'],*proposer_route.get('fallbacks',[])])
        manifest['policy_provider_only']=','.join([policy_route.get('policy_primary',policy_route['primary']),*policy_route.get('policy_fallbacks',[])])
    if args.tb_agent_timeout_seconds is not None:
        manifest['settings']['tb_agent_timeout_seconds']=args.tb_agent_timeout_seconds
    calibration = freeze_calibration(output, args.ucb_calibration)
    if calibration is not None:
        manifest['semantic_calibration'] = calibration
    for bench in ('aime','tb21'):
        for method in ('memory','ucb'):
            config = output / 'config' / (bench + '-' + method + '.toml')
            entry = {'id': bench + '-' + method, 'benchmark': bench, 'method': method,
                     'output': bench + '/' + method, 'config': str(config), 'status': 'not_started', 'blockers': []}
            try:
                if sources[bench] is None:
                    raise ValueError(f'Pass --{bench}-config with a prepared benchmark runtime TOML')
                env = prepare_config(sources[bench], config, benchmark=bench, method=method, root=output,
                                     docker_host=args.docker_host, calibration=calibration['path'] if calibration else None,
                                     tb_agent_timeout_seconds=args.tb_agent_timeout_seconds,provider_selection=routes,
                                     aime_api_timeout_seconds=args.aime_api_timeout_seconds if args.aime_api_timeout_seconds is not None else 600.,
                                     tb_api_timeout_seconds=args.tb_api_timeout_seconds if args.tb_api_timeout_seconds is not None else 1200.)
                entry['a_tasks'] = len(env.task_ids)
                entry['b_tasks'] = env.runtime.sampling.size
                entry['b_repeats'] = env.runtime.sampling.replicates
                entry['config_sha256'] = hashlib.sha256(config.read_bytes()).hexdigest()
            except Exception as error:
                entry['status'] = 'blocked'
                entry['blockers'] = [str(error)]
            manifest['runs'].append(entry)
    write_record(output / 'experiment.json', manifest)
    print(json.dumps({'output': str(output), 'runs': [{k: r[k] for k in ('id','status','blockers')} for r in manifest['runs']]}, ensure_ascii=False, indent=2))
    return 0


def load_manifest(args):
    path = args.output / 'experiment.json'
    data = json.loads(path.read_text())
    route=data.get('provider_selection')
    if route and hashlib.sha256(Path(route['path']).read_bytes()).hexdigest()!=route['sha256']:
        raise ValueError('Frozen provider selection changed')
    source = Path(data['source'])
    if inventory(source) != data['source_sha256']:
        raise ValueError('Frozen implementation changed; do not resume this experiment with another implementation')
    calibration = data.get('semantic_calibration')
    if calibration:
        path = Path(calibration['path'])
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != calibration['sha256']:
            raise ValueError('Frozen semantic calibration changed; prepare a new experiment')
    elif any(r['method'] == 'ucb' and r.get('status') != 'blocked' for r in data['runs']):
        raise ValueError('UCB preparation has no frozen semantic calibration; prepare it before running')
    for entry in data['runs']:
        if entry.get('config_sha256') and hashlib.sha256(Path(entry['config']).read_bytes()).hexdigest() != entry['config_sha256']:
            raise ValueError(entry['id'] + ': prepared config changed; prepare a new experiment')
    return data


def verify_images(environment):
    physical = environment.settings.cell_runtime
    images = {}
    if environment.benchmark == 'aime':
        from agentpilot_arena.aime.apparatus import frozen_task_authority
        _, identity = frozen_task_authority(physical.tasks_root)
        images[identity] = identity
    else:
        authority = json.loads(physical.offline_verifier_authority_path.read_text())
        for task in (*environment.settings.split.a_task_ids, *environment.settings.split.b_task_ids):
            record = authority['tasks'][task]
            for key in ('closure_base_image_id','closure_image_id'):
                images[record[key]] = record[key]
            document = tomllib.loads((environment.catalog.tasks_root / task / 'task.toml').read_text())
            reference = document.get('environment', {}).get('docker_image')
            if reference:
                images[reference] = record['closure_base_image_id']
    references = sorted(images)
    result = subprocess.run([str(physical.docker_executable), '--host', physical.docker_host,
        'image','inspect','--format','{{.Id}}',*references], capture_output=True, text=True, timeout=60)
    if result.returncode or result.stdout.splitlines() != [images[reference] for reference in references]:
        raise ValueError(environment.benchmark + ': pinned environment/verifier images are absent or changed; restore its prepared Docker cache before running models')


def preflight(args, manifest):
    from rsi_arena.benchmarks.harbor_runtime.provider_broker import HOST_TMPDIR_ENV, check_host_transport
    host_tmpdir = manifest.get('host_tmpdir')
    if not host_tmpdir:
        raise ValueError('Preparation has no host temporary directory; prepare a new experiment with --host-tmpdir')
    if args.host_tmpdir is not None and str(args.host_tmpdir.resolve()) != host_tmpdir:
        raise ValueError('Host temporary directory differs from the frozen preparation')
    os.environ[HOST_TMPDIR_ENV] = host_tmpdir
    transport = check_host_transport()
    write_record(args.output / 'transport-preflight.json', transport)
    selected = [r for r in manifest['runs'] if r['benchmark'] in args.benchmarks and r['method'] in args.methods]
    if len(selected) != len(set(args.benchmarks)) * len(set(args.methods)):
        raise ValueError('Selected benchmark/policy configurations are missing')
    for run in selected:
        if run.get('blockers'):
            raise ValueError(run['id'] + ': ' + '; '.join(run['blockers']))
        env = ResearchEnvironment(load_app_config(run['config']), validate=True)
        subprocess.run([str(env.settings.cell_runtime.docker_executable), '--host', env.settings.cell_runtime.docker_host,
                        'info', '--format', '{{.ID}}'], check=True, stdout=subprocess.DEVNULL, timeout=15)
        verify_images(env)
        runtime_path = env.settings.cell_runtime.runs_root
        runtime_path.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(runtime_path).free < 2 * 1024**3:
            raise ValueError('Runtime filesystem has less than 2 GiB free')
        from agentpilot_arena.runtime.worker_settings import worker_reservation
        minimum = env.runtime.sampling.size * env.runtime.sampling.replicates * 2 * args.cell_allowance_usd + worker_reservation(env.runtime) + args.call_allowance_usd
        if args.budget_usd is not None and args.budget_usd / len(args.methods) <= minimum:
            raise ValueError(f'{run["id"]}: allocated budget cannot reserve a build/B chain ({minimum:.2f} USD) plus baseline and research')
    return selected


def run_benchmark(args, manifest, bench, selected):
    root = args.output / bench
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'driver.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan_path = root / 'launch-plan.json'
        plan = {'methods': sorted(args.methods), 'rounds': args.rounds, 'budget_usd': args.budget_usd,
                'host_tmpdir': manifest['host_tmpdir'],
                'baseline_repeats': args.baseline_repeats, 'parallel': args.parallel, 'turns': args.turns,
                'task_concurrency': getattr(args, bench + '_task_concurrency'),
                'cell_allowance_usd': args.cell_allowance_usd, 'call_allowance_usd': args.call_allowance_usd}
        if plan_path.exists():
            if args.command != 'resume':
                raise ValueError('This benchmark has already started; use resume with the same launch settings')
            if json.loads(plan_path.read_text()) != plan:
                raise ValueError('Resume keeps the saved methods, rounds, models and spending plan unchanged')
        write_record(plan_path, plan)
        # Both policies receive the exact same freshly collected A packet.
        baseline_config = next(r['config'] for r in manifest['runs'] if r['benchmark'] == bench and r['method'] == 'memory')
        try:
            packet, baseline_cost = collect_baseline(baseline_config, root / 'baseline', repeats=args.baseline_repeats,
                concurrency=plan['task_concurrency'], budget=args.budget_usd, cell_allowance=args.cell_allowance_usd,
                task_pool=root / 'task-pool')
        except BaselinePaused as error:
            return [{'run':run['id'],'returncode':2,'status':error.status,'reason':str(error)} for run in selected]
        except BaseException as error:
            path = root / 'baseline' / 'progress.json'
            progress = json.loads(path.read_text()) if path.exists() else {'completed': 0, 'total': None}
            write_record(path, {**progress, 'status': 'unresolved', 'error_type': type(error).__name__})
            raise
        from agentpilot_arena.baseline_accounting import accountant
        billing = accountant(baseline_config, root/'baseline', budget=args.budget_usd,
                             cell_allowance=args.cell_allowance_usd).refresh(query=True)
        allocation_path = root/'policy-allocation.json'
        if allocation_path.exists():
            allocation = json.loads(allocation_path.read_text())
            if allocation.get('budget_mode') != 'actual':
                from agentpilot_arena.admission_recovery import once
                once(root/'policy-allocation-before-nonblocking.json',allocation)
                allocation = {'baseline_known_cost':allocation['baseline_known_cost'], 'baseline_held_cost':0.,
                    'budget_mode':'actual', 'per_policy_budget':(args.budget_usd-allocation['baseline_known_cost'])/len(selected)}
                for run in selected:
                    database=args.output/run['output']/'portfolio.sqlite3'
                    if database.exists():
                        with sqlite3.connect(database) as db:
                            db.execute("UPDATE settings SET value_json=? WHERE key='budget_usd'",(json.dumps(allocation['per_policy_budget']),))
                write_record(allocation_path,allocation)
        else:
            allocation = {'baseline_known_cost':billing['known_cost_usd'], 'baseline_held_cost':0., 'budget_mode':'actual',
                'per_policy_budget':(args.budget_usd-billing['known_cost_usd'])/len(selected)}
            if allocation['per_policy_budget'] > 0:
                write_record(allocation_path,allocation)
        per_policy = allocation['per_policy_budget']
        if per_policy <= 0:
            return [{'run':run['id'],'returncode':2,'status':'paused','reason':'Baseline charges and holds exhausted policy budget'} for run in selected]
        def launch(run):
            if (args.output/'PAUSE').exists() or (args.output/run['output']/'PAUSE').exists():
                return {'run':run['id'],'returncode':2,'status':'paused','reason':'Operator paused before research dispatch'}
            output = args.output / run['output']
            output.mkdir(parents=True, exist_ok=True)
            argv = [sys.executable, str(Path(manifest['source']) / WRAPPERS[1]), '--config', run['config'],
                '--evidence', str(packet), '--output', str(output), '--rounds', str(args.rounds),
                '--budget-usd', str(per_policy), '--call-allowance-usd', str(args.call_allowance_usd),
                '--cell-allowance-usd', str(args.cell_allowance_usd), '--parallel', str(args.parallel),
                '--turns', str(args.turns), '--task-concurrency', str(plan['task_concurrency']),
                '--task-pool', str(root / 'task-pool'), '--execute']
            if manifest.get('proposer_provider_only'):
                argv += ['--provider-only',manifest['proposer_provider_only']]
            if manifest.get('policy_provider_only'):
                argv += ['--policy-provider-only',manifest['policy_provider_only']]
            database = output / 'portfolio.sqlite3'
            if args.command == 'resume' and database.exists():
                with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as connection:
                    started = connection.execute("SELECT 1 FROM checkpoints WHERE key='driver-session'").fetchone()
                if started:
                    argv.append('--resume')
            write_record(output / 'launch.json', {'argv': argv, 'shared_baseline_cost': baseline_cost, 'policy_budget_usd': per_policy})
            with (output / 'run.log').open('ab') as log:
                process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, cwd=args.workspace)
                write_record(output / 'process.json', {'pid': process.pid, 'started_at': time.time(), 'returncode': None})
                result = process.wait()
                write_record(output / 'process.json', {'pid': process.pid, 'returncode': result, 'finished_at': time.time()})
                return {'run': run['id'], 'returncode': result, 'log': str(output / 'run.log')}
        stop_billing=threading.Event()
        def poll_billing():
            account=accountant(baseline_config,root/'baseline',budget=args.budget_usd,cell_allowance=args.cell_allowance_usd)
            while not stop_billing.wait(30):
                try:
                    account.refresh(query=True)
                except Exception as error:
                    write_record(root/'baseline/billing-error.json',{'error_type':type(error).__name__,'at':time.time()})
        poller=threading.Thread(target=poll_billing,daemon=True)
        poller.start()
        try:
            with ThreadPoolExecutor(max_workers=len(selected)) as pool:
                return list(pool.map(launch, selected))
        finally:
            stop_billing.set()
            poller.join(timeout=1)


def main(argv=None):
    args = parser().parse_args(argv)
    if args.baseline_repeats is None:
        existing={json.loads(p.read_text())['baseline_repeats'] for bench in args.benchmarks
                  if (p := args.output/bench/'launch-plan.json').is_file()} if args.command=='resume' else set()
        if len(existing)>1:
            raise ValueError('Selected baselines have different historical repeat counts; resume them separately')
        args.baseline_repeats=next(iter(existing),1)
    args.output, args.workspace = args.output.resolve(), args.workspace.resolve()
    if args.command != 'prepare' and (args.aime_api_timeout_seconds is not None or args.tb_api_timeout_seconds is not None):
        raise ValueError('API timeouts are frozen during prepare; changing them requires a new measurement configuration')
    args.methods, args.benchmarks = sorted(set(args.methods)), sorted(set(args.benchmarks))
    for name in ('cell_allowance_usd','call_allowance_usd','budget_usd'):
        value = getattr(args, name)
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ValueError(name + ' must be positive and finite')
    if args.command == 'pause':
        if not (args.output/'experiment.json').is_file():
            raise ValueError('No prepared experiment at --output')
        if list(args.output.glob('*/*/portfolio.sqlite3')):
            raise ValueError('This pause command only drains baseline collection; research has already started')
        write_record(args.output/'PAUSE', {'requested_at':time.time(), 'reason':'operator'})
        print(json.dumps({'status':'pause_requested','running_tasks':'allowed to settle','output':str(args.output)}))
        return 0
    if args.command == 'prepare':
        return prepare(args)
    if args.command == 'upgrade-accounting':
        return upgrade_accounting(args)
    if args.command == 'repair-baseline':
        from agentpilot_arena.baseline_recovery import repair_baseline
        return repair_baseline(args)
    manifest = load_manifest(args)
    if args.command in ('run', 'resume', 'preflight'):
        validate_provider_policy(manifest)
    # Always execute the frozen implementation, including the launcher itself.
    current = Path(__file__).resolve()
    if not current.is_relative_to(Path(manifest['source'])):
        return subprocess.run([sys.executable, str(Path(manifest['source']) / WRAPPERS[0]),
                               *(sys.argv[1:] if argv is None else argv)], cwd=args.workspace, check=False).returncode
    if args.command == 'reconcile':
        return reconcile_baselines(args,manifest)
    for name in ('baseline_repeats','parallel','turns','aime_task_concurrency','tb21_task_concurrency'):
        if getattr(args, name) < 1:
            raise ValueError(name + ' must be positive')
    selected = preflight(args, manifest)
    if args.command == 'preflight':
        print(json.dumps({'status': 'passed', 'runs': [r['id'] for r in selected], 'paid_calls': 0}))
        return 0
    if args.rounds is None or args.rounds < 1 or args.budget_usd is None or args.budget_usd <= 0:
        raise ValueError('Formal execution requires explicit --rounds and --budget-usd (per benchmark)')
    env_file = args.env_file or args.workspace / '.env'
    _load_env_file(env_file)
    # Dotenv must not redirect a validated frozen host path after preflight.
    from rsi_arena.benchmarks.harbor_runtime.provider_broker import HOST_TMPDIR_ENV
    os.environ[HOST_TMPDIR_ENV] = manifest['host_tmpdir']
    if args.command == 'resume':
        (args.output/'PAUSE').unlink(missing_ok=True)
    def request_pause(signum, frame):
        write_record(args.output/'PAUSE', {'requested_at':time.time(), 'signal':signum})
    signal.signal(signal.SIGTERM, request_pause)
    signal.signal(signal.SIGINT, request_pause)
    results = []
    with ThreadPoolExecutor(max_workers=len(args.benchmarks)) as pool:
        futures = {pool.submit(run_benchmark, args, manifest, bench, [r for r in selected if r['benchmark'] == bench]): bench for bench in args.benchmarks}
        for future in as_completed(futures):
            try:
                results.extend(future.result())
            except Exception as error:
                bench=futures[future]
                results.extend({'run':r['id'],'returncode':2,'status':'paused',
                    'error_type':type(error).__name__,'reason':str(error)} for r in selected if r['benchmark']==bench)
    write_record(args.output / 'results.json', results)
    print(json.dumps(results, indent=2))
    return int(any(r['returncode'] != 0 for r in results))


if __name__ == '__main__':
    raise SystemExit(main())
