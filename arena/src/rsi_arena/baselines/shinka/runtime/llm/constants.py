import os



def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    parsed = int(value)
    if parsed < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {parsed}")
    return parsed


TIMEOUT = _env_int("SHINKA_LLM_TIMEOUT", 1200)
MAX_RETRIES = _env_int("SHINKA_LLM_MAX_RETRIES", 3)
OPENAI_MAX_RETRIES = _env_int("SHINKA_OPENAI_MAX_RETRIES", 0, minimum=0)
BACKOFF_MAX_TRIES = _env_int("SHINKA_LLM_BACKOFF_MAX_TRIES", 20)
BACKOFF_MAX_VALUE = _env_int("SHINKA_LLM_BACKOFF_MAX_VALUE", 20)
BACKOFF_MAX_TIME_MULTIPLIER = _env_int("SHINKA_LLM_BACKOFF_MAX_TIME_MULTIPLIER", 5)
BACKOFF_MAX_TIME = _env_int(
    "SHINKA_LLM_BACKOFF_MAX_TIME",
    TIMEOUT * BACKOFF_MAX_TIME_MULTIPLIER,
)
