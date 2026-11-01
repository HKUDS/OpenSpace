"""Shared assistant history rules; raw receipts remain unchanged."""
from agentpilot.domain import canonical_json


def assistant_history_message(raw, model, *, turn=None, normalizations=None):
    message = {key: value for key, value in raw.items()
               if key in {'role', 'content', 'tool_calls', 'reasoning', 'reasoning_details'} and value is not None}
    message['role'] = 'assistant'
    if model.lower().removeprefix('openrouter/').startswith('qwen/qwen3.5-'):
        omitted = [key for key in ('reasoning', 'reasoning_details') if key in message]
        if omitted and normalizations is not None:
            normalizations.append({'kind': 'qwen_reasoning_history_omitted', 'turn': turn,
                'fields': omitted, 'bytes': len(canonical_json({k: message[k] for k in omitted}).encode())})
        for key in omitted:
            message.pop(key)
    details = message.get('reasoning_details')
    if (isinstance(details, list) and details
            and all(isinstance(item, dict) and item.get('type') == 'reasoning.text'
                    and isinstance(item.get('text'), str) for item in details)
            and ''.join(item['text'] for item in details) == message.get('reasoning')):
        message.pop('reasoning')
    return message
