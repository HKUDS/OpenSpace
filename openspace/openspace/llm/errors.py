"""LLM-layer error types, classification, and retry utilities

    AuthenticationError (401)        — bad key / forbidden
    NotFoundError (404)              — model does not exist
    BadRequestError (400)            — catch-all (context overflow via
                                       OpenRouter, bad tool schema, etc.)
    ContextWindowExceededError (400)  — context overflow (OpenAI direct)
    RateLimitError (429)             — rate limit
    ServiceUnavailableError (529)    — server overload
    Timeout (408)                    — request timeout
    APIConnectionError               — network-level failure
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Any

__all__ = [
    # Exceptions
    "PromptTooLongError",
    "ModelNotAvailableError",
    "OverloadedError",
    "CannotRetryError",
    "FallbackTriggeredError",
    # Dataclass
    "RetryContext",
    # Predicates
    "is_overloaded_error",
    "is_prompt_too_long_error",
    "is_model_not_available_error",
    "is_abort_error",
    "is_media_size_error",
    "should_retry",
    # Token / overflow parsing
    "parse_prompt_too_long_token_counts",
    "get_prompt_too_long_token_gap",
    "parse_max_tokens_context_overflow_error",
    # Retry helpers
    "get_retry_delay",
    "get_retry_after_ms",
    "get_default_max_retries",
    # Classification (for logging / analytics)
    "classify_api_error",
    "categorize_retryable_api_error",
    # Error formatting for user-facing messages (consumed by 7.1)
    "format_api_error",
    "get_error_message_for_user",
    "sanitize_error_html",
    # Wrap helpers
    "to_prompt_too_long_error",
    "to_model_not_available_error",
    "to_overloaded_error",
    # Constants
    "API_ERROR_MESSAGE_PREFIX",
    "PROMPT_TOO_LONG_ERROR_MESSAGE",
    "REPEATED_OVERLOADED_ERROR_MESSAGE",
    "API_TIMEOUT_ERROR_MESSAGE",
    "DEFAULT_MAX_RETRIES",
    "BASE_DELAY_MS",
    "MAX_OVERLOADED_RETRIES",
    "FLOOR_OUTPUT_TOKENS",
]

API_ERROR_MESSAGE_PREFIX = "API Error"
PROMPT_TOO_LONG_ERROR_MESSAGE = "Prompt is too long"
REPEATED_OVERLOADED_ERROR_MESSAGE = "Repeated overloaded errors"
API_TIMEOUT_ERROR_MESSAGE = "Request timed out"

DEFAULT_MAX_RETRIES = 10
BASE_DELAY_MS = 500
MAX_OVERLOADED_RETRIES = 3
FLOOR_OUTPUT_TOKENS = 3000


def get_default_max_retries() -> int:
    """Return the default max retries. Override with ``OPENSPACE_MAX_RETRIES``."""
    import os as _os
    env_val = _os.environ.get("OPENSPACE_MAX_RETRIES")
    if env_val is not None:
        try:
            return int(env_val)
        except (ValueError, TypeError):
            pass
    return DEFAULT_MAX_RETRIES


@dataclass
class RetryContext:
    """Mutable context threaded through the retry loop.

    The agent loop reads ``max_tokens_override`` after a ``CannotRetryError``
    to decide whether to retry with a reduced output-token budget.
    """
    model: str
    max_tokens_override: int | None = None
    thinking_budget_tokens: int = 0


class PromptTooLongError(Exception):
    """Prompt exceeded the model context window.

    The agent lop catches this to trigger context compression instead of
    retrying the same payload.  ``error_details`` stores the raw API message
    so ``get_prompt_too_long_token_gap`` can parse token counts.
    """

    def __init__(
        self,
        message: str,
        *,
        model: str | None = None,
        provider: str | None = None,
        original_error: Exception | None = None,
        error_details: str | None = None,
    ) -> None:
        super().__init__(message)
        self.model = model
        self.provider = provider
        self.original_error = original_error
        self.error_details = error_details or message


class ModelNotAvailableError(Exception):
    """Model or credentials are unavailable — should NOT be retried.

    Covers: bad API key (401), forbidden (403), model not found (404),
    invalid model name (400).
    """

    def __init__(
        self,
        message: str,
        *,
        model: str | None = None,
        provider: str | None = None,
        original_error: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.model = model
        self.provider = provider
        self.original_error = original_error


class OverloadedError(Exception):
    """Server returned 529 / overloaded.

    LiteLLM surfaces this as ``ServiceUnavailableError`` or status 529.
    """

    def __init__(
        self,
        message: str,
        *,
        model: str | None = None,
        provider: str | None = None,
        original_error: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.model = model
        self.provider = provider
        self.original_error = original_error


class CannotRetryError(Exception):
    """All retries exhausted — wraps the last error with retry context.

    The agent loop inspects ``retry_context.max_tokens_override`` to decide
    whether an output-budget reduction is viable before surfacing the error.
    """

    def __init__(
        self,
        original_error: BaseException | None,
        retry_context: RetryContext,
    ) -> None:
        msg = str(original_error) if original_error else "Retries exhausted"
        super().__init__(msg)
        self.original_error = original_error
        self.retry_context = retry_context
        if isinstance(original_error, BaseException) and original_error.__traceback__:
            self.__traceback__ = original_error.__traceback__


class FallbackTriggeredError(Exception):
    """Model fallback triggered after repeated overloaded errors.

    The caller should re-issue the request with ``fallback_model``.
    """

    def __init__(self, original_model: str, fallback_model: str) -> None:
        super().__init__(
            f"Model fallback triggered: {original_model} -> {fallback_model}"
        )
        self.original_model = original_model
        self.fallback_model = fallback_model


_PTL_PATTERN = re.compile(
    r"prompt is too long[^0-9]*(\d+)\s*tokens?\s*>\s*(\d+)", re.IGNORECASE,
)


def parse_prompt_too_long_token_counts(
    raw_message: str,
) -> tuple[int | None, int | None]:
    """Parse actual/limit token counts from a prompt-too-long error.

    Handles:
    - Anthropic: ``"prompt is too long: 137500 tokens > 135000"``
    - OpenAI: ``"maximum context length is 128000 … resulted in 200008"``
    - Gemini/Vertex: ``"input token count (1100000) exceeds the maximum
      number of tokens allowed (1048576)"``

    Returns ``(actual_tokens, limit_tokens)`` — either may be ``None``.
    """
    m = _PTL_PATTERN.search(raw_message)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m2 = _CONTEXT_LENGTH_RE.search(raw_message)
    if m2:
        return (int(m2.group(2)), int(m2.group(1)))
    m3 = _GEMINI_PTL_RE.search(raw_message)
    if m3:
        return (int(m3.group(1)), int(m3.group(2)))
    return (None, None)


_CONTEXT_LENGTH_RE = re.compile(
    r"maximum context length is (\d+) tokens.*?(\d+) tokens", re.IGNORECASE,
)

# Gemini / Vertex AI: "input token count (1100000) exceeds the maximum
# number of tokens allowed (1048576)" — parenthesised numbers.
_GEMINI_PTL_RE = re.compile(
    r"input token count\s*\(?(\d+)\)?\s*exceeds.*?"
    r"tokens?\s*allowed\s*\(?(\d+)\)?",
    re.IGNORECASE | re.DOTALL,
)


def get_prompt_too_long_token_gap(error: PromptTooLongError) -> int | None:
    """How many tokens over the limit a prompt-too-long error reports.

    Context compression uses this gap to skip past multiple message groups
    instead of peeling one-at-a-time.

    Returns positive gap, or ``None`` if not parseable.
    """
    if not error.error_details:
        return None
    actual, limit = parse_prompt_too_long_token_counts(error.error_details)
    if actual is None or limit is None:
        return None
    gap = actual - limit
    return gap if gap > 0 else None


_MAX_TOKENS_OVERFLOW_RE = re.compile(
    r"(?:"
    r"input length and `?max_tokens`? exceed context limit:\s*(\d+)\s*\+\s*(\d+)\s*>\s*(\d+)"
    r"|"
    r"max_tokens is too large:\s*(\d+).*?supports at most (\d+)"
    r")"
)


def parse_max_tokens_context_overflow_error(
    error: Exception,
) -> dict[str, int] | None:
    """Parse token counts from a max_tokens overflow error.

    Handles two formats observed from real API responses:

    1. Anthropic-style: ``"input length and max_tokens exceed context limit: X + Y > Z"``
    2. OpenAI-style: ``"max_tokens is too large: 200000. This model supports at most 16384"``

    Returns ``{"input_tokens": …, "max_tokens": …, "context_limit": …}`` or
    ``None`` when the error is not a max-tokens overflow.
    """
    status = _get_error_status(error)
    if status is not None and status != 400:
        return None
    msg = str(error)
    m = _MAX_TOKENS_OVERFLOW_RE.search(msg)
    if not m:
        return None
    if m.group(1) is not None:
        return {
            "input_tokens": int(m.group(1)),
            "max_tokens": int(m.group(2)),
            "context_limit": int(m.group(3)),
        }
    requested = int(m.group(4))
    supported = int(m.group(5))
    return {
        "input_tokens": 0,
        "max_tokens": requested,
        "context_limit": supported,
    }


def _get_error_status(error: Exception) -> int | None:
    """Best-effort HTTP status extraction across LiteLLM/OpenAI/httpx errors."""
    for attr in ("status_code", "status"):
        val = getattr(error, attr, None)
        if isinstance(val, int):
            return val
    resp = getattr(error, "response", None)
    if resp is not None:
        sc = getattr(resp, "status_code", None)
        if isinstance(sc, int):
            return sc
    return None


def _get_error_provider(error: Exception) -> str | None:
    """Best-effort provider name extraction from a LiteLLM exception."""
    provider = getattr(error, "llm_provider", None)
    return provider if isinstance(provider, str) and provider else None


def is_overloaded_error(error: Exception) -> bool:
    """True if the error represents server overload (529 / ServiceUnavailable).

    Verified: LiteLLM raises ``ServiceUnavailableError`` or sets status 529.
    """
    try:
        import litellm.exceptions as le
        if isinstance(error, le.ServiceUnavailableError):
            return True
    except ImportError:
        pass
    status = _get_error_status(error)
    if status == 529:
        return True
    if '"type":"overloaded_error"' in str(error):
        return True
    return False


def is_prompt_too_long_error(error: Exception) -> bool:
    """True if the error indicates prompt exceeds the context window.

    Verified against real responses:
    - OpenAI direct: ``ContextWindowExceededError`` (400) with
      ``"maximum context length is X tokens … resulted in Y tokens"``
    - OpenRouter: ``BadRequestError`` (400) with
      ``"maximum context length is X tokens"`` nested in JSON
    - Anthropic (native): ``"prompt is too long: X tokens > Y"``
    - Gemini (google.genai): ``"input token count (X) exceeds the maximum
      number of tokens allowed (Y)"``
    - Vertex AI: ``"Request payload size exceeds the limit"`` (byte-level,
      treated as PTL so the compaction/error path can handle it)
    """
    try:
        import litellm.exceptions as le
        if isinstance(error, le.ContextWindowExceededError):
            return True
    except ImportError:
        pass
    msg_lower = str(error).lower()
    if "prompt is too long" in msg_lower:
        return True
    if "maximum context length" in msg_lower:
        return True
    if "context length exceeded" in msg_lower:
        return True
    if "input token count" in msg_lower and "exceeds" in msg_lower:
        return True
    if "exceeds the maximum number of tokens" in msg_lower:
        return True
    if "request payload size exceeds the limit" in msg_lower:
        return True
    return False


def is_model_not_available_error(error: Exception) -> bool:
    """True when the error indicates model or credentials are unavailable.

    Verified: LiteLLM raises ``AuthenticationError`` (401), ``NotFoundError``
    (404), or ``BadRequestError`` (400) with "not a valid model".
    """
    try:
        import litellm.exceptions as le
        if isinstance(error, le.AuthenticationError):
            return True
        if isinstance(error, le.NotFoundError):
            return True
    except ImportError:
        pass
    status = _get_error_status(error)
    if status in (401, 403):
        return True
    if status == 404:
        return True
    msg_lower = str(error).lower()
    if (
        "not a valid model" in msg_lower
        or "invalid model" in msg_lower
        or "does not exist" in msg_lower
    ):
        return True
    return False

def classify_api_error(error: Exception) -> str:
    """Classify an API error into a tag for log messages.

    Only includes categories that are reachable through LiteLLM.

    Returns one of: ``aborted``, ``api_timeout``, ``repeated_overloaded``,
    ``rate_limit``, ``server_overload``, ``prompt_too_long``,
    ``invalid_model``, ``auth_error``, ``connection_error``,
    ``server_error``, ``client_error``, ``unknown``.
    """
    import asyncio

    if isinstance(error, asyncio.CancelledError):
        return "aborted"

    # Timeout
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return "api_timeout"
    try:
        import litellm.exceptions as le
        if isinstance(error, le.Timeout):
            return "api_timeout"
    except ImportError:
        pass

    msg = str(error)

    if REPEATED_OVERLOADED_ERROR_MESSAGE in msg:
        return "repeated_overloaded"

    # Rate limit (429)
    try:
        import litellm.exceptions as le
        if isinstance(error, le.RateLimitError):
            return "rate_limit"
    except ImportError:
        pass
    status = _get_error_status(error)
    if status == 429:
        return "rate_limit"

    if is_overloaded_error(error):
        return "server_overload"

    if is_prompt_too_long_error(error):
        return "prompt_too_long"

    if is_model_not_available_error(error):
        if (
            status == 404
            or "does not exist" in msg.lower()
            or "not a valid model" in msg.lower()
            or "invalid model" in msg.lower()
        ):
            return "invalid_model"
        return "auth_error"

    # Connection errors
    try:
        import litellm.exceptions as le
        if isinstance(error, le.APIConnectionError):
            return "connection_error"
    except ImportError:
        pass
    msg_lower = msg.lower()
    if any(
        kw in msg_lower
        for kw in ("connection refused", "connection reset", "connectionerror",
                    "name resolution", "network unreachable")
    ):
        return "connection_error"

    if status is not None:
        if status >= 500:
            return "server_error"
        if status >= 400:
            return "client_error"

    return "unknown"

def categorize_retryable_api_error(error: Exception) -> str:
    """Backward-compatible retry category helper.

    Older callers and tests used this name for the small retry analytics
    surface. Keep it as a thin adapter over the canonical classifier.
    """
    tag = classify_api_error(error)
    if tag == "auth_error":
        return "authentication_failed"
    return tag

def should_retry(error: Exception) -> bool:
    """Decide whether *error* is worth retrying.

    Non-retryable errors (prompt too long, auth failure, model not found)
    are handled before this function is called — they raise specific
    exception types immediately.  This function handles the remaining
    transient / recoverable errors.
    """
    if is_overloaded_error(error):
        return True

    if parse_max_tokens_context_overflow_error(error) is not None:
        return True

    try:
        import litellm.exceptions as le
        if isinstance(error, le.APIConnectionError):
            return True
    except ImportError:
        pass

    status = _get_error_status(error)
    if status is None:
        msg_lower = str(error).lower()
        return any(
            kw in msg_lower
            for kw in ("connection refused", "connection reset", "connectionerror",
                        "cannot connect", "name resolution",
                        "temporary failure", "network unreachable")
        )

    if status == 408:  # Timeout
        return True
    if status == 409:  # Lock conflict
        return True
    if status == 429:  # Rate limit
        return True
    if status in (401, 403):  # LiteLLM may refresh credentials
        return True
    if status >= 500:  # Server errors
        return True

    return False


def get_retry_delay(
    attempt: int,
    retry_after_header: str | None = None,
    max_delay_ms: int = 32_000,
) -> int:
    """Compute retry delay in ms with exponential backoff + jitter.

    ``base = min(BASE_DELAY_MS * 2^(attempt-1), max_delay_ms)``
    ``jitter = random(0, 0.25 * base)``

    If *retry_after_header* is present and parseable, it is used verbatim.
    """
    if retry_after_header:
        try:
            seconds = int(retry_after_header)
            if seconds > 0:
                return seconds * 1000
        except (ValueError, TypeError):
            pass

    base_delay = min(BASE_DELAY_MS * (2 ** (attempt - 1)), max_delay_ms)
    jitter = random.random() * 0.25 * base_delay
    return int(base_delay + jitter)


def get_retry_after_ms(error: Exception) -> int | None:
    """Extract retry-after delay in ms from error headers.

    LiteLLM exceptions may expose headers via ``.response`` or ``.headers``.
    """
    headers = getattr(error, "headers", None)
    if headers is not None:
        val = _extract_retry_after_value(headers)
        if val is not None:
            return val

    resp = getattr(error, "response", None)
    if resp is not None:
        resp_headers = getattr(resp, "headers", None)
        if resp_headers is not None:
            val = _extract_retry_after_value(resp_headers)
            if val is not None:
                return val

    return None


def _extract_retry_after_value(headers: Any) -> int | None:
    """Parse retry-after seconds → ms from a header-like object."""
    raw = None
    if hasattr(headers, "get"):
        raw = headers.get("retry-after") or headers.get("Retry-After")
    elif isinstance(headers, dict):
        raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is not None:
        try:
            seconds = int(raw)
            if seconds > 0:
                return seconds * 1000
        except (ValueError, TypeError):
            pass
    return None


def to_prompt_too_long_error(
    error: Exception,
    *,
    model: str | None = None,
) -> PromptTooLongError:
    """Wrap a raw API error as ``PromptTooLongError``."""
    return PromptTooLongError(
        str(error),
        model=model,
        provider=_get_error_provider(error),
        original_error=error,
        error_details=str(error),
    )


def to_model_not_available_error(
    error: Exception,
    *,
    model: str | None = None,
) -> ModelNotAvailableError:
    """Wrap a raw API error as ``ModelNotAvailableError``."""
    return ModelNotAvailableError(
        str(error),
        model=model,
        provider=_get_error_provider(error),
        original_error=error,
    )


def to_overloaded_error(
    error: Exception,
    *,
    model: str | None = None,
) -> OverloadedError:
    """Wrap a raw API error as ``OverloadedError``."""
    return OverloadedError(
        str(error),
        model=model,
        provider=_get_error_provider(error),
        original_error=error,
    )


# ═══════════════════════════════════════════════════════════════════════
# Functions consumed by step 7.1 — Agent Loop error handling
# Implementation: utils/errors.ts isAbortError, services/api/errors.ts
#     getAssistantMessageFromError + classifyAPIError + formatAPIError
# ═══════════════════════════════════════════════════════════════════════

def is_abort_error(error: Exception) -> bool:
    """True if the error represents an intentional abort / cancellation.

    Implementation: ``utils/errors.ts`` ``isAbortError`` (L27-33).
    Checks for ``asyncio.CancelledError`` and common abort patterns.
    """
    import asyncio

    if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt)):
        return True
    name = getattr(error, "name", "") or type(error).__name__
    if name == "AbortError":
        return True
    msg = str(error).lower()
    if "request was aborted" in msg or "aborted" == msg.strip():
        return True
    return False


def is_media_size_error(error: Exception) -> bool:
    """True if the error is about an image/PDF exceeding size limits.

    Implementation: ``ImageSizeError``, ``ImageResizeError``, PDF page limit, etc.
    LiteLLM/OpenRouter surface these as 400 BadRequestError with specific
    message patterns.
    """
    msg = str(error).lower()
    if "image exceeds" in msg and "maximum" in msg:
        return True
    if "maximum of" in msg and "pdf pages" in msg:
        return True
    if "image dimensions exceed" in msg:
        return True
    if "pdf" in msg and ("password protected" in msg or "not valid" in msg):
        return True
    return False


_HTML_TITLE_RE = re.compile(r"<title[^>]*>\s*(.*?)\s*</title>", re.IGNORECASE | re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def sanitize_error_html(message: str) -> str:
    """Collapse HTML error payloads to a short human-readable message."""
    import html as _html

    raw = str(message)
    title_match = _HTML_TITLE_RE.search(raw)
    if title_match:
        title = _HTML_TAG_RE.sub("", title_match.group(1))
        return _html.unescape(re.sub(r"\s+", " ", title)).strip()
    if "<html" not in raw.lower() and "<!doctype" not in raw.lower():
        return raw
    text = _HTML_TAG_RE.sub(" ", raw)
    text = _html.unescape(re.sub(r"\s+", " ", text)).strip()
    return text or raw


def format_api_error(error: Exception) -> str:
    """Format an API error into a single-line human-readable description.

    Implementation: ``formatAPIError`` in ``services/api/errors.ts``.
    Used by the agent loop to build user-facing error messages.

    Extracts HTTP status, provider name, and the most informative part
    of the error message, removing JSON noise and internal stack info.
    """
    status = _get_error_status(error)
    provider = _get_error_provider(error)

    raw = str(error)
    # LiteLLM often wraps the real message in JSON — try to extract it
    inner = _extract_inner_error_message(raw) or sanitize_error_html(raw)

    parts: list[str] = []
    if status:
        parts.append(str(status))
    if provider:
        parts.append(provider)
    parts.append(inner or raw[:500])

    return " · ".join(parts)


def _extract_inner_error_message(raw: str) -> str | None:
    """Try to pull the most informative piece from a LiteLLM error string.

    LiteLLM wraps provider errors in verbose multi-line strings.
    We try several heuristics to extract the real message.
    """
    # Pattern: "litellm.XXXError: ... message" → take after last colon
    # but only if it's informative

    # Try JSON extraction (LiteLLM sometimes JSON-stringifies the body)
    try:
        # Look for {"message": "..."} or {"error": {"message": "..."}}
        for pattern in (
            re.compile(r'"message"\s*:\s*"([^"]{5,})"'),
            re.compile(r'"error"\s*:\s*\{[^}]*"message"\s*:\s*"([^"]{5,})"'),
        ):
            m = pattern.search(raw)
            if m:
                return m.group(1)
    except Exception:
        pass

    # Trim the verbose LiteLLM prefix
    if "litellm." in raw.lower():
        idx = raw.rfind(": ")
        if idx > 0 and len(raw) - idx > 10:
            candidate = raw[idx + 2:].strip()
            if len(candidate) > 10:
                return candidate[:500]

    return None


def get_error_message_for_user(
    error: Exception,
    model: str = "unknown",
) -> str:
    """Build a user-friendly error message from an API exception.

    Implementation: ``getAssistantMessageFromError`` (errors.ts L425-934).
    Maps 12 error categories to clear, actionable messages.

    The returned string is suitable for ``build_assistant_api_error_message``.

    Categories handled (mapped from OpenSpace, adapted for LiteLLM/OpenRouter):
      1. timeout
      2. prompt_too_long
      3. image/PDF size exceeded
      4. tool_use/tool_result pairing error
      5. duplicate_tool_use_id
      6. invalid_model (404 / bad model name)
      7. credit_balance_low
      8. auth_error (401/403)
      9. connection_error
     10. request_too_large (413)
     11. rate_limit (429)
     12. unknown / generic
    """
    import asyncio

    msg = str(error)
    msg_lower = msg.lower()
    status = _get_error_status(error)

    # 0. Abort — not really an error
    if is_abort_error(error):
        return "Request was cancelled."

    # 1. Timeout
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return API_TIMEOUT_ERROR_MESSAGE
    try:
        import litellm.exceptions as le
        if isinstance(error, le.Timeout):
            return API_TIMEOUT_ERROR_MESSAGE
    except ImportError:
        pass
    if isinstance(error, OSError) and "timeout" in msg_lower:
        return API_TIMEOUT_ERROR_MESSAGE

    # 2. Prompt too long (already handled by PromptTooLongError in call_model,
    #    but this catches edge cases)
    if isinstance(error, PromptTooLongError):
        return PROMPT_TOO_LONG_ERROR_MESSAGE

    # 3. Image / PDF size errors
    if is_media_size_error(error):
        if "pdf" in msg_lower:
            return (
                f"{API_ERROR_MESSAGE_PREFIX}: A PDF in the conversation exceeds "
                "the size limit. Try using a smaller file or /compact to free context."
            )
        return (
            f"{API_ERROR_MESSAGE_PREFIX}: An image in the conversation exceeds "
            "the size limit. Try /compact to remove old images from context."
        )

    # 4. tool_use / tool_result pairing error
    if (
        status == 400
        and "tool_use" in msg_lower
        and "tool_result" in msg_lower
        and ("without" in msg_lower or "immediately after" in msg_lower)
    ):
        return (
            f"{API_ERROR_MESSAGE_PREFIX}: 400 — tool_use/tool_result pairing "
            "error in conversation history. This is a known edge case. "
            "Try /compact to clean up the conversation."
        )

    # 5. Duplicate tool_use IDs
    if status == 400 and "tool_use" in msg_lower and "must be unique" in msg_lower:
        return (
            f"{API_ERROR_MESSAGE_PREFIX}: 400 — duplicate tool_use ID in "
            "conversation history. Try /compact to recover."
        )

    # 6. Invalid model name / model not found
    if status == 404 or (status == 400 and "invalid model" in msg_lower):
        return (
            f"{API_ERROR_MESSAGE_PREFIX}: The model '{model}' is not available. "
            "It may not exist or you may not have access to it. "
            "Check your API provider configuration."
        )
    if "does not exist" in msg_lower and "model" in msg_lower:
        return (
            f"{API_ERROR_MESSAGE_PREFIX}: The model '{model}' does not exist. "
            "Check your configuration."
        )

    # 7. Credit balance / billing
    if "credit balance" in msg_lower and "too low" in msg_lower:
        return (
            f"{API_ERROR_MESSAGE_PREFIX}: Your API credit balance is too low. "
            "Please add credits to your account."
        )
    if "billing" in msg_lower or "payment" in msg_lower:
        return (
            f"{API_ERROR_MESSAGE_PREFIX}: Billing issue — {format_api_error(error)}"
        )

    # 8. Auth errors (401 / 403)
    if status in (401, 403):
        return (
            f"{API_ERROR_MESSAGE_PREFIX}: Authentication failed (HTTP {status}). "
            "Check that your API key is valid and has the required permissions."
        )
    try:
        import litellm.exceptions as le
        if isinstance(error, le.AuthenticationError):
            return (
                f"{API_ERROR_MESSAGE_PREFIX}: Authentication failed. "
                "Check your API key configuration."
            )
    except ImportError:
        pass

    # 9. Connection errors
    try:
        import litellm.exceptions as le
        if isinstance(error, le.APIConnectionError):
            return (
                f"{API_ERROR_MESSAGE_PREFIX}: Connection error — "
                f"{format_api_error(error)}"
            )
    except ImportError:
        pass
    if any(kw in msg_lower for kw in (
        "connection refused", "connection reset", "name resolution",
        "network unreachable", "connectionerror",
    )):
        return (
            f"{API_ERROR_MESSAGE_PREFIX}: Network connection failed. "
            "Check your internet connection and API endpoint."
        )

    # 10. Request too large (413)
    if status == 413:
        return (
            f"{API_ERROR_MESSAGE_PREFIX}: Request too large (413). "
            "The conversation + attachments exceed the API size limit. "
            "Try /compact to reduce context size."
        )

    # 11. Rate limit (429)
    if status == 429:
        return (
            f"{API_ERROR_MESSAGE_PREFIX}: Rate limit reached (429). "
            "Please wait a moment before retrying."
        )
    try:
        import litellm.exceptions as le
        if isinstance(error, le.RateLimitError):
            return (
                f"{API_ERROR_MESSAGE_PREFIX}: Rate limit reached. "
                "Please wait a moment before retrying."
            )
    except ImportError:
        pass

    # 11b. Overloaded / 529
    if isinstance(error, OverloadedError) or is_overloaded_error(error):
        return (
            f"{API_ERROR_MESSAGE_PREFIX}: {REPEATED_OVERLOADED_ERROR_MESSAGE}. "
            "The API server is overloaded. Please try again later."
        )

    # 12. Generic / unknown
    formatted = format_api_error(error)
    return f"{API_ERROR_MESSAGE_PREFIX}: {formatted}"
