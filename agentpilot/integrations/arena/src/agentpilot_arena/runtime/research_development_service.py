"""A development measurement requested by a worker, on immutable program bytes."""
from __future__ import annotations

import fcntl
import json
from pathlib import Path
import re

from agentpilot.domain import canonical_json, stable_id
from .git_workspace import GitWorkspace
from .research_worker import write_record


class ExperimentSnapshotter:
    """Copy a disposable experiment into a separately validated Git snapshot."""

    def __init__(self, runner):
        self.runner = runner

    def inputs(self, workspace: Path, base):
        work = GitWorkspace(workspace.name, workspace, base['commit_sha'])
        manager = self.runner.workspaces
        manager.base._assert_owned(work)
        # Do not silently measure a different program after an out-of-scope edit.
        raw = manager.validator._git(work, ['diff', '--name-only', 'HEAD', '-z'])
        raw += manager.validator._git(work, ['ls-files', '--others', '--exclude-standard', '-z'])
        for name in raw.decode().split('\0'):
            if name and not name.startswith('.agentpilot-experiment/') and not self.runner.action_factory.allowed(name):
                raise ValueError('Development measurement cannot include an out-of-scope source edit')
        patch, additions = self.runner._source_changes(work)
        return {'patch': patch, 'new_files': dict(additions)}

    def freeze(self, *, identity, base, changes, folder):
        saved_path = folder / 'snapshot.json'
        if saved_path.exists():
            artifact = json.loads(saved_path.read_text())
            self.runner.workspaces.base._git(['update-ref', 'refs/agentpilot/experiments/' + identity,
                                             artifact['commit_sha']], cwd=self.runner.runtime.program_repository)
            workspace_record = folder / 'snapshot-workspace.json'
            if workspace_record.exists():
                saved = json.loads(workspace_record.read_text())
                work = GitWorkspace(saved['worktree_id'], Path(saved['path']), saved['parent_commit_sha'])
                if work.path.exists():
                    self.runner.workspaces.remove(work)
            return artifact
        if not changes['patch'] and not changes['new_files']:
            artifact = {**base, 'id': identity, 'artifact_id': identity}
            write_record(saved_path, artifact)
            return artifact
        manager = self.runner.workspaces
        workspace_record = folder / 'snapshot-workspace.json'
        if workspace_record.exists():
            saved = json.loads(workspace_record.read_text())
            work = GitWorkspace(saved['worktree_id'], Path(saved['path']), saved['parent_commit_sha'])
            manager.base._assert_owned(work)
        else:
            work = manager.create(parent_commit_sha=base['commit_sha'], action_id=identity)
            write_record(workspace_record, {'worktree_id': work.worktree_id, 'path': str(work.path),
                                           'parent_commit_sha': work.parent_commit_sha})
        applied = folder / 'snapshot-applied.json'
        if not applied.exists():
            # A crash partway through applying an unbilled snapshot is safe to
            # discard; its authoritative inputs are the frozen request below.
            manager.validator._git(work, ['reset', '--hard', base['commit_sha']])
            manager.validator._git(work, ['clean', '-fd'])
            if changes['patch']:
                import subprocess
                process = subprocess.run(['git', 'apply', '--index', '--whitespace=nowarn', '-'],
                    cwd=work.path, input=changes['patch'].encode(), capture_output=True,
                    env=manager.base._environment(), timeout=30)
                if process.returncode:
                    raise ValueError('Experimental source patch cannot be applied to its base')
            for path, content in changes['new_files'].items():
                if not self.runner.action_factory.allowed(path):
                    raise ValueError('Experimental new source is outside editable scope')
                target = work.path / path
                if target.exists() or target.is_symlink():
                    raise ValueError('Experimental new source already exists')
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            write_record(applied, {'applied': True})
        staged = manager.stage(work)
        action = self.runner.action_factory.prototype_action(identity, base, work, staged)
        manager.validator.validate(action, work, staged)
        commit = manager.prepare_commit(work, message='Freeze worker development experiment')
        artifact = {'id': identity, 'artifact_id': identity, 'commit_sha': commit,
                    'config': dict(base.get('config') or {}), 'base_id': base['id'],
                    'metadata': {'scope_validated': True, 'experimental_snapshot': True}}
        write_record(saved_path, artifact)
        # Keep measured bytes reachable without publishing a candidate or moving HEAD.
        manager.validator._git(work, ['update-ref', 'refs/agentpilot/experiments/' + identity, commit])
        manager.remove(work)
        return artifact


