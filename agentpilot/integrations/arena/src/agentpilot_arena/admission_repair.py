"""Version research control code and reconcile legacy drivers at settled boundaries."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import tomllib
from types import SimpleNamespace

from agentpilot.app.cli import _load_env_file
from agentpilot.research import ResearchStore
from agentpilot.research.lifecycle import inspect_session
from agentpilot_arena.admission_accounting import AdmissionAccounting
from agentpilot_arena.runtime.research_worker import write_record

FILES = tuple('agentpilot/integrations/arena/src/agentpilot_arena/'+name+'.py'
              for name in ('admission_accounting', 'admission_repair', 'baseline_accounting', 'research')) + (
    'agentpilot/src/agentpilot/research/coordinator.py',)
FLOW_FILES = FILES + (
    'agentpilot/src/agentpilot/research/context.py',
    'agentpilot/src/agentpilot/research/lifecycle.py',
    'agentpilot/src/agentpilot/research/policy.py',
    'agentpilot/src/agentpilot/app/provider_calls.py',
    'agentpilot/src/agentpilot/policy/proposer/portfolio_completions.py',
    'agentpilot/src/agentpilot/policy/proposer/guidance.py',
    'agentpilot/src/agentpilot/research/store.py',
    'agentpilot/src/agentpilot/research/evolution.py',
    'agentpilot/integrations/arena/src/agentpilot_arena/execution_journal.py',
    'agentpilot/integrations/arena/src/agentpilot_arena/execution_resume.py',
    'agentpilot/integrations/arena/src/agentpilot_arena/baseline_recovery.py',
    'agentpilot/integrations/arena/src/agentpilot_arena/pending_prices.py',
    'agentpilot/integrations/arena/src/agentpilot_arena/research_reuse.py',
    'agentpilot/integrations/arena/src/agentpilot_arena/research_transition.py',
    'agentpilot/integrations/arena/src/agentpilot_arena/admission_recovery.py',
    'agentpilot/integrations/arena/src/agentpilot_arena/formal.py',
    'agentpilot/integrations/arena/src/agentpilot_arena/task_scheduling.py',
    'agentpilot/integrations/arena/src/agentpilot_arena/research_setup.py',
    'agentpilot/src/agentpilot/policy/proposer/portfolio_session.py',
    'agentpilot/src/agentpilot/policy/proposer/native_research.py',
    'agentpilot/src/agentpilot/policy/proposer/research_io.py',
    'agentpilot/src/agentpilot/policy/proposer/skills/proposer-guide/SKILL.md',
    'agentpilot/integrations/arena/src/agentpilot_arena/runtime/research_experiment.py',
    'agentpilot/src/agentpilot/policy/proposer/skills/experiment-guide/SKILL.md',
    'agentpilot/src/agentpilot/webui/research_progress.py',
    'agentpilot/src/agentpilot/webui/research.py',
    'agentpilot/src/agentpilot/webui/research_view.py',
    'agentpilot/src/agentpilot/webui/static/research.js',
)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def install(root, workspace, *, research_flow=False):
    from agentpilot_arena.formal import inventory, load_manifest
    with (root/'admission-accounting-install.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = load_manifest(SimpleNamespace(output=root))
        files = FLOW_FILES if research_flow else FILES
        kind = 'research-flow' if research_flow else 'admission-accounting'
        key = 'research_flow_upgrade' if research_flow else 'admission_accounting_upgrade'
        scheduler = 'agentpilot/integrations/arena/src/agentpilot_arena/task_scheduling.py'
        research = 'agentpilot/integrations/arena/src/agentpilot_arena/research.py'
        if (not research_flow and 'agentpilot_arena.task_scheduling' in (workspace/research).read_text()
                and (not (Path(previous['source'])/scheduler).exists()
                     or sha(Path(previous['source'])/scheduler) != sha(workspace/scheduler))):
            raise ValueError('The scheduler also changed; use --research-flow to record its activation')
        recovery = 'agentpilot/integrations/arena/src/agentpilot_arena/admission_recovery.py'
        if (not research_flow and 'agentpilot_arena.admission_recovery' in (workspace/research).read_text()
                and (not (Path(previous['source'])/recovery).exists()
                     or sha(Path(previous['source'])/recovery) != sha(workspace/recovery))):
            raise ValueError('The measurement recovery protocol changed; use --research-flow to record its activation')
        changes = {name:sha(workspace/name) for name in files}
        if key in previous and all(previous['source_sha256'].get(k) == v for k,v in changes.items()):
            return previous
        identity = hashlib.sha256(json.dumps(changes, sort_keys=True).encode()).hexdigest()[:12]
        source = root/('source-'+kind+'-'+identity)
        if not source.exists():
            shutil.copytree(previous['source'], source, ignore=shutil.ignore_patterns('__pycache__','*.pyc','*.pyo'))
            for name in files:
                shutil.copyfile(workspace/name, source/name)
        expected = {**previous['source_sha256'], **changes}
        if inventory(source) != expected:
            raise ValueError('Accounting overlay changed unapproved source files')
        audit = root/(kind+'-upgrades')/identity
        audit.mkdir(parents=True, exist_ok=True)
        if not (audit/'previous-experiment.json').exists():
            write_record(audit/'previous-experiment.json', previous)
        code = '''import sys,json
from pathlib import Path
root=Path(sys.argv[1]);sys.path[:0]=[str(root/p) for p in ('agentpilot/src','agentpilot/integrations/arena/src','arena/src')]
from agentpilot.app import load_app_config
from agentpilot_arena.runtime.research_environment import ResearchEnvironment
manifest=json.loads(Path(sys.argv[2]).read_text())
for row in manifest['runs']:
    ResearchEnvironment(load_app_config(row['config']),validate=True)
print(json.dumps({'validated_configs':len(manifest['runs']),'measurement_apparatus':'unchanged','paid_calls':0}))
'''
        environment = {**os.environ, 'RSI_ARENA_HOST_TMPDIR':previous['host_tmpdir']}
        checked = subprocess.run([sys.executable, '-c', code, str(source), str(audit/'previous-experiment.json')],
            env=environment, capture_output=True, text=True, check=True)
        proof = json.loads(checked.stdout)
        updated = {**previous, 'source':str(source), 'source_sha256':expected,
                   key:{'id':identity, 'previous_source':previous['source'],
                       'changed_files':list(files), 'audit':str(audit), **proof}}
        if research_flow:
            updated[key].update(scheduler='fair-owner-v1', guidance='New conversations only; existing system messages are pinned')
            from agentpilot_arena.admission_recovery import POLICY
            updated[key]['infrastructure_recovery']=POLICY
            updated[key]['execution_recovery']={'protocol':'durable-execution-v1',
                'baseline_invalid_max_extra_attempts':1, 'unknown_dispatch':'requires durable receipt or explicit audited authorization',
                'valid_wrong_answers':'never repeated', 'io_retries':'filesystem operations only'}
        write_record(audit/'validation.json', proof)
        write_record(root/'experiment.json', updated)
        return updated


def resumable(store, output):
    """An accounting-blocked delivery may resume; completed decisions may not."""
    process_path = output/'process.json'
    if not process_path.exists():
        return None
    process = json.loads(process_path.read_text())
    if process.get('returncode') is None:
        return None
    current = store.load_checkpoint('driver-session', {})
    key = current.get('key')
    state = store.load_checkpoint('portfolio-session/'+str(key), {})
    report_path=output/'session-result.json'
    report=json.loads(report_path.read_text()) if report_path.exists() else {}
    if report.get('status') == 'provider_blocked':
        return None
    if state.get('workflow_result') is None and retryable_session_stop(report,state):
        available=store.board()['budget']['available_usd']
        if available is not None and available <= 0:
            return None
        retry=store.load_checkpoint('driver-retry/'+str(key),{})
        ready_at=max(process.get('finished_at',0)+60,retry.get('next_retry_at',0))
        return key if key is not None and time.time() >= ready_at else None
    if state.get('workflow_result') is not None or (state.get('last_delivery') or {}).get('status') != 'unresolved':
        return None
    if store.load_document('admission-accounting-restart/'+str(key)) is not None:
        return None
    snapshot = inspect_session(store, owned_ticket_ids=state.get('owned_ticket_ids', ()),
        build_ticket_ids=state.get('build_ticket_ids', ()),
        presented_ticket_statuses=state.get('presented_ticket_statuses', {}), active_ticket_ids=())
    chains = snapshot['build_chains']
    if snapshot['action'] != 'deliver' or not chains or any(c['status'] in {'pending','unresolved'} for c in chains):
        return None
    repaired = {t.id for t in store.tickets() if t.kind == 'admit'
                and (store.load_document('admission-accounting/'+t.id) is not None
                     or store.load_document('execution-resume/'+t.id) is not None)}
    owned = set(state.get('build_ticket_ids', ()))
    if not any(f['build_ticket'] in owned and f.get('ticket') in repaired for f in store.followups()):
        return None
    return key


def retryable_session_stop(report, state):
    from agentpilot.policy.proposer.portfolio_session import MAX_GENERATION_RECOVERIES
    if report.get('status') == 'provider_blocked':
        return False
    return (report.get('status') in {'provider_refused','budget_limit'} or
            (state.get('pending') is None and len(state.get('generation_failures',())) > MAX_GENERATION_RECOVERIES))


def policy_format_recovery(store, output, source):
    """Roll an older driver forward once for the repaired arm whitespace error."""
    process = json.loads((output/'process.json').read_text()) if (output/'process.json').exists() else {}
    if process.get('returncode') in (None, 0):
        return None
    launch_path = output/'accounting-resume.json'
    launch = json.loads(launch_path.read_text()) if launch_path.exists() else {}
    if launch.get('pid') != process.get('pid'):
        launch = json.loads((output/'launch.json').read_text()) if (output/'launch.json').exists() else {}
    argv = launch.get('argv', [])
    if len(argv) < 2 or Path(argv[1]).resolve().is_relative_to(Path(source).resolve()):
        return None
    key = store.load_checkpoint('driver-session', {}).get('key')
    if key is None or store.load_document('policy-format-restart/'+Path(source).name+'/'+key) is not None:
        return None
    log = output/'run.log'
    if not log.exists():
        return None
    with log.open('rb') as stream:
        stream.seek(max(0, log.stat().st_size - 8192))
        tail = stream.read().decode('utf-8', errors='replace')
    if '/agentpilot/research/policy.py' in tail and tail.rstrip().endswith('ValueError: failure_mechanism must be trimmed'):
        return key
    return None


class LiveAccountingRepair:
    def __init__(self, root, manifest, *, popen=subprocess.Popen):
        self.root, self.manifest, self.popen = root, manifest, popen
        self.children, self.accounts, self.recoveries = {}, {}, {}
        self.baseline_children = {}

    def legacy_driver_active(self, run):
        output = self.root/run['output']
        if not (output/'process.json').exists():
            return False
        try:
            process = json.loads((output/'process.json').read_text())
            if process.get('returncode') is not None:
                return False
            from agentpilot_arena.task_scheduling import process_identity
            if process_identity(process.get('pid')) is None:
                return False
            resumed = output/'accounting-resume.json'
            launch = json.loads(resumed.read_text()) if resumed.exists() else {}
            if launch.get('pid') != process.get('pid'):
                launch = json.loads((output/'launch.json').read_text())
            entrypoint=Path(launch['argv'][1]).resolve()
            if entrypoint.is_relative_to(Path(self.manifest['source'])):
                return False
            source=entrypoint.parents[2]
            research=source/'agentpilot/integrations/arena/src/agentpilot_arena/research.py'
            scheduler=source/'agentpilot/integrations/arena/src/agentpilot_arena/task_scheduling.py'
            # Compatible control upgrades do not need the legacy queue barrier.
            return not (scheduler.is_file() and research.is_file() and
                        'agentpilot_arena.task_scheduling' in research.read_text())
        except (OSError, ValueError, KeyError, IndexError):
            return True

    def _resume_baselines(self):
        path = self.root/'research-flow-transition.json'
        if not path.is_file():
            return
        record = json.loads(path.read_text())
        for bench, row in record.get('baselines', {}).items():
            if row['status'] != 'waiting_for_drain':
                continue
            pause = self.root/'PAUSE'
            if not pause.exists() or json.loads(pause.read_text()).get('transition_id') != record['id']:
                continue  # Never override a later operator pause or manual resume.
            if any((self.root/r['output']/'portfolio.sqlite3').exists() for r in self.manifest['runs'] if r['benchmark']==bench):
                row.update(status='needs_inspection', error='Research already started before baseline transition')
                continue
            from contextlib import ExitStack
            try:
                with ExitStack() as stack:
                    for name in ('driver.lock', 'baseline/collection.lock'):
                        lock = stack.enter_context((self.root/bench/name).open('a'))
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            plan = json.loads((self.root/bench/'launch-plan.json').read_text())
            argv = [sys.executable, str(Path(self.manifest['source'])/'agentpilot/experiments/run_formal_research.py'),
                    'resume', '--output', str(self.root), '--benchmarks', bench,
                    '--methods', *plan['methods'], '--env-file', record['env_file']]
            for flag in ('rounds','budget_usd','baseline_repeats','parallel','turns','cell_allowance_usd','call_allowance_usd'):
                argv += ['--'+flag.replace('_','-'), str(plan[flag])]
            argv += ['--'+bench.replace('_','-')+'-task-concurrency', str(plan['task_concurrency'])]
            with (self.root/bench/'flow-resume.log').open('ab') as log:
                process = self.popen(argv, cwd=self.root.parent.parent, stdout=log, stderr=subprocess.STDOUT)
            self.baseline_children[bench] = process
            row.update(status='resuming', pid=process.pid, argv=argv)
        for bench, process in list(self.baseline_children.items()):
            code = process.poll()
            if code is not None:
                record['baselines'][bench].update(status='finished' if code==0 else 'needs_inspection',returncode=code)
                del self.baseline_children[bench]
        write_record(path, record)

    def _resume_incomplete(self, run, store):
        """Automatically replay only work backed by durable execution receipts."""
        from agentpilot_arena.task_scheduling import process_identity
        from agentpilot_arena.execution_resume import journal_rows
        from agentpilot_arena.execution_journal import ReceiptExecutor
        output = self.root/run['output']
        process_path = output/'process.json'
        if not process_path.exists() or json.loads(process_path.read_text()).get('returncode') is None:
            return
        for ticket in store.tickets(status='accounting_unknown'):
            folder = output/'confirmation-private'/hashlib.sha256(ticket.id.encode()).hexdigest()[:32]
            if ticket.kind != 'admit' or (folder/'result.public.json').exists() or not (folder/'frozen-plan.private.json').exists():
                continue
            launch_path = folder/'resumption/launch.json'
            launch = json.loads(launch_path.read_text()) if launch_path.exists() else {}
            if (launch.get('process_identity') is not None and
                    process_identity(launch.get('pid')) == launch['process_identity']):
                continue
            status_path = folder/'resumption/status.json'
            status = json.loads(status_path.read_text()) if status_path.exists() else {}
            if (status.get('process_identity') is not None and status.get('status') == 'running' and
                    process_identity(status.get('pid')) == status['process_identity']):
                continue
            if launch.get('attempts',0) >= 3 or time.time()-launch.get('at',0) < 60:
                continue
            receipts = ReceiptExecutor(None,folder/'execution-journal')
            safe = True
            for stem, _, program, cell in journal_rows(folder,ticket):
                if (folder/'cells'/(stem+'.started.json')).exists() and not (folder/'cells'/(stem+'.result.json')).exists():
                    saved = receipts.folder(program,cell)
                    safe &= ((saved/'result.json').exists() or
                             ((saved/'request.json').exists() and not (saved/'dispatched.json').exists()))
            if not safe:
                continue
            (folder/'resumption').mkdir(parents=True,exist_ok=True)
            argv = [sys.executable,'-m','agentpilot_arena.execution_resume','--root',str(self.root),'--run',run['id']]
            with (folder/'resumption/worker.log').open('ab') as log:
                child = self.popen(argv,cwd=self.root.parent.parent,stdout=log,stderr=subprocess.STDOUT)
            write_record(launch_path,{'pid':child.pid,'process_identity':process_identity(child.pid),
                'argv':argv,'attempts':launch.get('attempts',0)+1,'at':time.time()})

    def step(self, *, query=True):
        self._resume_baselines()
        rows = []
        for run in self.manifest['runs']:
            try:
                output = self.root/run['output']
                if (output/'PAUSE').exists():
                    rows.append({'run': run['id'], 'status': 'paused', 'reason': 'operator'})
                    continue
                database = output/'portfolio.sqlite3'
                if not database.is_file():
                    continue
                if run['id'] not in self.accounts:
                    config = tomllib.loads(Path(run['config']).read_text())['environment']['settings']
                    store = ResearchStore(database)
                    self.accounts[run['id']] = AdmissionAccounting(store, output/'confirmation-private',
                        model=config['action_runtime']['program']['model_name'],
                        hold_usd=config['cell_runtime']['settled_cost_stop_usd'] or .25)
                account = self.accounts[run['id']]
                self._resume_incomplete(run, account.store)
                from agentpilot_arena.admission_recovery import RecoveryWatch
                if run['id'] not in self.recoveries:
                    self.recoveries[run['id']] = RecoveryWatch(self.root,self.manifest,run,account.store)
                recovery = self.recoveries[run['id']]
                recovery.tick()
                # Finish legacy B under its original scheduler. Waiting until the
                # driver exits guarantees a natural migration boundary, without
                # cancelling a solver or restarting a partly measured comparison.
                deferred = self.manifest.get('research_flow_upgrade') and self.legacy_driver_active(run)
                repaired = [] if deferred else account.settle_completed(transform=recovery.deliver)
                account.refresh(query=query)
                if query:
                    from agentpilot_arena.pending_prices import refresh
                    refresh(account.store, output)
                process = self.children.get(run['id'])
                if process is not None and process.poll() is not None:
                    write_record(output/'process.json', {'pid':process.pid, 'returncode':process.returncode,
                        'finished_at':time.time(), 'resumed_by':'admission-accounting-repair'})
                    del self.children[run['id']]
                format_repair = policy_format_recovery(account.store, output, self.manifest['source'])
                key = format_repair or resumable(account.store, output)
                if key is not None and self.manifest.get('research_flow_upgrade'):
                    if any(self.legacy_driver_active(other) for other in self.manifest['runs']
                           if other['benchmark']==run['benchmark']):
                        key = None  # One-time barrier between incompatible queue protocols.
                if key is not None:
                    with (output/'driver.lock').open('a') as lock:
                        try:
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            key = None
                    if key is not None:
                        launch = json.loads((output/'launch.json').read_text())
                        argv = list(launch['argv'])
                        argv[1] = str(Path(self.manifest['source'])/'agentpilot/experiments/run_research_portfolio.py')
                        argv[argv.index('--config')+1] = run['config']
                        if '--budget-usd' in argv:
                            argv[argv.index('--budget-usd')+1] = str(account.store.board()['budget']['limit_usd'])
                        if '--resume' not in argv:
                            argv.append('--resume')
                        # No active task is stopped. The resumed driver itself also
                        # takes driver.lock before dispatch, excluding a concurrent
                        # manual restart. At most one repair restart per session.
                        state=account.store.load_checkpoint('driver-retry/'+key,{})
                        attempt=state.get('attempts',0)+1
                        report_path=output/'session-result.json'
                        report=json.loads(report_path.read_text()) if report_path.exists() else {}
                        transient=retryable_session_stop(report,account.store.load_checkpoint('portfolio-session/'+key,{}))
                        restart_key=('driver-retry/'+key+'/'+str(attempt) if transient else 'admission-accounting-restart/'+key)
                        if format_repair:
                            restart_key = 'policy-format-restart/'+Path(self.manifest['source']).name+'/'+key
                        account.store.put_document(restart_key,
                            {'previous_launch':launch, 'argv':argv, 'session_key':key})
                        if transient:
                            account.store.checkpoint('driver-retry/'+key,{'attempts':attempt,
                                'next_retry_at':time.time()+min(300,60*2**min(attempt-1,3))})
                        with (output/'run.log').open('ab') as log:
                            process = self.popen(argv, cwd=self.root.parent.parent, stdout=log, stderr=subprocess.STDOUT)
                        self.children[run['id']] = process
                        write_record(output/'process.json', {'pid':process.pid, 'returncode':None,
                            'started_at':time.time(), 'resumed_by':'admission-accounting-repair'})
                        write_record(output/'accounting-resume.json', {'argv':argv, 'session_key':key, 'pid':process.pid})
                budget = account.store.board()['budget']
                report_path = output/'session-result.json'
                report = json.loads(report_path.read_text()) if report_path.exists() else {}
                with account.store._connect() as db:
                    count = db.execute("SELECT count(*) FROM external_costs WHERE key LIKE 'admission-invoice/%' AND state != 'settled'").fetchone()[0]
                rows.append({'run':run['id'], 'repaired_tickets':repaired, 'resumed_session':key,
                             **({'status': 'provider_blocked', 'blocker': report.get('blocker')}
                                if report.get('status') == 'provider_blocked' else {}),
                             'pending_invoices':count, 'spent_usd':budget['spent_usd'], 'held_usd':budget['reserved_usd']})
            except Exception as error:
                from agentpilot_arena.execution_journal import exception_record
                output = self.root/run['output']
                write_record(output/'recovery-error.private.json', exception_record(error))
                rows.append({'run':run['id'], 'status':'needs_inspection', 'error_type':type(error).__name__})
        write_record(self.root/'admission-accounting-status.json', {'status':'watching', 'pid':os.getpid(),
                     'updated_at':time.time(), 'runs':rows, 'active_restarted_drivers':list(self.children)})
        return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, default=Path.cwd())
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--install', action='store_true')
    parser.add_argument('--research-flow', action='store_true', help='Include fair scheduling, progress and guidance updates')
    parser.add_argument('--watch', action='store_true')
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.install:
        manifest = install(root, args.workspace.resolve(), research_flow=args.research_flow)
        print(json.dumps(manifest['research_flow_upgrade' if args.research_flow else 'admission_accounting_upgrade']))
        return 0
    from agentpilot_arena.formal import load_manifest
    manifest = load_manifest(SimpleNamespace(output=root))
    if not Path(__file__).resolve().is_relative_to(Path(manifest['source'])):
        raise ValueError('Run the live reconciler from the validated frozen overlay')
    if args.env_file:
        _load_env_file(args.env_file)
    os.environ['RSI_ARENA_HOST_TMPDIR'] = manifest['host_tmpdir']
    with (root/'admission-accounting-watch.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        repair = LiveAccountingRepair(root, manifest)
        while True:
            try:
                repair.step()
            except Exception as error:
                write_record(root/'admission-accounting-status.json', {'status':'error', 'pid':os.getpid(),
                    'updated_at':time.time(), 'error_type':type(error).__name__, 'error':str(error)})
                if not args.watch:
                    raise
            if not args.watch:
                return 0
            time.sleep(10)


if __name__ == '__main__':
    raise SystemExit(main())
