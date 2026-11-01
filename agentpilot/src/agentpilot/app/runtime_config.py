"""Strict application configuration for the local runtime.

Policy parameters describe the selected learning carrier.  Storage and local
execution controls live in a separate ``runtime`` table so they cannot be
mistaken for policy state or silently inherited from the removed runtime.
"""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from agentpilot.app.config import (
    PolicyConfig,
    PolicyConfigError,
    parse_policy_config,
)
from agentpilot.domain import deep_freeze


class RuntimeConfigError(ValueError):
    """The application document is incomplete or ambiguous."""


@dataclass(frozen=True)
class RuntimeConfig:
    """Carrier-neutral controls for one local durable runtime."""

    database_path: Path
    runs_root: Path
    worker_slots: int
    sqlite_timeout_seconds: float
    lease_ttl_seconds: float
    lease_renew_interval_seconds: float
    require_provider_reported_cost: bool

    def __post_init__(self) -> None:
        for name in ("database_path", "runs_root"):
            value = getattr(self, name)
            if not isinstance(value, Path) or not value.is_absolute():
                raise RuntimeConfigError(
                    f"runtime.{name} must resolve absolutely"
                )
        if (
            isinstance(self.worker_slots, bool)
            or not isinstance(self.worker_slots, int)
            or self.worker_slots < 1
        ):
            raise RuntimeConfigError("runtime.worker_slots must be >= 1")
        if (
            isinstance(self.sqlite_timeout_seconds, bool)
            or not isinstance(self.sqlite_timeout_seconds, (int, float))
            or not math.isfinite(float(self.sqlite_timeout_seconds))
            or self.sqlite_timeout_seconds <= 0.0
        ):
            raise RuntimeConfigError(
                "runtime.sqlite_timeout_seconds must be finite and > 0"
            )
        for name in ("lease_ttl_seconds", "lease_renew_interval_seconds"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value <= 0.0
            ):
                raise RuntimeConfigError(
                    f"runtime.{name} must be finite and > 0"
                )
        if self.lease_renew_interval_seconds >= self.lease_ttl_seconds:
            raise RuntimeConfigError(
                "runtime.lease_renew_interval_seconds must be less than "
                "lease_ttl_seconds"
            )
        if not isinstance(self.require_provider_reported_cost, bool):
            raise RuntimeConfigError(
                "runtime.require_provider_reported_cost must be boolean"
            )
        if self.database_path == self.runs_root:
            raise RuntimeConfigError("database_path and runs_root must differ")


@dataclass(frozen=True)
class CampaignConfig:
    """The acceptance shape of one supervised multi-process campaign.

    Every number here is an experiment choice, so the supervisor reads them from
    the same document that freezes the policy rather than carrying constants.
    """

    total_processes: int
    actions_per_process: int
    max_planning_batches_per_process: int
    max_stalled_resumes: int
    poll_interval_seconds: float
    reconciliation_deadline_seconds: float

    def __post_init__(self) -> None:
        for name in (
            "total_processes",
            "actions_per_process",
            "max_planning_batches_per_process",
            "max_stalled_resumes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise RuntimeConfigError(f"campaign.{name} must be >= 1")
        for name in ("poll_interval_seconds", "reconciliation_deadline_seconds"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value <= 0.0
            ):
                raise RuntimeConfigError(f"campaign.{name} must be finite and > 0")


@dataclass(frozen=True)
class EnvironmentConfig:
    """Identity and opaque, explicit settings for the external adapter."""

    factory: str
    identity: str
    settings: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.factory, str) or not self.factory.strip():
            raise RuntimeConfigError("environment.factory must be non-empty")
        if not isinstance(self.identity, str) or not self.identity.strip():
            raise RuntimeConfigError("environment.identity must be non-empty")
        if not isinstance(self.settings, Mapping):
            raise RuntimeConfigError("environment.settings must be a table")
        object.__setattr__(self, "settings", deep_freeze(self.settings))


@dataclass(frozen=True)
class AppConfig:
    policy: PolicyConfig
    runtime: RuntimeConfig
    environment: EnvironmentConfig
    source_directory: Path
    # Only a supervised campaign needs this table, and a document without it is
    # simply not usable for one.
    campaign: CampaignConfig | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.source_directory, Path)
            or not self.source_directory.is_absolute()
        ):
            raise RuntimeConfigError(
                "source_directory must resolve absolutely"
            )


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeConfigError(f"{field_name} must be a table")
    return value


def _only(
    mapping: Mapping[str, Any], allowed: set[str], field_name: str
) -> None:
    unknown = set(mapping) - allowed
    if unknown:
        raise RuntimeConfigError(
            f"unknown {field_name} keys: {sorted(unknown)}"
        )