class ExperimentDevelopmentService:
    def __init__(self, *, store, root, adapter, snapshotter, max_measurements=4):
        self.store, self.root, self.adapter = store, Path(root), adapter
        self.snapshotter, self.max_measurements = snapshotter, max_measurements
        self.root.mkdir(parents=True, exist_ok=True)

    def _directory(self, ticket_id):
        path = self.root / stable_id('worker-development', self.store.namespace, ticket_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def results(self, ticket_id):
        directory = self._directory(ticket_id)
        index = directory / 'index.json'
        if not index.exists():
            return []
        values = []
        for name, identity in json.loads(index.read_text()).items():
            receipt = directory / identity / 'result.json'
            values.append(json.loads(receipt.read_text()) if receipt.exists() else {
                'request_id': name, 'measurement_id': identity, 'status': 'unresolved'})
        return values

    def measure(self, *, ticket_id, base, workspace, request):
        if not isinstance(request, dict) or set(request) - {'request_id', 'tasks', 'replicates', 'artifact', 'control'}:
            raise ValueError('measure_a accepts request_id, tasks, replicates, artifact and control')
        name = request.get('request_id')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', name):
            raise ValueError('request_id must be a short stable experiment name')
        tasks = request.get('tasks')
        if tasks is None:
            tasks = list(self.adapter.task_ids)
        if (not isinstance(tasks, list) or not tasks or any(not isinstance(t, str) for t in tasks)
                or len(set(tasks)) != len(tasks) or not set(tasks) <= set(self.adapter.task_ids)):
            raise ValueError('Development tasks must be a nonempty selection from the supplied A directory')
        repeats = request.get('replicates', 1)
        if type(repeats) is not int or not 1 <= repeats <= self.adapter.max_replicates:
            raise ValueError('Development repetitions are unavailable')
        control = self.store.artifact(request['control']).to_dict() if request.get('control') else dict(base)
        control['artifact_id'] = control['id']
        artifact = self.store.artifact(request['artifact']).to_dict() if request.get('artifact') else None
        if artifact:
            artifact['artifact_id'] = artifact['id']
            if artifact['id'] == control['id']:
                raise ValueError('Choose a candidate different from the comparison artifact')
        changes = None if artifact else self.snapshotter.inputs(Path(workspace), base)
        identity = stable_id('development-experiment', self.store.namespace, ticket_id, name)
        inputs = {'base': base, 'changes': changes, 'artifact': artifact,
                  'control': control, 'tasks': tasks, 'replicates': repeats}
        directory = self._directory(ticket_id)
        with (directory / '.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            folder = directory / identity
            folder.mkdir(exist_ok=True)
            input_path, result_path = folder / 'input.json', folder / 'result.json'
            if input_path.exists() and json.loads(input_path.read_text()) != inputs:
                raise ValueError('Development request_id was already used for different inputs or source')
            if result_path.exists():
                result = json.loads(result_path.read_text())
                self.store.settle_external(identity, result['cost_usd'])
                return result
            index_path = directory / 'index.json'
            index = json.loads(index_path.read_text()) if index_path.exists() else {}
            if name not in index and len(index) >= self.max_measurements:
                raise ValueError('Additional development measurements are unavailable in this experiment')
            if not input_path.exists():
                write_record(input_path, inputs)
            candidate = artifact or self.snapshotter.freeze(identity=identity, base=base, changes=changes, folder=folder)
            reservation = self.adapter.estimate(candidate_count=1, task_count=len(tasks), replicates=repeats)
            self.store.reserve_external(identity, reservation, label='development experiment ' + ticket_id + '/' + name)
            index[name] = identity
            write_record(index_path, index)
            try:
                measured = dict(self.adapter.measure(ticket_id=identity, control=control,
                    candidates=[candidate], tasks=tasks, replicates=repeats, namespace=self.store.namespace))
                cost = measured.pop('cost_usd', None)
                if cost is None:
                    raise RuntimeError('Development result has no settled receipt')
                result = {'request_id': name, 'measurement_id': identity, 'candidate': candidate,
                          'control': control, 'result': measured, 'cost_usd': cost,
                          **({'source_changes': changes} if changes is not None else {})}
                write_record(result_path, result)
                self.store.settle_external(identity, cost)
                return result
            except BaseException as error:
                if not result_path.exists():
                    self.store.mark_external_unknown(identity, type(error).__name__)
                raise
