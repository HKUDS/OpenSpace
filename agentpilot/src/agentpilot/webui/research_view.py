"""Project asynchronous research into the existing inspector, without writes.

Only bound, complete B comparisons supply chart values. Local experiments and
unmeasured artifacts remain visible without acquiring a performance score.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tomllib

from .research import read_json, snapshot


def catalogue(root):
    root = Path(root).resolve()
    return {entry['id']: entry for entry in read_json(root/'experiment.json', {}).get('runs', [])
            if isinstance(entry.get('id'), str) and isinstance(entry.get('output'), str)
            and (root/entry['output']).resolve().is_relative_to(root)}


def run_config(entry):
    try:
        return tomllib.loads(Path(entry['config']).read_text())
    except (OSError, ValueError, KeyError):
        return {}


def repository(entry):
    config = run_config(entry)
    value = config.get('environment', {}).get('settings', {}).get('action_runtime', {}).get('program_repository')
    return Path(value).resolve() if value else None


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if isinstance(value, (int, float)) else value


def actor(config):
    return {key: config.get(key) for key in ('provider','model','reasoning_effort','provider_only')}


def unpack(value):
    """Display the persistence envelope as plain JSON; never instantiate it."""
    if isinstance(value, dict):
        if value.get('$') == 'mapping':
            return {str(unpack(k)): unpack(v) for k, v in value['items']}
        if value.get('$') == 'dataclass':
            return {k: unpack(v) for k, v in value['fields']}
        if value.get('$') in {'tuple', 'frozenset'}:
            return [unpack(v) for v in value['items']]
        if value.get('$') in {'enum', 'datetime'}:
            return value['value']
        return {k: unpack(v) for k, v in value.items()}
    return [unpack(v) for v in value] if isinstance(value, list) else value


def _records(path):
    empty = dict(tickets=[], artifacts=[], tracks=[], events=[], sessions={}, checkpoints={}, notes=[], followups=[])
    if not path.is_file():
        return empty
    db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=1)
    db.row_factory = sqlite3.Row
    try:
        db.execute('BEGIN')
        data = {**empty}
        for table in ('tickets', 'artifacts', 'tracks', 'notes', 'followups'):
            data[table] = [dict(r) for r in db.execute('SELECT * FROM '+table)]
        data['events'] = [dict(r, value=json.loads(r['value_json'])) for r in db.execute(
            "SELECT sequence,kind,value_json FROM policy_events WHERE kind IN ('candidate','dispatch','feedback') ORDER BY sequence")]
        data['sessions'] = {r[0].removeprefix('research-policy/session/'): json.loads(r[1]) for r in db.execute(
            "SELECT key,value_json FROM documents WHERE key LIKE 'research-policy/session/%'")}
        # Select only presentation facts; conversation bodies stay outside the view.
        data['checkpoints'] = {r['key']: dict(r) for r in db.execute(
            "SELECT key,updated_at,json_extract(value_json,'$.turn') AS turn,"
            "json_extract(value_json,'$.lifecycle_phase') AS phase FROM checkpoints WHERE key LIKE 'portfolio-session/%'")}
        return data
    finally:
        db.close()


def comparison(ticket):
    result = json.loads(ticket.get('result_json') or '{}')
    args = json.loads(ticket['arguments_json'])
    candidate, control = args.get('candidate', {}), args.get('control', {})
    proof = result.get('policy_evidence', {})
    bound = (result.get('evidence_tier') == 'adaptive_b'
             and bool(candidate.get('commit_sha')) and bool(control.get('commit_sha'))
             and proof.get('candidate_commit') == candidate.get('commit_sha')
             and proof.get('control_commit') == control.get('commit_sha'))
    valid = (bound and ticket['status'] == 'succeeded'
             and result.get('coverage', {}).get('complete') is True
             and proof.get('evaluation_validity') == 'valid')
    estimate = result.get('estimate') or {}
    return dict(ticket=ticket['id'], status=ticket['status'], valid=valid,
        candidate_id=candidate.get('artifact_id', candidate.get('id')),
        candidate_commit=candidate.get('commit_sha'), control_commit=control.get('commit_sha'),
        admission=result.get('admission_fate') if bound else None,
        accepted=result.get('accepted') is True and result.get('admission_fate') == 'admitted' and valid,
        value=estimate.get('challenger_absolute_value') if valid else None,
        delta=estimate.get('primary_delta_mean') if valid else None,
        lower=result.get('conservative_lower') if valid else None,
        coverage=result.get('coverage', {}) if bound else {},
        constraints_pass=result.get('constraints_pass') if bound else None,
        reason=result.get('reason') if bound else 'No complete, bound B comparison yet.',
        finished_at=iso(ticket.get('finished_at')))


def build_view(root, run_id):
    root = Path(root).resolve()
    entry = catalogue(root).get(run_id)
    if entry is None:
        return {'error': 'unknown run: '+run_id}
    board = snapshot(root)
    status = next(row for row in board['runs'] if row['id'] == run_id)
    output = (root/entry['output']).resolve()
    config = run_config(entry)
    settings = config.get('environment', {}).get('settings', {})
    runtime = settings.get('action_runtime', {})
    warnings = list(entry.get('blockers') or [])
    try:
        records = _records(output/'portfolio.sqlite3')
    except (sqlite3.Error, ValueError) as error:
        records = _records(output/'absent-read-only-database')
        warnings.append('Research records could not be read: '+type(error).__name__)
    tickets = sorted(records['tickets'], key=lambda t: (t['created_at'], t['id']))
    from agentpilot.research.context import research_lines, select_research_lines
    lines = research_lines(records['tracks'], records['notes'], tickets)
    artifacts = {r['id']: {**r, 'metadata': json.loads(r['metadata_json'])} for r in records['artifacts']}
    candidates = {e['value']['ticket_id']: e['value'] for e in records['events'] if e['kind'] == 'candidate'}
    dispatch = {e['value']['ticket_id']: e['value'] for e in records['events'] if e['kind'] == 'dispatch'}
    feedback = {e['value']['ticket_id']: e['value'] for e in records['events'] if e['kind'] == 'feedback'}
    comparisons = [comparison(t) for t in tickets if t['kind'] == 'admit']
    progress = {t['id']:t.get('progress') for t in status.get('tickets', []) if t.get('progress')}
    for item in comparisons:
        item['progress'] = progress.get(item['ticket'])
    accepted = {c['candidate_id'] for c in comparisons if c['accepted']}
    jobs, actions = [], []
    for ticket in tickets:
        args, result = json.loads(ticket['arguments_json']), json.loads(ticket.get('result_json') or '{}')
        job = {k: ticket.get(k) for k in ('id','kind','label','status','track_id','artifact_id','cost_usd','reservation_usd','error')}
        job['worker_accounting'] = result.get('worker_accounting')
        job.update(instruction=str(args.get('instruction') or args.get('question') or '')[:16000],
                   summary=(str(result.get('summary') or '')[:16000] if ticket['kind'] != 'admit'
                            or result.get('evidence_tier') == 'adaptive_b' else 'Comparison output is restricted.'),
                   created_at=iso(ticket['created_at']), finished_at=iso(ticket['finished_at']))
        job['progress'] = progress.get(ticket['id'])
        jobs.append(job)
        if ticket['kind'] != 'build' and not ticket['artifact_id']:
            continue
        candidate, decision, outcome = candidates.get(ticket['id'], {}), dispatch.get(ticket['id'], {}), feedback.get(ticket['id'], {})
        artifact = artifacts.get(ticket['artifact_id'], {})
        base = args.get('base') or {}
        parent_id = artifact.get('base_id') or base.get('artifact_id', base.get('id'))
        parent = artifacts.get(parent_id, {})
        evaluations = [c for c in comparisons if c['candidate_id'] == ticket['artifact_id']
                       and ticket['artifact_id'] and c['candidate_commit'] == artifact.get('commit_sha')]
        measured = [c for c in evaluations if c['valid']]
        latest = measured[-1] if measured else {}
        stage = {'queued':'planned', 'running':'running', 'failed':'blocked', 'accounting_unknown':'blocked'}.get(ticket['status'], 'built')
        if any(c['status'] in {'queued','running'} for c in evaluations):
            stage = 'evaluating'
        elif measured:
            stage = 'completed'
        instruction = job['instruction'] or artifact.get('metadata', {}).get('instruction') or ticket['label']
        changes = artifact.get('metadata', {}).get('changed_files', [])
        session = candidate.get('session') or 'Unassigned session'
        actions.append(dict(action_id=ticket['id'], batch_id=session, ordinal=len(actions), attempt_number=len(actions)+1,
            label=ticket['label'], parent_id=parent_id, parent_label=parent_id or 'Initial program',
            parent_commit_sha=parent.get('commit_sha') or base.get('commit_sha'), created_at=iso(ticket['created_at']),
            lifecycle_stage=stage, archive_seq=None, archive_label=None,
            direction=dict(direction_id=ticket['id'], failure_mechanism=instruction,
                implementation_direction=instruction, targets=[{'file':p,'symbols':[]} for p in args.get('files', [])], evidence_refs=[]),
            proposer=actor(config.get('policy', {}).get('proposer', {})), worker=actor(runtime.get('codex', {})),
            arm_id=candidate.get('arm_id'), slot_kind=decision.get('slot_kind'), match_kind=decision.get('match_kind'),
            match_distance=candidate.get('arm_distance'), effectiveness_label=outcome.get('public', {}).get('result'),
            effectiveness_reason=outcome.get('reason'), effectiveness_margin=None,
            decision_trace={},
            evaluation=dict(evidence_tier='adaptive_b', validity='valid' if measured else 'missing', attribution=None,
                signed_statistic=latest.get('lower'), primary_delta_mean=latest.get('delta'), primary_delta_lower=latest.get('lower'),
                constraints_pass=latest.get('constraints_pass'), admission_fate=latest.get('admission'),
                admitted=True if ticket['artifact_id'] in accepted else False if measured else None,
                child_commit_sha=artifact.get('commit_sha'), child_primary_value=latest.get('value'),
                cell_results={}, cell_notes={}, cell_diagnostics={}, completed_at=latest.get('finished_at')),
            execution=dict(worker_status=ticket['status'], adherence=artifact.get('metadata', {}).get('adherence'),
                cost_usd=ticket['cost_usd'], result_commit_sha=artifact.get('commit_sha'), touched_files=changes, file_changes=[],
                commit={'sha':artifact.get('commit_sha'),'touched_files':changes,'file_changes':[]},
                started_at=iso(ticket['started_at']), completed_at=iso(ticket['finished_at']),
                duration_seconds=(ticket['finished_at']-ticket['started_at']) if ticket['finished_at'] and ticket['started_at'] else None,
                diagnostics={}, diagnostic_ran=False, diagnostic_command=[], harness_attempts=[], rework_count=None),
            research=dict(job=job, comparisons=evaluations, selection={k:unpack(decision[k]) for k in
                ('ranking','eligible_tickets','cutoff','slot_kind','allocation','direction_selection','cooldown','semantic_repeat_fallback') if k in decision},
                parent_selection=unpack(records['sessions'].get(session, {}).get('parent_selection', {})),
                feedback={k:outcome.get(k) for k in ('reward','reason','completed_at')},
                semantic_repeat_of=candidate.get('semantic_repeat_of'))))
    batches = []
    for session in dict.fromkeys([*records['sessions'], *(a['batch_id'] for a in actions)]):
        members = [a for a in actions if a['batch_id'] == session]
        saved = records['sessions'].get(session, {})
        batches.append(dict(batch_id=session, label=session, status='recorded',
            created_at=members[0]['created_at'] if members else None,
            planned_action_ids=[a['action_id'] for a in members], expected_rounds=None,
            dispatched_action_ids=[a['action_id'] for a in members if a['action_id'] in dispatch],
            completed_action_ids=[a['action_id'] for a in members if a['evaluation']['completed_at']],
            cost_usd=sum(a['execution']['cost_usd'] or 0 for a in members),
            arm_ids=list(dict.fromkeys(a['arm_id'] for a in members if a['arm_id'])), memory_content=saved.get('memory','')))
    programs = []
    for artifact in sorted(artifacts.values(), key=lambda a: a['created_at']):
        action = next((a for a in actions if a['research']['job']['artifact_id'] == artifact['id']), None)
        if artifact['base_id'] and artifact['id'] not in accepted:
            continue
        programs.append(dict(program_id=artifact['id'], parent_id=artifact['id'], label=artifact['id'],
            archive_label=artifact['id'], archive_seq=0 if not artifact['base_id'] else len(programs),
            commit_sha=artifact['commit_sha'], primary_value=None if not action else action['evaluation']['child_primary_value'],
            cell_results={}, source_action_id=action['action_id'] if action else None,
            source_batch_id=action['batch_id'] if action else None, parent_program_id=artifact['base_id'], available_at=iso(artifact['created_at'])))
    baseline = next((b for b in board['baselines'] if b['benchmark'] == entry['benchmark']), {})
    if not programs and settings.get('root_seed', {}).get('commit_sha'):
        programs.append(dict(program_id='configured-root',parent_id='configured-root',label='Initial program',archive_seq=0,
            commit_sha=settings['root_seed']['commit_sha'],primary_value=None,cell_results={},source_action_id=None))
    warnings += [status.get('read_status')] if status.get('read_status') else []
    return dict(run_id=run_id, meta=dict(run_id=run_id,method=entry['method'],batch_count=len(batches),
        environment=entry['benchmark'],objective='Task success',metric_name='B success rate',metric_direction='maximize',
        proposer={**actor(config.get('policy',{}).get('proposer',{})),
            'provider_only':read_json(root/'experiment.json',{}).get('proposer_provider_only')},
        worker=(
            {**actor(runtime['worker'].get('provider', {})), 'backend': runtime['worker']['backend'],
             'mode': runtime['worker']['mode'],
             'provider': runtime['worker'].get('provider', {}).get('name')}
            if 'worker' in runtime else actor(runtime.get('codex',{}))),
        solver={'model':runtime.get('program',{}).get('model_name'),
                'provider_only':runtime.get('program',{}).get('provider_only'),
                'task_timeout_seconds':settings.get('cell_runtime',{}).get('timeout_policy',{}).get('agent_timeout_override_seconds')},
        visible_a_evidence=[],data_warnings=warnings),
        summary=dict(total_cost_usd=status['spent'],total_actions=len(actions),total_admitted=len(accepted),
                     externally_billed_workers=sum(j.get('worker_accounting', {}).get('mode') == 'external'
                                                   for j in jobs if j.get('worker_accounting'))),
        actions=actions,programs=programs,batches=batches,curve=[],decisions=[],planning_failures=[],ucb_diagnostics={},
        protocol=dict(tiers=[
            dict(tier='A',role='Research evidence',cell_count=baseline.get('total'),source='Shared fresh baseline',state=baseline.get('status')),
            dict(tier='B',role='Paired admission',cell_count=entry.get('b_tasks'),source='Configured sample per comparison',state='restricted'),
            dict(tier='C',role='Final evaluation',cell_count=None,source='Sealed',state='sealed')],
            estimator_design={'sample_size':entry.get('b_tasks'),'replicates':entry.get('b_repeats')},
            measured_actions=sum(bool(a['evaluation']['completed_at']) for a in actions),sealed_state='sealed'),
        research=dict(status=status['status'],reserved=status['reserved'],unknown=status['unknown'],budget_mode=status.get('budget_mode','reserved'),
            lines=select_research_lines(lines, limit=50, full=True),
            provider_blocker=status.get('provider_blocker'),
            proposer_turn=status['proposer_turn'],baseline=baseline,jobs=jobs,tracks=records['tracks'],
            sessions=list(records['checkpoints'].values()),
            memory=status.get('policy_memory',''),feedback_count=len(feedback),
            notes=[{k:r[k] for k in ('track_id','text','created_at')} for r in records['notes'][-20:]]))
