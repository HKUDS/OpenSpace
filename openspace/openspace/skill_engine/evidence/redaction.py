"""Deterministic redaction for evidence previews and metadata."""

from __future__ import annotations

import re
from typing import Any, Mapping

SECRET_REDACTION = "[REDACTED_SECRET]"

_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|token|authorization|cookie|secret|password|credential)",
    re.IGNORECASE,
)
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?i)\b([A-Z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*)"
        r"\s*=\s*([^\s\"']+)"
    ),
    re.compile(
        r"(?i)\b([A-Z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*)"
        r"\s*:\s*([^\s\"']+)"
    ),
    re.compile(r"(?i)\bAuthorization\s*:\s*Bearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\bCookie\s*:\s*[^;\n]+(?:;[^\n]+)?"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
)


def contains_secret(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _SECRET_KEY_RE.search(str(key)) and not _is_redacted_value(item):
                return True
            if contains_secret(item):
                return True
        return False
    if isinstance(value, (list, tuple, set)):
        return any(contains_secret(item) for item in value)
    if value is None:
        return False
    text = str(value).replace(SECRET_REDACTION, "")
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def _is_redacted_value(value: Any) -> bool:
    return isinstance(value, str) and value.strip() == SECRET_REDACTION


def redact_text(text: str) -> str:
    redacted = str(text)
    redacted = _SECRET_PATTERNS[0].sub(
        lambda match: f"{match.group(1)}={SECRET_REDACTION}",
        redacted,
    )
    redacted = _SECRET_PATTERNS[1].sub(
        lambda match: f"{match.group(1)}: {SECRET_REDACTION}",
        redacted,
    )
    for pattern in _SECRET_PATTERNS[2:]:
        redacted = pattern.sub(SECRET_REDACTION, redacted)
    return redacted


def redact_metadata(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _SECRET_KEY_RE.search(key_text):
                result[key_text] = SECRET_REDACTION
            else:
                result[key_text] = redact_metadata(item)
        return result
    if isinstance(value, list):
        return [redact_metadata(item) for item in value]
    if isinstance(value, tuple):
        return [redact_metadata(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
