"""Present a later working version without relabelling historical executions."""
from __future__ import annotations

from agentpilot.domain import canonical_json


def working_context(session, artifact, history):
    if artifact is None:
        return
    session.working_artifact_id = artifact.id
    session.working_commit_sha = artifact.commit_sha
    session.reader.working_commit_sha = artifact.commit_sha
    if artifact.commit_sha != session.request.parent_selection.commit_sha:
        if session.artifact_navigator is None:
            raise ValueError('A later working version requires versioned source navigation')
        navigation = session.artifact_navigator('list_sources', {'artifact': artifact.id}, max_bytes=24576)
        if navigation.get('artifact') != artifact.id or navigation.get('commit_sha') != artifact.commit_sha:
            raise ValueError('Working source context belongs to another immutable version')
        source = (
            'Current program to investigate and improve:\n'
            + canonical_json({'artifact': artifact.id, 'commit_sha': artifact.commit_sha})
            + '\nSource directory and structure for this exact version:\n' + canonical_json(navigation)
            + '\nSource reads without an artifact use this working version. Read bodies to establish behavior. '
              'The initial execution directory below is historical evidence from an earlier baseline, '
              'not a measurement of this working version. New development experiments can measure it.')
        users = [i for i, item in enumerate(session.reader.messages) if item['role'] == 'user']
        if not users:
            raise ValueError('Research input has no program context')
        session.reader.messages[users[0]] = {'role': 'user', 'content': source}
        original_context = session.reader._evaluation_context_text()
        historical_context = session.reader._evaluation_context_text(
            historical_commit=session.request.parent_selection.commit_sha)
        for index in users[1:]:
            item = session.reader.messages[index]
            if item['content'].startswith(original_context):
                session.reader.messages[index] = {**item, 'content':
                    historical_context + item['content'][len(original_context):]}
    if history:
        session.reader.messages.append({'role': 'user', 'content':
            'Earlier research sessions: recorded proposals and aggregate outcomes. '
            'An accepted result does not prove its proposed explanation.\n' + canonical_json(history)})
