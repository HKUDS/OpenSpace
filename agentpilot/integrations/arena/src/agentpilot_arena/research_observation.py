"""Observe real research experiments without dispatching a build or B comparison."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

from agentpilot_arena.runtime.research_worker import write_record


_FORBIDDEN = frozenset({'build', 'admit'})
_SETTLED = frozenset({'succeeded', 'failed', 'accounting_unknown'})


def _kind(ticket):
    return ticket.get('kind') if isinstance(ticket, Mapping) else ticket.kind


def validate_experiment_only_store(store):
    """Check durable work before a coordinator can resume or recover it."""
    if any(_kind(ticket) in _FORBIDDEN for ticket in store.tickets()):
        raise ValueError('Experiments-only mode cannot resume build or B tickets')
    if any(row.get('kind') in _FORBIDDEN for row in store.followups()):
        raise ValueError('Experiments-only mode cannot resume build or B followups')


class ExperimentsOnlyBackend:
    """Keep proposal choices visible, with an independent dispatch backstop."""
    def __init__(self, backend, measurement_context=None):
        self.backend = backend
        self.measurement_context = deepcopy(measurement_context)

    def __getattr__(self, name):
        return getattr(self.backend, name)

    def capabilities(self):
        return self.backend.capabilities()

    @staticmethod
    def _check(kind):
        if kind in _FORBIDDEN:
            raise RuntimeError('Experiments-only mode cannot dispatch builds or B comparisons')

    def prepare(self, kind, arguments):
        self._check(kind)
        prepared = self.backend.prepare(kind, arguments)
        followup = prepared.get('followup')
        if isinstance(followup, Mapping):
            self._check(followup.get('kind'))
        if kind == 'experiment' and self.measurement_context is not None:
            arguments = dict(prepared['arguments'])
            arguments['research_evidence'] = {**arguments.get('research_evidence', {}),
                'measurement_context': deepcopy(self.measurement_context)}
            prepared = {**prepared, 'arguments': arguments}
        return prepared

    def _result(self, ticket, result):
        if _kind(ticket) == 'experiment' and self.measurement_context is not None and isinstance(result, Mapping):
            return {**result, 'measurement_context': deepcopy(self.measurement_context)}
        return result

    def execute(self, ticket):
        self._check(_kind(ticket))
        return self._result(ticket, self.backend.execute(ticket))

    def recover(self, ticket):
        self._check(_kind(ticket))
        return self._result(ticket, self.backend.recover(ticket))


class _ObservationBoundary(Exception):
    def __init__(self, status, decision):
        self.status, self.decision = status, decision


def observe_experiments(args, session, coordinator):
    """Run bounded research and a separate allowance after its first result.

    Observation limits preserve pending calls and do not close the conversation.
    A resume uses the original limits and counters, never a fresh allowance.
    """
    store = coordinator.store
    validate_experiment_only_store(store)
    limits = {
        'research_turns': args.turns if args.turns is not None else session.engine.budget.max_proposer_turns,
        'feedback_turns': getattr(args, 'feedback_turns', 16),
        'max_experiments': getattr(args, 'max_experiments', 2),
    }
    if any(type(value) is not int or value < 1 for value in limits.values()):
        raise ValueError('Experiment observation limits must be positive integers')
    key = 'experiment-observation/' + session.session_key
    state = store.load_checkpoint(key)
    if state is None:
        state = {'limits': limits, 'starting_turn': session.state['turn'],
                 'feedback_start_turn': None, 'phase': 'research',
                 'initial_results': [ticket.id for ticket in store.tickets()
                                     if ticket.kind == 'experiment' and ticket.status in _SETTLED],
                 'result_tickets': [], 'experiment_keys': [], 'observations': []}
    elif state['limits'] != limits:
        raise ValueError('Experiment observation limits changed; use a separate output directory')

    def save():
        store.checkpoint(key, state)
        write_record(args.output / 'proposer-observations.json', state['observations'])

    def experiments():
        return [ticket for ticket in store.tickets() if ticket.kind == 'experiment']

    original_collect = session._collect

    def collect(ready):
        if ready:
            original_collect(ready)
        new = [ticket for ticket in experiments()
               if ticket.status in _SETTLED and ticket.id not in state['initial_results']
               and ticket.id not in state['result_tickets']]
        if new:
            original_collect(new)
            state['result_tickets'].extend(ticket.id for ticket in new)
            if state['feedback_start_turn'] is None:
                state['feedback_start_turn'] = session.state['turn']
                state['phase'] = 'feedback'
            save()

    def active():
        return bool(coordinator.active_ticket_ids)

    def drain():
        while active():
            collect(coordinator.wait(timeout=30))
        collect(coordinator.poll())

    original = session._execute_tool

    def execute(name, arguments, *, identity):
        decision = next((row for row in state['observations'] if row['identity'] == identity), None)
        if decision is None:
            decision = {'tool': name, 'arguments': arguments, 'turn': session.state['turn'], 'identity': identity}
            state['observations'].append(decision)
            save()
        if name in {'build', 'evaluate'}:
            raise _ObservationBoundary('work_requested', decision)
        if name == 'experiment':
            known = set(state['experiment_keys']) | {ticket.idempotency_key for ticket in experiments()}
            if identity not in known and len(known) >= limits['max_experiments']:
                raise _ObservationBoundary('experiment_limit', decision)
        feedback = original(name, arguments, identity=identity)
        if name == 'experiment' and isinstance(feedback, Mapping) and feedback.get('ticket'):
            if identity not in state['experiment_keys']:
                state['experiment_keys'].append(identity)
            decision['ticket'] = feedback['ticket']
            save()
        return feedback

    save()
    input_path = args.output / 'proposer-input.json'
    if not input_path.exists():
        write_record(input_path, {'messages': session._messages(), 'tools': session.tools(),
            'model': session.engine.model, 'reasoning_effort': session.engine.reasoning_effort})
    session._execute_tool = execute
    # Session polling can observe a completion between the driver poll and
    # dispatch. Start the feedback allowance before that request sees it.
    session._collect = collect
    outcome = {'status': 'turn_limit'}
    try:
        coordinator.recover_running()
        coordinator.resume()
        while True:
            collect(coordinator.poll())
            if session.state.get('closed') or session.state.get('workflow_result'):
                outcome = {'status': 'closed'}
                break
            start = state['feedback_start_turn']
            spent = session.state['turn'] - (state['starting_turn'] if start is None else start)
            remaining = (limits['research_turns'] if start is None else limits['feedback_turns']) - spent
            # Pending tool calls are already-paid model output, and may include
            # an experiment from the very last available research call.
            pending = session.state.get('pending')
            if remaining <= 0 and pending is None:
                if start is None and active():
                    collect(coordinator.wait(timeout=30))
                    continue
                outcome = {'status': 'feedback_limit' if start is not None else 'turn_limit'}
                break
            lifecycle = session.lifecycle()
            if lifecycle['action'] == 'wait' and pending is None:
                if active():
                    collect(coordinator.wait(timeout=30))
                    continue
                outcome = {'status': 'yielded'}
                break
            if start is not None and remaining <= 2 and pending is None:
                session.begin_convergence()
            outcome = session.run(max_turns=1, converge_at_limit=False,
                                  allow_new_model_calls=remaining > 0)
            save()
            if outcome['status'] != 'yielded':
                break
    except _ObservationBoundary as stop:
        outcome = {'status': stop.status, 'decision': stop.decision,
                   'pending_tool_calls': (session.state.get('pending') or {}).get('calls', [])}
    finally:
        session._execute_tool = original
        session._collect = original_collect
        # A requested B operation stays pending; already launched experiments
        # finish and are retained even when this observation has ended.
        drain()
        save()
        write_record(args.output / 'proposer-conversation.json', session.state['messages'])
    validate_experiment_only_store(store)
    tickets = experiments()
    start = state['feedback_start_turn']
    return {**outcome, 'experiments_only': True,
            'model_calls': session.state['turn'] - state['starting_turn'],
            'feedback_model_calls': 0 if start is None else session.state['turn'] - start,
            'experiment_tickets': [ticket.id for ticket in tickets],
            'experiment_results': state['result_tickets'],
            'experiments_succeeded': sum(ticket.status == 'succeeded' for ticket in tickets),
            'experiments_failed': sum(ticket.status == 'failed' for ticket in tickets),
            'experiments_unresolved': sum(ticket.status not in {'succeeded', 'failed'} for ticket in tickets),
            'build_calls': 0, 'b_executions': 0}
