"""Pause research at durable checkpoints and drain already executing cells.

No in-flight model request or worker is terminated. Separate maintenance slot
owners acquire the shared capacity as tasks finish, so queued B cells cannot
start before the operator's protocol transition.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sqlite3
import threading
import time

from agentpilot_arena.task_scheduling import TaskPool, process_identity
from agentpilot_arena.runtime.research_worker import write_record


def state(output):
    with sqlite3.connect(f'file:{output}/portfolio.sqlite3?mode=ro',uri=True,timeout=.2) as db:
        jobs=list(db.execute("SELECT id,kind,status FROM tickets WHERE status IN ('queued','running')"))
        calls=db.execute("SELECT count(*) FROM external_costs WHERE state='reserved' AND (label='proposer' OR label LIKE 'research-%')").fetchone()[0]
    return jobs,calls


def stop_idle(output, process, *, admissions_drained=False):
    pid=process['pid'];identity=process['identity']
    if process_identity(pid)!=identity:return True
    os.kill(pid,signal.SIGSTOP)
    stopped=False
    try:
        jobs,calls=state(output)
        safe=not calls and all(kind=='admit' and admissions_drained for _,kind,_ in jobs)
        if not safe:return False
        # The main process has no paid request/worker in flight. Its latest
        # durable model receipt and tool cursor are replayable on restart.
        os.kill(pid,signal.SIGTERM);os.kill(pid,signal.SIGCONT)
        for _ in range(30):
            if process_identity(pid)!=identity:
                stopped=True;return True
            time.sleep(.1)
        raise RuntimeError('Quiescent process did not exit after SIGTERM')
    finally:
        if not stopped and process_identity(pid)==identity:
            os.kill(pid,signal.SIGCONT)


def drain(root, *, benchmarks=None, status_name='single-repeat-transition.json'):
    manifest=json.loads((root/'experiment.json').read_text())
    selected = set(benchmarks or manifest['benchmarks'])
    if not selected <= set(manifest['benchmarks']):
        raise ValueError('Unknown benchmark to drain')
    manifest = {**manifest, 'benchmarks': sorted(selected),
                'runs': [r for r in manifest['runs'] if r['benchmark'] in selected]}
    status_path=root/status_name
    if status_path.exists():raise ValueError('An existing transition must be inspected before another drain')
    watch=root/'admission-accounting-status.json'
    if watch.exists():
        pid=json.loads(watch.read_text()).get('pid')
        if pid and process_identity(pid):
            cmd=Path(f'/proc/{pid}/cmdline').read_text()
            if 'agentpilot_arena.admission_repair' not in cmd or str(root) not in cmd:
                raise ValueError('Watcher identity changed')
            os.kill(pid,signal.SIGTERM)
    processes={}
    for run in manifest['runs']:
        p=root/run['output']/'process.json'
        if not p.exists():continue
        saved=json.loads(p.read_text());pid=saved.get('pid');identity=process_identity(pid)
        if saved.get('returncode') is None and identity:
            cmd=Path(f'/proc/{pid}/cmdline').read_text()
            if str(root/run['output']) not in cmd:raise ValueError('Research PID identity changed')
            processes[run['id']]={'pid':pid,'identity':identity,'output':run['output'],'benchmark':run['benchmark']}
    parked=set();gates={};release=threading.Event();guard=threading.Lock();errors=[]
    def hold(bench,slot):
        try:
            poolroot=root/bench/'task-pool';capacity=json.loads((poolroot/'capacity.json').read_text())['capacity']
            pool=TaskPool(poolroot,capacity,owner=f'{root}/protocol-maintenance/{bench}/{slot}')
            with pool.slot(scope='protocol-transition'):
                with guard:gates[bench]+=1
                release.wait()
        except BaseException as e:
            with guard:errors.append(type(e).__name__+': '+str(e))
    threads=[]
    try:
        while len(parked)<len(processes):
            for name,p in processes.items():
                if name in parked:continue
                try:
                    jobs,_=state(root/p['output'])
                    if not jobs and stop_idle(root/p['output'],p):parked.add(name)
                except sqlite3.OperationalError:
                    continue  # Resume/try at the next committed checkpoint.
            # Start draining a benchmark only after its non-admission work has
            # settled. This avoids blocking a live experiment's own A tasks.
            for bench in manifest['benchmarks']:
                remaining=[p for name,p in processes.items() if name not in parked and p['benchmark']==bench]
                if remaining and bench not in gates:
                    try:ready=all(all(k=='admit' for _,k,_ in state(root/p['output'])[0]) for p in remaining)
                    except sqlite3.OperationalError:ready=False
                    if ready:
                        gates[bench]=0
                        capacity=json.loads((root/bench/'task-pool/capacity.json').read_text())['capacity']
                        for slot in range(capacity):
                            t=threading.Thread(target=hold,args=(bench,slot),daemon=True);t.start();threads.append(t)
                capacity=json.loads((root/bench/'task-pool/capacity.json').read_text())['capacity']
                if gates.get(bench)==capacity:
                    for name,p in processes.items():
                        if name not in parked and p['benchmark']==bench:
                            try:
                                if stop_idle(root/p['output'],p,admissions_drained=True):parked.add(name)
                            except sqlite3.OperationalError:pass
            if errors:raise RuntimeError('; '.join(errors))
            write_record(status_path,{'phase':'draining','pid':os.getpid(),'processes':processes,
                'parked':sorted(parked),'held_slots':dict(gates),'updated_at':time.time()})
            if len(parked)<len(processes):time.sleep(1)
        write_record(status_path,{'phase':'ready','processes':processes,'parked':sorted(parked),'updated_at':time.time()})
    finally:
        release.set()
        for t in threads:t.join(timeout=1)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,required=True)
    p.add_argument('--benchmarks', nargs='+')
    p.add_argument('--status-name', default='single-repeat-transition.json')
    args=p.parse_args(argv);drain(args.root.resolve(), benchmarks=args.benchmarks, status_name=args.status_name);return 0


if __name__=='__main__':raise SystemExit(main())
