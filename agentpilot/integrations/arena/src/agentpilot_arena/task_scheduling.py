"""Fair, observable scheduling outside the benchmark measurement apparatus."""
from __future__ import annotations

from contextlib import contextmanager
import copy
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time


def process_identity(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return None if fields[0] == 'Z' else fields[19]
    except (OSError, IndexError):
        return None


class TaskPool:
    """Prefer waiting owners with fewer active slots, then least recently served.

    Each owner has a FIFO queue. Idle owners reserve no capacity. Slot flocks
    remain authoritative, so old processes cannot cause oversubscription during
    a recorded transition; fairness applies once all contenders use this queue.
    """
    def __init__(self, root, capacity, *, owner=None):
        if type(capacity) is not int or capacity < 1:
            raise ValueError('task concurrency must be positive')
        self.root, self.capacity = Path(root).resolve(), capacity
        self.owner = str(owner or f'process:{os.getpid()}')
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root/'authority.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = self.root/'capacity.json'
            if path.exists() and json.loads(path.read_text()) != {'capacity':capacity}:
                raise ValueError('Task pool already has a different concurrency; use its existing setting')
            if not path.exists():
                path.write_text(json.dumps({'capacity':capacity})+'\n')
            with self.connect() as db:
                db.executescript('''
                    CREATE TABLE IF NOT EXISTS requests (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL,
                        program_id TEXT, cell_id TEXT, measurement_seed TEXT, scope TEXT, pid INTEGER NOT NULL, process_identity TEXT,
                        state TEXT NOT NULL, queued_at REAL NOT NULL, acquired_at REAL,
                        finished_at REAL, slot INTEGER);
                    CREATE INDEX IF NOT EXISTS requests_state ON requests(state, owner, id);
                    CREATE TABLE IF NOT EXISTS owners (owner TEXT PRIMARY KEY, last_grant INTEGER NOT NULL);
                    CREATE TABLE IF NOT EXISTS protocol (version TEXT PRIMARY KEY);
                ''')
                versions = [r[0] for r in db.execute('SELECT version FROM protocol')]
                if versions and versions != ['fair-owner-v1']:
                    raise ValueError('Task pool uses another scheduling protocol')
                db.execute("INSERT OR IGNORE INTO protocol VALUES ('fair-owner-v1')")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.root/'queue.sqlite3', timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    @staticmethod
    def _reap(db):
        rows = list(db.execute("SELECT DISTINCT pid,process_identity FROM requests WHERE state IN ('queued','running')"))
        for row in rows:
            if process_identity(row['pid']) != row['process_identity']:
                db.execute("UPDATE requests SET state='abandoned',finished_at=? WHERE pid=? AND process_identity IS ? AND state IN ('queued','running')",
                           (time.time(), row['pid'], row['process_identity']))

    def _claim(self, identity):
        held = None
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                self._reap(db)
                chosen = db.execute('''
                    SELECT min(q.id) AS id FROM requests q
                    LEFT JOIN owners o ON o.owner=q.owner
                    WHERE q.state='queued' GROUP BY q.owner
                    ORDER BY (SELECT count(*) FROM requests r WHERE r.owner=q.owner AND r.state='running'),
                             coalesce(o.last_grant,0), min(q.id) LIMIT 1
                ''').fetchone()
                if chosen is not None and chosen['id'] == identity:
                    for slot in range(self.capacity):
                        candidate = (self.root/f'{slot}.lock').open('a')
                        try:
                            fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            candidate.close()
                            continue
                        held = candidate
                        db.execute("UPDATE requests SET state='running',acquired_at=?,slot=? WHERE id=?", (time.time(), slot, identity))
                        grant = db.execute('SELECT coalesce(max(last_grant),0)+1 FROM owners').fetchone()[0]
                        db.execute('INSERT INTO owners VALUES (?,?) ON CONFLICT(owner) DO UPDATE SET last_grant=excluded.last_grant', (self.owner, grant))
                        break
                db.commit()
            except BaseException:
                db.rollback()
                if held is not None:
                    held.close()
                raise
        return held

    @contextmanager
    def slot(self, *, program_id=None, cell_id=None, measurement_seed=None, scope=None):
        with self.connect() as db:
            identity = db.execute("INSERT INTO requests(owner,program_id,cell_id,measurement_seed,scope,pid,process_identity,state,queued_at) VALUES (?,?,?,?,?,?,?,'queued',?)",
                (self.owner, program_id, cell_id, str(measurement_seed) if measurement_seed is not None else None,
                 scope, os.getpid(), process_identity(os.getpid()), time.time())).lastrowid
        held = None
        try:
            while held is None:
                held = self._claim(identity)
                if held is None:
                    time.sleep(.05)
            yield
        finally:
            try:
                with self.connect() as db:
                    db.execute("UPDATE requests SET state=?,finished_at=? WHERE id=? AND state IN ('queued','running')",
                        ('released' if held is not None else 'cancelled', time.time(), identity))
            finally:
                if held is not None:
                    held.close()


class PooledExecutor:
    def __init__(self, executor, pool, *, scope=None):
        self.executor, self.pool, self.scope = executor, pool, scope

    def scoped(self, scope):
        return PooledExecutor(self.executor, self.pool, scope=scope)

    def measure(self, *, program_id, cell):
        with self.pool.slot(program_id=program_id, cell_id=cell.cell_id, measurement_seed=cell.measurement_seed, scope=self.scope):
            return self.executor.measure(program_id=program_id, cell=cell)


class ScheduledAdmission:
    """Give concurrent B comparisons separate telemetry, with the same evaluator."""
    def __init__(self, evaluator):
        self.evaluator = evaluator

    def __getattr__(self, name):
        return getattr(self.evaluator, name)

    def _scoped(self, ticket_id):
        evaluator = copy.copy(self.evaluator)
        evaluator.executor = evaluator.executor.scoped('admit/'+ticket_id)
        return evaluator

    def admit(self, *, ticket_id, **arguments):
        return self._scoped(ticket_id).admit(ticket_id=ticket_id, **arguments)

    def recover(self, ticket):
        identity = ticket['id'] if isinstance(ticket, dict) else ticket.id
        return self._scoped(identity).recover(ticket)
