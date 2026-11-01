"""Local storage for OpenSpace cloud agent credentials."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from openspace.cloud.config import (
    DEFAULT_CLOUD_BASE_URL,
    OPENSPACE_CLOUD_API_KEY_ENV,
    OPENSPACE_CLOUD_BASE_URL_ENV,
    OPENSPACE_CLOUD_MODE_ENV,
    normalize_cloud_base_url,
)


def default_credentials_path() -> Path:
    """Return the package-local env file loaded by OpenSpace runtime."""

    return Path(__file__).resolve().parent.parent / ".env"


def read_cloud_credentials(path: str | Path | None = None) -> dict[str, str]:
    """Read cloud credential keys from a simple dotenv-style file."""

    target = Path(path).expanduser() if path else default_credentials_path()
    if not target.exists():
        return {}
    values: dict[str, str] = {}
    for line in target.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw_value = stripped.split("=", 1)
        key = key.strip()
        if key in {
            OPENSPACE_CLOUD_MODE_ENV,
            OPENSPACE_CLOUD_BASE_URL_ENV,
            OPENSPACE_CLOUD_API_KEY_ENV,
        }:
            values[key] = _unquote_env_value(raw_value.strip())
    return values


def save_cloud_agent_credentials(
    *,
    api_key: str,
    base_url: str = DEFAULT_CLOUD_BASE_URL,
    path: str | Path | None = None,
    update_process_env: bool = True,
) -> Path:
    """Persist the active cloud agent API key and update this process env."""

    if not api_key:
        raise ValueError("api_key is required")
    normalized_base_url = normalize_cloud_base_url(base_url)
    target = Path(path).expanduser() if path else default_credentials_path()
    updates = {
        OPENSPACE_CLOUD_MODE_ENV: "live",
        OPENSPACE_CLOUD_BASE_URL_ENV: normalized_base_url,
        OPENSPACE_CLOUD_API_KEY_ENV: api_key,
    }
    _write_env_updates(target, updates)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    if update_process_env:
        os.environ.update(updates)
    return target


def _write_env_updates(path: Path, updates: Mapping[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    output: list[str] = []

    for line in existing:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            output.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            output.append(f"{key}={_quote_env_value(updates[key])}")
            seen.add(key)
        else:
            output.append(line)

    if output and output[-1].strip():
        output.append("")
    for key, value in updates.items():
        if key not in seen:
            output.append(f"{key}={_quote_env_value(value)}")

    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def _quote_env_value(value: str) -> str:
    if not value:
        return ""
    if any(ch.isspace() for ch in value) or any(ch in value for ch in ['"', "'", "#"]):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _unquote_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return value.replace('\\"', '"').replace("\\\\", "\\")
