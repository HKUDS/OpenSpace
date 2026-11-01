"""Deterministic handoff between research sessions, without a model callback.

Root artifacts and historical trials remain immutable. A completed session
can nominate the working version for a *new* session; it cannot change the
inputs or comparison bound to an existing session.
"""
from __future__ import annotations

from copy import deepcopy
import math


class ResearchEvolution:
    KEY = 'research-evolution/v1'

    def __init__(self, store, initial_artifact_id, policy=None):
        self.store, self.initial = store, initial_artifact_id
        self.policy = policy

    def _state(self):
        return self.store.load_checkpoint(self.KEY, default={
            'initial_artifact_id': self.initial, 'current_artifact_id': self.initial,
            'sessions': [], 'campaigns': [], 'current_campaign': None})

    def context(self, session_key, *, persist=True):
        state = self._state()
        if state['initial_artifact_id'] != self.initial:
            raise ValueError('Evolution belongs to another initial program')
        previous = next((row for row in state['sessions'] if row['session_key'] == session_key), None)
        if previous is not None:
            return deepcopy(previous)
        history = [row['result'] for row in state['sessions'] if row.get('result')][-8:]
        row = {'session_key': session_key, 'base_artifact_id': state['current_artifact_id'],
               'history': history, 'result': None}
        if self.policy is not None:
            strategy = self.policy.begin(session_key, persist=persist)
            row['base_artifact_id'] = strategy['base_artifact_id']
            row['policy_context'] = self.policy.context_text(strategy)
        self.store.artifact(row['base_artifact_id'])
        if persist:
            state['sessions'].append(row)
            self.store.checkpoint(self.KEY, state)
        return deepcopy(row)

    def campaign(self, entry_session, *, rounds=None, resume=False, new_session=False):
        state = self._state()
        active = state.get('current_campaign')
        name = entry_session if new_session or not resume or not active else active
        saved = next((row for row in state['campaigns'] if row['entry_session'] == name), None)
        if saved is not None:
            if rounds is not None and rounds != len(saved['sessions']):
                raise ValueError('Resume preserves the planned research sessions; choose a new session to start another study')
            return deepcopy(saved)
        count = rounds if rounds is not None else 1
        if type(count) is not int or count < 1:
            raise ValueError('rounds must be a positive integer')
        plan = {'entry_session': name,
                'sessions': [name] + [f'{name}-round-{i}' for i in range(2, count + 1)]}
        state['campaigns'].append(plan)
        state['current_campaign'] = name
        self.store.checkpoint(self.KEY, state)
        return deepcopy(plan)

    def planned_session(self, key, *, persist_context=True):
        """Validate an internal transition, including a crash before driver-session saved."""
        state = self._state()
        plan = next((row for row in state['campaigns']
                     if row['entry_session'] == state.get('current_campaign')), None)
        if plan is None or key not in plan['sessions']:
            raise ValueError('Session is not part of the persisted research plan')
        contexts = {row['session_key']: row for row in state['sessions']}
        for prior in plan['sessions'][:plan['sessions'].index(key)]:
            if not contexts.get(prior, {}).get('result'):
                raise ValueError('Earlier research session has not completed')
        if persist_context:
            self.context(key)
        ordinal = next((i for i, row in enumerate(self._state()['sessions'], 1) if row['session_key'] == key),
                       len(self._state()['sessions']) + 1)
        existing = self.store.load_checkpoint('portfolio-session/' + key, default=None)
        return {'key': key, 'ordinal': ordinal, 'new': existing is None}

    @staticmethod
    def _score(ticket):
        result = ticket.result or {}
        value = result.get('conservative_lower')
        if value is None:
            value = (result.get('estimate') or {}).get('primary_delta_mean')
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else -math.inf

    def finish(self, session_key, outcome, *, owned_ticket_ids=()):
        if self.policy is not None:
            self.policy.sync()
        context = self.context(session_key)
        state = self._state()
        index = next(i for i, row in enumerate(state['sessions']) if row['session_key'] == session_key)
        if state['sessions'][index].get('result') is not None:
            return deepcopy(state['sessions'][index]['result'])
        # Never advance a root while an execution or its recovery is unresolved.
        if outcome.get('status') in {'unresolved', 'pending', 'yielded', 'context_limit', 'budget_limit', 'provider_refused', 'provider_blocked', 'turn_limit', 'paused'}:
            return None
        owned = set(owned_ticket_ids)
        owned.update(row['build_ticket'] for row in outcome.get('build_chains', ()) if row.get('build_ticket'))
        admission_ids = {row['ticket'] for row in self.store.followups()
                         if row.get('kind') == 'admit' and row['build_ticket'] in owned and row.get('ticket')}
        candidates, accepted = [], []
        for ticket_id in sorted(admission_ids):
            ticket = self.store.get_ticket(ticket_id)
            if ticket.status in {'queued', 'running', 'accounting_unknown'}:
                return None
            args, result = ticket.arguments, ticket.result or {}
            candidate = args.get('candidate', {})
            control = args.get('control', {})
            candidate_id = candidate.get('artifact_id')
            if control.get('artifact_id') != context['base_artifact_id']:
                raise ValueError('Admission compared a different session baseline')
            row = {'artifact_id': candidate_id, 'base_artifact_id': control['artifact_id'],
                   'instruction': candidate.get('metadata', {}).get('instruction', ''),
                   'outcome': result.get('admission_fate', ticket.status),
                   'estimate': result.get('estimate'), 'reason': result.get('reason', '')}
            candidates.append(row)
            if (ticket.status == 'succeeded' and result.get('accepted') is True
                    and result.get('admission_fate') == 'admitted' and result.get('evidence_tier') == 'adaptive_b'):
                accepted.append(ticket)
        chosen = max(accepted, key=lambda t: (self._score(t), t.id)) if accepted else None
        selected = chosen.arguments['candidate']['artifact_id'] if chosen else context['base_artifact_id']
        checkpoint = self.store.load_checkpoint('portfolio-session/' + session_key, default={})
        experiments = []
        for identity in sorted(owned):
            ticket = self.store.get_ticket(identity)
            if ticket.kind == 'experiment':
                experiments.append({'ticket': identity, 'question': ticket.arguments.get('instruction', ''),
                                    'status': ticket.status,
                                    'open': {'tool': 'read_experiment', 'arguments': {'ticket': identity}}})
        result = {'session_key': session_key, 'base_artifact_id': context['base_artifact_id'],
                  'selected_artifact_id': selected, 'status': outcome.get('status'),
                  'candidates': candidates, 'summary': outcome.get('summary', ''),
                  'findings': [row.get('text', '') for row in checkpoint.get('notes', [])[-6:]],
                  'experiments': experiments,
                  'selection': 'accepted candidate with strongest recorded comparison' if chosen else 'retain current program'}
        if self.policy is not None:
            result['policy_method'] = self.policy.method
            result['selection_scope'] = 'This session delivery; the next research parent is selected independently by the strategy.'
        self.store.artifact(selected)
        state['sessions'][index]['result'] = result
        # Existing earlier sessions are replayed above; they cannot roll back a
        # later session's selected program after a restart.
        if index == len(state['sessions']) - 1:
            state['current_artifact_id'] = selected
        self.store.checkpoint(self.KEY, state)
        return deepcopy(result)

    def results(self):
        return [deepcopy(row['result']) for row in self._state()['sessions'] if row.get('result')]
