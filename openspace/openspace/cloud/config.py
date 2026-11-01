"""Strict OpenSpace cloud runtime configuration."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Literal
from urllib.parse import urlparse

from openspace.host_detection import load_runtime_env, read_host_mcp_env

CloudMode = Literal["off", "live"]
TelemetryMode = Literal["off", "outbox"]

OPENSPACE_CLOUD_MODE_ENV = "OPENSPACE_CLOUD_MODE"
OPENSPACE_CLOUD_BASE_URL_ENV = "OPENSPACE_CLOUD_BASE_URL"
OPENSPACE_CLOUD_API_KEY_ENV = "OPENSPACE_CLOUD_API_KEY"
OPENSPACE_CLOUD_TELEMETRY_MODE_ENV = "OPENSPACE_CLOUD_TELEMETRY_MODE"
OPENSPACE_CLOUD_SKILL_QUALITY_REPORTING_ENV = "OPENSPACE_CLOUD_SKILL_QUALITY_REPORTING"

DEFAULT_CLOUD_BASE_URL = "https://open-space.cloud"

_ALLOWED_CLOUD_MODES = {"off", "live"}
_ALLOWED_TELEMETRY_MODES = {"off", "outbox"}
_TRUE_CONFIG_VALUES = {"1", "true", "yes", "on", "enabled"}


class CloudConfigError(RuntimeError):
    """Raised when OpenSpace cloud configuration is invalid."""


@dataclass(frozen=True)
class CloudConfig:
    mode: CloudMode
    base_url: str
    api_key: str
    telemetry_mode: TelemetryMode

    @property
    def enabled(self) -> bool:
        return self.mode == "live"


def _get_cloud_env(name: str, host_env: dict[str, str]) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    return str(host_env.get(name, "")).strip()


def _normalize_mode(value: str) -> CloudMode:
    mode = (value or "off").strip().lower()
    if mode not in _ALLOWED_CLOUD_MODES:
        raise CloudConfigError(
            f"{OPENSPACE_CLOUD_MODE_ENV} must be one of off or live, got {value!r}"
        )
    return mode  # type: ignore[return-value]


def _normalize_telemetry_mode(value: str) -> TelemetryMode:
    mode = (value or "off").strip().lower()
    if mode not in _ALLOWED_TELEMETRY_MODES:
        raise CloudConfigError(
            f"{OPENSPACE_CLOUD_TELEMETRY_MODE_ENV} must be one of off or outbox, got {value!r}"
        )
    return mode  # type: ignore[return-value]


def _normalize_enabled_flag(value: str) -> bool:
    return (value or "").strip().lower() in _TRUE_CONFIG_VALUES


def normalize_cloud_base_url(value: str) -> str:
    base_url = (value or DEFAULT_CLOUD_BASE_URL).strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CloudConfigError(
            f"{OPENSPACE_CLOUD_BASE_URL_ENV} must be an absolute http(s) service root URL"
        )
    if parsed.query or parsed.fragment:
        raise CloudConfigError(
            f"{OPENSPACE_CLOUD_BASE_URL_ENV} must not include query strings or fragments"
        )
    normalized_path = parsed.path.rstrip("/")
    if normalized_path:
        raise CloudConfigError(
            f"{OPENSPACE_CLOUD_BASE_URL_ENV} must be a service root URL without a path"
        )
    return base_url


def load_cloud_config() -> CloudConfig:
    """Load strict cloud configuration from OPENSPACE_CLOUD_* only."""

    load_runtime_env()
    host_env = read_host_mcp_env()
    mode = _normalize_mode(_get_cloud_env(OPENSPACE_CLOUD_MODE_ENV, host_env))
    base_url = normalize_cloud_base_url(
        _get_cloud_env(OPENSPACE_CLOUD_BASE_URL_ENV, host_env) or DEFAULT_CLOUD_BASE_URL
    )
    api_key = _get_cloud_env(OPENSPACE_CLOUD_API_KEY_ENV, host_env)
    telemetry_mode = _normalize_telemetry_mode(
        _get_cloud_env(OPENSPACE_CLOUD_TELEMETRY_MODE_ENV, host_env)
    )
    return CloudConfig(
        mode=mode,
        base_url=base_url,
        api_key=api_key,
        telemetry_mode=telemetry_mode,
    )


def load_cloud_skill_quality_reporting_enabled() -> bool:
    """Return whether analyzer skill-quality telemetry may be emitted."""

    load_runtime_env()
    host_env = read_host_mcp_env()
    return _normalize_enabled_flag(
        _get_cloud_env(OPENSPACE_CLOUD_SKILL_QUALITY_REPORTING_ENV, host_env)
    )


def require_cloud_enabled(config: CloudConfig | None = None) -> CloudConfig:
    cfg = config or load_cloud_config()
    if not cfg.enabled:
        raise CloudConfigError(
            f"OpenSpace cloud is disabled. Set {OPENSPACE_CLOUD_MODE_ENV}=live to use cloud features."
        )
    return cfg


def require_cloud_agent_key(config: CloudConfig | None = None) -> CloudConfig:
    cfg = require_cloud_enabled(config)
    if not cfg.api_key:
        raise CloudConfigError(
            f"OpenSpace cloud agent key is required. Set {OPENSPACE_CLOUD_API_KEY_ENV}."
        )
    return cfg
