from typing import Optional


def truncate_log_tail(text: str, max_chars: Optional[int] = None) -> str:
    """Return only the last ``max_chars`` characters of ``text``.

    Tail-biased because the end of a captured log holds the final results /
    error context. ``max_chars`` of None or <= 0 returns ``text`` unchanged. On
    truncation, a one-line marker noting how many characters were dropped is
    prepended so consumers can tell the log is partial.
    """
    if not text or max_chars is None or max_chars <= 0:
        return text
    if len(text) <= max_chars:
        return text
    dropped = len(text) - max_chars
    marker = f"... [truncated {dropped} chars of stdout; full log on disk] ...\n"
    return marker + text[-max_chars:]


def parse_time_to_seconds(time_str: str) -> int:
    """Converts hh:mm:ss to seconds."""
    parts = time_str.split(":")
    if len(parts) != 3:
        raise ValueError("Time format must be hh:mm:ss")
    h, m, s = [int(p) for p in parts]
    return h * 3600 + m * 60 + s
