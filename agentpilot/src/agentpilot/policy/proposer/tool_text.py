"""Readable native tool results with losslessly delimited text bodies.

JSON remains the durable evidence format. Only model-facing tool messages use
this view, so source newlines and command escapes retain their actual meaning.
"""

from __future__ import annotations

import json

from agentpilot.domain import canonical_json


def render_tool_text(observation):
    bodies = {key: observation[key] for key in ("content", "stdout", "stderr")
              if isinstance(observation.get(key), str)}
    if not bodies:
        return canonical_json(observation)
    metadata = {key: value for key, value in observation.items() if key not in bodies}
    metadata["text_fields"] = [{"name": key, "characters": len(value)}
                               for key, value in bodies.items()]
    return canonical_json(metadata) + "".join(
        f"\n\n{key}:\n{value}" for key, value in bodies.items()
    )


def parse_tool_text(text):
    """Recover a saved observation for audits; never interpret body contents."""
    decoder = json.JSONDecoder()
    metadata, offset = decoder.raw_decode(text)
    fields = metadata.pop("text_fields", [])
    for field in fields:
        prefix = f"\n\n{field['name']}:\n"
        if not text.startswith(prefix, offset):
            raise ValueError("Invalid tool text boundary")
        offset += len(prefix)
        end = offset + field["characters"]
        if end > len(text):
            raise ValueError("Incomplete tool text body")
        metadata[field["name"]] = text[offset:end]
        offset = end
    if text[offset:].strip():
        raise ValueError("Unexpected tool text suffix")
    return metadata
