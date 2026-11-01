"""One durable worker build, independent of any benchmark measurement."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import hashlib
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
from typing import Mapping

from agentpilot.domain import PolicyAction, PolicyMethod, canonical_json, stable_id
from agentpilot.domain.policy import NewPath, ScopeKind, ScopeTarget, StructuredDirection
from agentpilot.persistence.sqlite import _dumps, _loads
from agentpilot.realization import CommandDiagnostics
from .action_connectors import (
    CandidateBoundaryConfig,
    ScopeSecretCandidateValidator, ValidatedGitWorkspaceManager,
)
from .git_workspace import GitWorkspace, GitWorkspaceManager
from .action_runner import HarnessResult
from .research_build_prompt import PortfolioBuildHarness
from .worker_factory import create_worker_harness, worker_accounting
from .worker_settings import worker_settings, worker_identity, worker_reservation
from .research_handoff import (
    apply_adopted_experiment,
    development_measurement_sources,
    freeze_handoff,
    install_worker_handoff,
    remove_worker_handoff,
    verify_handoff,
    verify_worker_handoff,
)


def write_record(path: Path, value) -> None:
    write_text_record(path, canonical_json(value) + '\n')


def write_text_record(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f'.{os.getpid()}.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def artifact_payload(value):
    if is_dataclass(value):
        return artifact_payload(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): artifact_payload(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [artifact_payload(item) for item in value]
    return json.loads(canonical_json(value))


class PortfolioActionFactory:
    """Adapt a free research instruction to the existing worker boundary.

    The compatibility fields are labelled as unverified research, rather than
    inventing a causal diagnosis to satisfy the legacy direction carrier.
    """

    def __init__(self, *, repository: Path, request, editable_roots, namespace: str,
                 protected_roots=()):
        self.repository = Path(repository).resolve()
        self.request = request
        self.roots = tuple(sorted(set(str(p).rstrip('/') for p in editable_roots)))
        self.protected = tuple(str(p).rstrip('/') for p in protected_roots)
        self.namespace = namespace

    def files(self, commit: str) -> tuple[str, ...]:
        raw = subprocess.check_output(
            ['git', '-C', str(self.repository), 'ls-tree', '-rz', '--name-only', commit],
            timeout=30,
        )
        return tuple(sorted(p for p in raw.decode('utf-8').split('\0') if p))

    def allowed(self, path: str) -> bool:
        p = PurePosixPath(path)
        return (not p.is_absolute() and p.as_posix() == path
                and not any(x in {'', '.', '..'} for x in p.parts)
                and not any(ord(c) < 32 for c in path)
                and '\\' not in path
                and any(path == r or path.startswith(r + '/') for r in self.roots)
                and not any(path == r or path.startswith(r + '/') for r in self.protected))

    def create(self, *, ticket_id: str, base: Mapping, instruction: str,
               files=(), new_files=(), research_evidence=None) -> PolicyAction:
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError('build needs a nonempty instruction')
        for name, values in (("files", files), ("new_files", new_files)):
            if not isinstance(values, (tuple, list)) or any(not isinstance(value, str) for value in values):
                raise ValueError(f'{name} must be an array of repository paths')
        commit = base['commit_sha']
        existing = set(self.files(commit))
        eligible = {p for p in existing if self.allowed(p)}
        requested = tuple(files or ())
        if any(not isinstance(p, str) or p not in eligible for p in requested):
            raise ValueError('files must name existing editable files at this base')
        additions = tuple(sorted(set(new_files or ())))
        if any(not isinstance(p, str) or not self.allowed(p) or p in existing for p in additions):
            raise ValueError('new_files must name new paths inside editable roots')
        selected = tuple(sorted(set(requested) if requested else eligible))
        if not selected and not additions:
            raise ValueError('this base has no editable files')
        scopes = (tuple(ScopeTarget(ScopeKind.FILE, p, ()) for p in selected)
                  if requested else tuple(ScopeTarget(ScopeKind.PACKAGE, p, ()) for p in self.roots))
        evidence = dict(research_evidence or {})
        evidence['base_source'] = {'ref': f'git:{commit}', 'commit_sha': commit,
                                   'editable_files': list(selected)}
        # This is trusted provenance, not an invented observation about a task.
        direction = StructuredDirection.create(
            observed_behavior='Unverified research instruction; inspect the attached source and execution evidence.',
            failure_mechanism='Research candidate; no causal diagnosis or task benefit has been certified.',
            implementation_direction=instruction.strip(),
            declared_scope=scopes,
            new_paths=tuple(NewPath(p, 'New file requested for this research implementation.') for p in additions),
            resolved_scope=tuple(sorted(set(selected) | set(additions))),
            evidence_refs=(f'git:{commit}',),
        )
        identity = stable_id('portfolio-worker', self.namespace, ticket_id)
        return PolicyAction(
            action_id=identity, method=PolicyMethod.MEMORY, run_id=identity,
            batch_id=stable_id('portfolio-build-batch', identity),
            decision_round_id=identity, state_id=self.request.state.state_id,
            parent_id=str(base.get('id') or commit), parent_commit_sha=commit,
            direction=direction, source_slot=None, arm_id=None, direction_match=None,
            created_at=datetime.now(timezone.utc),
            d2_trace={'implementation_evidence': evidence, 'research_ticket': ticket_id},
        )

    def prototype_action(self, ticket_id, base, workspace, changed_files):
        existing = set(self.files(base['commit_sha']))
        return self.create(
            ticket_id=ticket_id, base=base,
            instruction='Apply the supplied experimental patch; this is an unvalidated prototype.',
            files=tuple(p for p in changed_files if p in existing),
            new_files=tuple(p for p in changed_files if p not in existing),
        )


class PortfolioWorkerBuilder:
    """Checkpoint the paid worker separately from post-edit validation/commit.

    Completed artifacts replay without another worker invocation. A process
    interrupted before a worker receipt is saved stays unresolved; resuming it
    never silently spends the same action again.
    """

    def __init__(self, *, runtime, request, root: Path, environment=None,
                 harness=None, workspaces=None, validator=None, diagnostics=None, refusal_ledger=None):
        self.runtime = runtime
        self.root = Path(root).resolve()
        self.environment = dict(environment or {})
        self.action_factory = PortfolioActionFactory(
            repository=runtime.program_repository, request=request,
            editable_roots=runtime.candidate.editable_roots, namespace=str(self.root),
            protected_roots=runtime.candidate.protected_roots,
        )
        boundary = asdict(runtime.candidate)
        for navigation_field in ('entrypoint_paths', 'entrypoint_symbols', 'read_only_roots'):
            boundary.pop(navigation_field, None)
        self.validator = validator or ScopeSecretCandidateValidator(
            CandidateBoundaryConfig(**boundary), environment=self.environment,
        )
        self.workspaces = workspaces or ValidatedGitWorkspaceManager(
            GitWorkspaceManager(repository=runtime.program_repository, worktrees_root=runtime.worktrees_root),
            self.validator,
        )
        self.harness = harness if harness is not None else create_worker_harness(
            runtime, environment=self.environment, purpose="build", refusal_ledger=refusal_ledger,
        )
        configured = runtime.diagnostics
        self.diagnostics = diagnostics
        if self.diagnostics is None and configured is not None:
            self.diagnostics = CommandDiagnostics(
                configured.command, timeout_seconds=configured.timeout_seconds,
                max_output_bytes=configured.max_output_bytes,
                environment={k: self.environment[k] for k in configured.env_allowlist if k in self.environment},
            )

    def estimate(self, arguments=None) -> float:
        return worker_reservation(self.runtime)

    def build(self, *, ticket_id, base, instruction, files=(), new_files=(),
              research_evidence=None, experiment_handoff=None):
        identity = stable_id('portfolio-build-directory', ticket_id)
        folder = self.root / identity
        folder.mkdir(parents=True, exist_ok=True)
        # Independent tickets use independent locks; no budget/global lock is
        # held while a worker executes. Duplicate recovery cannot run it twice.
        with (folder / '.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._build(folder, ticket_id=ticket_id, base=base, instruction=instruction,
                               files=files, new_files=new_files, research_evidence=research_evidence,
                               experiment_handoff=experiment_handoff)

    def _build(self, folder, *, ticket_id, base, instruction, files, new_files,
               research_evidence, experiment_handoff):
        handoff_path = folder / 'experiment-handoff.json'
        handoff, handoff_descriptor = freeze_handoff(handoff_path, experiment_handoff)
        inputs = {'base': artifact_payload(base), 'instruction': instruction,
                  'files': list(files or ()), 'new_files': list(new_files or ()),
                  'research_evidence': dict(research_evidence or {})}
        if handoff is not None:
            inputs['experiment_handoff'] = handoff
        frozen_runtime = {
            'repository': str(Path(self.runtime.program_repository).resolve()),
            'worktrees_root': str(Path(self.runtime.worktrees_root).resolve()),
            'candidate': artifact_payload(self.runtime.candidate),
            **artifact_payload(worker_identity(self.runtime)),
            'diagnostics': artifact_payload(self.runtime.diagnostics),
        }
        fingerprint = hashlib.sha256(canonical_json({'input': inputs, 'runtime': frozen_runtime}).encode()).hexdigest()
        final_path = folder / 'result.json'
        original = folder / 'input.json'
        if original.exists():
            if json.loads(original.read_text())['sha256'] != fingerprint:
                raise ValueError('build ticket was reused for different input')
        else:
            write_record(original, {'sha256': fingerprint, 'input': inputs, 'runtime': frozen_runtime})
        verify_handoff(handoff_path, handoff)
        if final_path.exists():
            final = json.loads(final_path.read_text())
            if final.get('cost_usd') is not None or final.get('worker_accounting', {}).get('mode') == 'external':
                return final
        action_path = folder / 'action.json'
        receipt = folder / 'worker-result.json'
        started = folder / 'worker-started.json'
        cost = None if receipt.exists() or started.exists() else 0.0
        try:
            if action_path.exists():
                action = _loads(action_path.read_text())
            else:
                action_evidence = dict(inputs['research_evidence'])
                if handoff_descriptor is not None:
                    action_evidence['experiment_handoff_file'] = handoff_descriptor
                action = self.action_factory.create(
                    ticket_id=ticket_id,
                    base=inputs['base'],
                    instruction=inputs['instruction'],
                    files=inputs['files'],
                    new_files=inputs['new_files'],
                    research_evidence=action_evidence,
                )
                write_text_record(action_path, _dumps(action))
            # Reject incompatible/oversized legacy delivery before recording a
            # paid dispatch, even when a test or custom harness is injected.
            prompt_builder = getattr(self.harness, '_prompt', PortfolioBuildHarness._prompt)
            prompt = prompt_builder(action)
            if len(prompt) > worker_settings(self.runtime).max_prompt_bytes:
                raise ValueError('worker prompt exceeds its configured allowance')
            worker = None
            if receipt.exists():
                saved = json.loads(receipt.read_text())
                if saved.get('action_id') != action.action_id:
                    raise ValueError('worker receipt belongs to another action')
                worker = HarnessResult(diagnostics=saved['diagnostics'], cost_usd=saved['cost_usd'])
                cost = worker.cost_usd
            workspace_path = folder / 'workspace.json'
            if workspace_path.exists():
                saved = json.loads(workspace_path.read_text())
                workspace = GitWorkspace(saved['worktree_id'], Path(saved['path']), saved['parent_commit_sha'])
                self.workspaces.base._assert_owned(workspace)
                if (workspace.worktree_id != self.workspaces.base._workspace_name(action.action_id)
                        or workspace.path.is_symlink() or not workspace.path.is_dir()):
                    raise ValueError('saved workspace is missing or belongs to another action')
                if workspace.parent_commit_sha != base['commit_sha']:
                    raise ValueError('saved workspace belongs to another base')
            else:
                workspace = self.workspaces.create(parent_commit_sha=base['commit_sha'], action_id=action.action_id)
                write_record(workspace_path, artifact_payload(workspace))
            adoption_state = apply_adopted_experiment(
                workspace.path,
                action,
                handoff,
                plan_path=folder / 'experiment-adoption-plan.json',
                marker_path=folder / 'experiment-adoption-applied.json',
            )
            install_worker_handoff(workspace.path, handoff)
            if worker is None:
                if started.exists():
                    return {'status': 'failed', 'cost_usd': None,
                            'summary': 'Worker receipt is missing after dispatch; reconcile this build before retrying.'}
                write_record(started, {'action_id': action.action_id})
                worker = self.harness.run(action, workspace)
                if not isinstance(worker, HarnessResult):
                    raise TypeError('worker must return HarnessResult')
                write_record(receipt, {'action_id': action.action_id,
                                       'diagnostics': artifact_payload(worker.diagnostics),
                                       'cost_usd': worker.cost_usd})
            verify_handoff(handoff_path, handoff)
            verify_worker_handoff(workspace.path, handoff)
            remove_worker_handoff(workspace.path, handoff)
            cost = worker.cost_usd
            if worker.diagnostics.get('exit_code', 0) != 0:
                result = {'status': 'failed', 'cost_usd': cost,
                          'summary': 'Worker did not complete the requested implementation.'}
            else:
                staged = self.workspaces.stage(workspace)
                self.validator.validate(action, workspace, staged)
                final_tree = self.validator.validated_tree(workspace)
                checked = self.diagnostics.run(worktree=workspace.path) if self.diagnostics else None
                if (self.validator._git(workspace, ['diff', '--name-only', '-z'])
                        or self.validator._git(workspace, ['write-tree']).decode('ascii').strip()
                        != final_tree):
                    raise ValueError('diagnostics changed the candidate source after validation')
                if checked is not None:
                    write_record(folder / 'diagnostics.json', artifact_payload(checked))
                if checked is not None and not checked.passed:
                    result = {'status': 'failed', 'cost_usd': cost,
                              'summary': 'Implementation completed, but its configured smoke check failed.',
                              'diagnostics': artifact_payload(checked)}
                else:
                    commit_path = folder / 'prepared-commit.json'
                    if commit_path.exists():
                        commit = json.loads(commit_path.read_text())['commit_sha']
                        self.workspaces.prepare_commit(workspace, message='Build portfolio research candidate',
                                                       prepared_commit_sha=commit)
                    else:
                        commit = self.workspaces.prepare_commit(workspace, message='Build portfolio research candidate')
                        write_record(commit_path, {'commit_sha': commit, 'action_id': action.action_id})
                    self.workspaces.publish_commit(workspace, result_commit_sha=commit)
                    handoff_metadata = {}
                    if handoff_descriptor is not None:
                        handoff_metadata = {
                            'experiment_tickets': handoff_descriptor['experiment_tickets'],
                            'adopted_experiment': handoff_descriptor['adopted_ticket'],
                            'experiment_handoff_sha256': handoff_descriptor['sha256'],
                        }
                    if adoption_state is not None:
                        handoff_metadata.update({
                            'experiment_preapplied_tree_sha': adoption_state['preapplied_tree_sha'],
                            'experiment_preapplied_source_diff_sha256': adoption_state['preapplied_source_diff_sha256'],
                            'candidate_tree_sha': final_tree,
                            'worker_changed_after_experiment_adoption': (
                                final_tree != adoption_state['preapplied_tree_sha']
                            ),
                        })
                        measured_sources = development_measurement_sources(
                            self.action_factory.repository,
                            handoff,
                            final_tree,
                        )
                        if measured_sources is not None:
                            handoff_metadata['development_measurement_sources'] = measured_sources
                    result = {'cost_usd': cost, 'summary': 'Implementation saved; task effects have not been measured.',
                              'artifact': {'commit_sha': commit, 'config': dict(base.get('config') or {}),
                                           'base_id': base['id'], 'metadata': {
                                               'build_ticket': ticket_id, 'action_id': action.action_id,
                                               'changed_files': list(staged), 'instruction': instruction,
                                               'scope_validated': True, 'diagnostics_passed': checked.passed if checked else None,
                                               'adherence': 'not_assessed',
                                               **handoff_metadata,
                                           }}}
            result.update(worker_accounting(worker))
            write_record(final_path, result)
            return result
        except Exception as exc:
            if receipt.exists():
                saved_cost = json.loads(receipt.read_text()).get('cost_usd')
                cost = saved_cost if isinstance(saved_cost, (int, float)) and not isinstance(saved_cost, bool) else None
            elif started.exists():
                # A lower bound attached to an exception is not a complete bill.
                # Only an authoritative worker receipt can settle this dispatch.
                cost = None
            result = {'status': 'failed', 'cost_usd': cost,
                      'summary': f'Build stopped during execution or validation ({type(exc).__name__}).'}
            if receipt.exists():
                saved = json.loads(receipt.read_text())
                result.update(worker_accounting(HarnessResult(saved['diagnostics'], saved['cost_usd'])))
            write_record(final_path, result)
            return result

    def recover(self, *, ticket_id, **arguments):
        return self.build(ticket_id=ticket_id, **arguments)