def _path(table: Mapping[str, Any], name: str, *, base: Path) -> Path:
    value = table.get(name)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeConfigError(f"runtime.{name} must be a non-empty path")
    path = Path(value.strip()).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def parse_runtime_config(
    value: Mapping[str, Any], *, source_directory: str | Path
) -> RuntimeConfig:
    table = _mapping(value, "runtime")
    names = {
        "database_path",
        "runs_root",
        "worker_slots",
        "sqlite_timeout_seconds",
        "lease_ttl_seconds",
        "lease_renew_interval_seconds",
        "require_provider_reported_cost",
    }
    _only(table, names, "runtime")
    missing = names - set(table)
    if missing:
        raise RuntimeConfigError(f"missing runtime keys: {sorted(missing)}")
    worker_slots = table["worker_slots"]
    timeout = table["sqlite_timeout_seconds"]
    cost = table["require_provider_reported_cost"]
    return RuntimeConfig(
        database_path=_path(
            table, "database_path", base=Path(source_directory)
        ),
        runs_root=_path(table, "runs_root", base=Path(source_directory)),
        worker_slots=worker_slots,
        sqlite_timeout_seconds=timeout,
        lease_ttl_seconds=table["lease_ttl_seconds"],
        lease_renew_interval_seconds=table["lease_renew_interval_seconds"],
        require_provider_reported_cost=cost,
    )


def parse_environment_config(value: Mapping[str, Any]) -> EnvironmentConfig:
    table = _mapping(value, "environment")
    _only(table, {"factory", "identity", "settings"}, "environment")
    missing = {"factory", "identity", "settings"} - set(table)
    if missing:
        raise RuntimeConfigError(
            f"missing environment keys: {sorted(missing)}"
        )
    factory = table["factory"]
    identity = table["identity"]
    if not isinstance(factory, str):
        raise RuntimeConfigError("environment.factory must be non-empty")
    if not isinstance(identity, str):
        raise RuntimeConfigError("environment.identity must be non-empty")
    return EnvironmentConfig(
        factory=factory.strip(),
        identity=identity.strip(),
        settings=_mapping(table["settings"], "environment.settings"),
    )


def parse_campaign_config(value: Mapping[str, Any]) -> CampaignConfig:
    table = _mapping(value, "campaign")
    names = {
        "total_processes",
        "actions_per_process",
        "max_planning_batches_per_process",
        "max_stalled_resumes",
        "poll_interval_seconds",
        "reconciliation_deadline_seconds",
    }
    _only(table, names, "campaign")
    missing = names - set(table)
    if missing:
        raise RuntimeConfigError(f"missing campaign keys: {sorted(missing)}")
    return CampaignConfig(
        total_processes=table["total_processes"],
        actions_per_process=table["actions_per_process"],
        max_planning_batches_per_process=table["max_planning_batches_per_process"],
        max_stalled_resumes=table["max_stalled_resumes"],
        poll_interval_seconds=table["poll_interval_seconds"],
        reconciliation_deadline_seconds=table["reconciliation_deadline_seconds"],
    )


def parse_app_config(
    value: Mapping[str, Any], *, source_directory: str | Path
) -> AppConfig:
    document = _mapping(value, "document")
    _only(document, {"policy", "runtime", "environment", "campaign"}, "top-level")
    missing = {"policy", "runtime", "environment"} - set(document)
    if missing:
        raise RuntimeConfigError(
            f"missing top-level tables: {sorted(missing)}"
        )
    try:
        policy = parse_policy_config(_mapping(document["policy"], "policy"))
    except PolicyConfigError as exc:
        raise RuntimeConfigError(str(exc)) from exc
    source = Path(source_directory).expanduser().resolve()
    campaign_table = document.get("campaign")
    return AppConfig(
        policy=policy,
        runtime=parse_runtime_config(
            _mapping(document["runtime"], "runtime"),
            source_directory=source,
        ),
        environment=parse_environment_config(
            _mapping(document["environment"], "environment")
        ),
        source_directory=source,
        campaign=(
            None
            if campaign_table is None
            else parse_campaign_config(_mapping(campaign_table, "campaign"))
        ),
    )


def load_app_config(path: str | Path) -> AppConfig:
    source = Path(path).expanduser().resolve()
    try:
        document = tomllib.loads(source.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeConfigError(
            f"cannot load config from {source}"
        ) from exc
    return parse_app_config(document, source_directory=source.parent)


__all__ = [
    "CampaignConfig",
    "EnvironmentConfig",
    "RuntimeConfig",
    "RuntimeConfigError",
    "AppConfig",
    "load_app_config",
    "parse_campaign_config",
    "parse_runtime_config",
    "parse_environment_config",
    "parse_app_config",
]
