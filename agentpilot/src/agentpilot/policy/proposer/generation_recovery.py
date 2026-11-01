"""Bounded, explicitly unexecuted hints from a settled assistant response.

Call-like text in reasoning can be a quotation or an abandoned idea. It is
never dispatched here: the model must choose and issue a new native call.
"""
import json
import re

from agentpilot.domain import canonical_json
from agentpilot.providers.base import NATIVE_MESSAGE_CONTRACT

_BLOCK = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)
_FUNCTION = re.compile(r"<function=([A-Za-z_][A-Za-z_0-9]*)>\s*(.*?)\s*</function>", re.DOTALL)
_PARAMETER = re.compile(r"\s*<parameter=([A-Za-z_][A-Za-z_0-9]*)>(.*?)</parameter>", re.DOTALL)


def _valid(value, schema):
    kinds = schema.get("type", [])
    kinds = [kinds] if isinstance(kinds, str) else kinds
    kind = ("null" if value is None else "boolean" if type(value) is bool
            else "integer" if type(value) is int else "number" if type(value) is float
            else "string" if isinstance(value, str) else "array" if isinstance(value, list)
            else "object" if isinstance(value, dict) else "unknown")
    if kinds and kind not in kinds and not (kind == "integer" and "number" in kinds):
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if kind in {"integer", "number"}:
        return schema.get("minimum", value) <= value <= schema.get("maximum", value)
    if kind == "string":
        return schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", len(value))
    if kind == "array":
        return (schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", len(value))
                and all(_valid(item, schema.get("items", {})) for item in value))
    if kind == "object":
        properties = schema.get("properties", {})
        return (set(schema.get("required", [])) <= set(value)
                and (schema.get("additionalProperties", True) is not False or set(value) <= set(properties))
                and all(_valid(item, properties.get(key, {})) for key, item in value.items()))
    return kind != "unknown"


def _candidate(body, schemas):
    try:
        if body.startswith("{"):
            call = json.loads(body)
            if not isinstance(call, dict) or set(call) != {"name", "arguments"}:
                return None
            name, args = call["name"], call["arguments"]
        else:
            function = _FUNCTION.fullmatch(body.strip())
            if function is None:
                return None
            name, remaining = function.groups()
            if name not in schemas:
                return None
            args = {}
            while remaining.strip():
                parameter = _PARAMETER.match(remaining)
                if parameter is None or parameter[1] in args:
                    return None
                key, value = parameter[1], parameter[2].strip()
                if key not in schemas[name].get("properties", {}):
                    return None
                kinds = schemas[name]["properties"][key].get("type", [])
                kinds = [kinds] if isinstance(kinds, str) else kinds
                args[key] = value if "string" in kinds else json.loads(value)
                remaining = remaining[parameter.end():]
        if not isinstance(name, str) or name not in schemas or not isinstance(args, dict):
            return None
        if not _valid(args, schemas[name]):
            return None
        call = {"tool": name, "arguments": args}
        return call if len(canonical_json(call)) <= 1000 else None
    except (TypeError, ValueError):
        return None


def recovery_hint(settled_text, tools, *, allow_candidate=False):
    """Copy short notes and at most one unambiguous syntactic call candidate."""
    try:
        envelope = json.loads(settled_text or "")
        message = envelope["message"]
        if envelope.get("contract") != NATIVE_MESSAGE_CONTRACT or message.get("role") != "assistant":
            return {}
    except (TypeError, ValueError, KeyError, AttributeError):
        return {}
    details = message.get("reasoning_details")
    details = details if isinstance(details, list) else []
    texts = [message.get("reasoning"), *[
        row.get("text") for row in details
        if isinstance(row, dict) and row.get("type") == "reasoning.text"
    ], message.get("content")]
    texts = list(dict.fromkeys(text for text in texts if isinstance(text, str) and text.strip()))
    schemas = {tool["function"]["name"]: tool["function"]["parameters"] for tool in tools}
    candidates, notes = {}, []
    for text in texts:
        if allow_candidate:
            for match in _BLOCK.finditer(text):
                candidate = _candidate(match[1], schemas)
                if candidate is not None:
                    candidates[canonical_json(candidate)] = candidate
        # Preserve corrections without copying incomplete calls or code as prose.
        prose = _BLOCK.sub("", text).split("<tool_call>", 1)[0]
        prose = re.sub(r"```.*?(?:```|$)", "", prose, flags=re.DOTALL)
        for paragraph in re.split(r"\n\s*\n", prose):
            paragraph = paragraph.strip()
            if paragraph:
                if paragraph in notes:
                    notes.remove(paragraph)
                notes.append(paragraph)
    hint = {}
    if notes:
        hint["unfinished_note"] = "\n\n".join(notes[-3:])[-900:]
    if len(candidates) == 1:
        hint["candidate_call"] = next(iter(candidates.values()))
    return hint
