"""Read-only research-line projections shared by model inputs and the inspector.

Observations and proposer judgments stay separate. This projection never turns
local test success into a reward, and never opens private B measurements.
"""
import json


def research_lines(tracks, notes, tickets):
    latest = {}
    for note in sorted(notes, key=lambda n: n['id']):
        latest[note['track_id']] = note
    lines = []
    for track in tracks:
        work = [t for t in tickets if t.get('track_id') == track['id'] and t['kind'] in {'experiment', 'build'}]
        note = latest.get(track['id'])
        judgment = None
        if note:
            context = note.get('context') or json.loads(note.get('context_json') or '{}')
            judgment = {**context, 'text': note['text'], 'created_at': note['created_at']}
        experiments = []
        for ticket in work:
            if ticket['kind'] != 'experiment':
                continue
            args = ticket.get('arguments') or json.loads(ticket.get('arguments_json') or '{}')
            result = ticket.get('result') or json.loads(ticket.get('result_json') or '{}')
            experiments.append({'ticket': ticket['id'], 'question': args.get('instruction', ''),
                'status': ticket['status'], 'base_commit_sha': args.get('base', {}).get('commit_sha'),
                'reported_result': str(result.get('summary') or ''),
                'open': {'tool': 'read_experiment', 'arguments': {'ticket': ticket['id']}}})
        lines.append({'track': track['id'], 'label': track['label'], 'state': track['state'],
            'reason': track.get('reason'), 'latest_judgment': judgment, 'experiments': experiments,
            'open': {'tool': 'read_experiment', 'arguments': {'track': track['id']}},
            'updated_at': max([track['updated_at'], *[t.get('finished_at') or t.get('created_at', 0) for t in work]])})
    return lines


def select_research_lines(lines, *, preferred=(), current_commit=None, limit=4, full=False):
    """Bound model context; full source records remain addressable by track."""
    preferred = set(preferred)
    ordered = sorted(lines, key=lambda r: (r['track'] in preferred, r['state'] == 'active', r['updated_at']), reverse=True)
    result = []
    for row in ordered[:limit]:
        item = {k: row[k] for k in ('track', 'label', 'state', 'reason', 'open')}
        note = row.get('latest_judgment')
        if note:
            text = note['text']
            item['latest_judgment'] = {**note, 'text': text if full else text[:1200],
                'shortened': not full and len(text) > 1200,
                'version_relation': ('same_program' if note.get('source_commit') == current_commit else
                                     'historical_program' if note.get('source_commit') else 'unrecorded')}
        experiments = row['experiments'][-2:]
        item['experiments'] = [{**e, 'question': e['question'][:400],
            'reported_result': e['reported_result'][:500],
            'report_shortened': len(e['reported_result']) > 500} for e in experiments]
        item['omitted_experiments'] = len(row['experiments']) - len(experiments)
        result.append(item)
    return result
