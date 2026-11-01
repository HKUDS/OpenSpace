"""Command-line entrypoint for the explicitly assembled runtime."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import sleep
from typing import Any

from agentpilot.app.assembly import (
    AssemblyError,
    AssemblyDependencies,
    RuntimeFactory,
)
from agentpilot.app.batch_loop import BatchExecutionInterrupted
from agentpilot.app.campaign_audit import audit_campaign
from agentpilot.app.campaign_driver import (
    CampaignStatus,
    CampaignSupervisor,
    SubprocessBatchInvoker,
)
from agentpilot.app.contracts import TerminalBatchError
from agentpilot.app.embedding_audit import audit_embedding_accounting
from agentpilot.app.model_harnesses import StructuredProposalParseError
from agentpilot.app.runtime_config import (
    RuntimeConfigError,
    AppConfig,
    load_app_config,
)
from agentpilot.domain import PlanningRoundFailed
from agentpilot.persistence import (
    PersistenceConflict,
    RunLeaseLost,
    RunLeaseUnavailable,
    SQLiteUnitOfWorkFactory,
)
from agentpilot.policy.memory import MemoryRankingError, MemorySummaryError
from agentpilot.providers import ProviderAccountingError, ProviderError

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


@dataclass(frozen=True)
class _ErrorContract:
    code: str
    message: str
    exit_code: int


_CONFIG_ERROR = _ErrorContract(
    "configuration_error", "configuration is invalid", 64
)
_ASSEMBLY_ERROR = _ErrorContract("assembly_error", "runtime assembly failed", 65)
_LEASE_UNAVAILABLE = _ErrorContract(
    "lease_unavailable", "another driver owns this run", 66
)
_LEASE_LOST = _ErrorContract(
    "lease_lost", "the run lease was lost during execution", 67
)
_BATCH_INTERRUPTED = _ErrorContract(
    "batch_execution_interrupted", "one or more actions were interrupted", 68
)
_PROVIDER_ACCOUNTING_ERROR = _ErrorContract(
    "provider_accounting_error",
    "provider accounting could not be attested",
    69,
)
_PROVIDER_ERROR = _ErrorContract(
    "provider_error", "an external provider call failed", 70
)
_MODEL_OUTPUT_ERROR = _ErrorContract(
    "model_output_error", "a model response violated its frozen contract", 71
)
_PLANNING_ERROR = _ErrorContract(
    "planning_failed", "the policy could not produce a valid action", 72
)
_PERSISTENCE_ERROR = _ErrorContract(
    "persistence_conflict", "durable state changed concurrently", 73
)
_VALIDATION_ERROR = _ErrorContract(
    "runtime_validation_error", "runtime data violated a frozen contract", 74
)
_TERMINAL_BATCH_ERROR = _ErrorContract(
    "terminal_batch_error", "the batch reached a non-resumable state", 75
)
_INTERNAL_ERROR = _ErrorContract("internal_error", "the runtime failed unexpectedly", 1)

# A supervised campaign stops for reasons that are not runtime errors, so each
# one gets its own code: an operator has to be able to tell "planning never
# produced an action" from "an unaccounted dispatch needs an audit".
_CAMPAIGN_EXIT_CODES = {
    CampaignStatus.COMPLETED: 0,
    CampaignStatus.PLANNING_BUDGET_EXHAUSTED: 76,
    CampaignStatus.ACCOUNTING_QUARANTINE: 77,
    CampaignStatus.TERMINAL_FAILURE: 78,
    CampaignStatus.STALLED: 79,
}


def _load_dependencies(config: AppConfig) -> AssemblyDependencies:
    spec = config.environment.factory
    module_name, separator, attribute_name = spec.partition(":")
    if not separator or not module_name.strip() or not attribute_name.strip():
        raise AssemblyError(
            "environment.factory must use the form 'package.module:callable'"
        )
    try:
        module = importlib.import_module(module_name)
        factory = getattr(module, attribute_name)
    except (ImportError, AttributeError) as exc:
        raise AssemblyError(f"cannot load environment factory {spec!r}") from exc
    if not isinstance(factory, Callable):
        raise AssemblyError(f"environment factory {spec!r} is not callable")
    dependencies = factory(config)
    if not isinstance(dependencies, AssemblyDependencies):
        raise AssemblyError(
            "environment dependency factory must return " "AssemblyDependencies"
        )
    return dependencies


def _load_env_file(path: str | Path) -> None:
    """Load an explicitly selected dotenv file without overriding host values."""

    source = Path(path).expanduser()
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise RuntimeConfigError("cannot read the selected env file") from exc
    pending: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, value = line.partition("=")
        name = name.strip()
        value = value.strip()
        if not separator or _ENV_NAME.fullmatch(name) is None:
            raise RuntimeConfigError("the selected env file has invalid syntax")
        if value[:1] in {"'", '"'}:
            if len(value) < 2 or value[-1] != value[0]:
                raise RuntimeConfigError("the selected env file has invalid syntax")
            value = value[1:-1]
        if "\x00" in value:
            raise RuntimeConfigError("the selected env file has invalid syntax")
        pending[name] = value
    for name, value in pending.items():
        os.environ.setdefault(name, value)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentpilot",
        description=(
            "Validate or run AgentPilot with an explicit external "
            "environment dependency factory."
        ),
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    # Their full argument parsers live in host.cli; main delegates before parsing.
    for name, help_text in (
        ("setup", "install skills for the current coding host"),
        ("doctor", "check the local installation without model calls"),
        ("project", "inspect existing project test commands without running them"),
    ):
        subcommands.add_parser(name, help=help_text)

    validate = subcommands.add_parser(
        "validate-config",
        help="validate the config TOML without importing the external environment",
    )
    validate.add_argument("--config", required=True)

    run = subcommands.add_parser("run", help="start one new durable batch")
    run.add_argument("--config", required=True)
    run.add_argument("--run-id", required=True)
    run.add_argument("--batch-id", required=True)
    run.add_argument("--rounds", type=int, required=True)
    run.add_argument(
        "--env-file",
        help="load missing environment variables from this explicit dotenv file",
    )

    campaign = subcommands.add_parser(
        "campaign",
        help="supervise a whole campaign from durable facts, not exit codes",
    )
    campaign.add_argument("--config", required=True)
    campaign.add_argument("--run-id", required=True)
    campaign.add_argument(
        "--env-file",
        help="load missing environment variables from this explicit dotenv file",
    )

    audit = subcommands.add_parser(
        "audit-campaign",
        help="check a finished run against its campaign acceptance shape",
    )
    audit.add_argument("--config", required=True)
    audit.add_argument("--run-id", required=True)

    embedding_audit = subcommands.add_parser(
        "verify-embedding-accounting",
        help="match settled embedding charges to their analytics rows",
    )
    embedding_audit.add_argument("--config", required=True)
    embedding_audit.add_argument("--run-id", required=True)
    embedding_audit.add_argument(
        "--env-file",
        help="load missing environment variables from this explicit dotenv file",
    )

    resume = subcommands.add_parser("resume", help="resume one durable batch")
    resume.add_argument("--config", required=True)
    resume.add_argument("--batch-id", required=True)
    resume.add_argument(
        "--env-file",
        help="load missing environment variables from this explicit dotenv file",
    )
    return parser


def _result_payload(result: Any) -> dict[str, Any]:
    return {
        "run_id": result.batch.run_id,
        "batch_id": result.batch.batch_id,
        "method": result.batch.method.value,
        "status": result.batch.status.value,
        "action_ids": [item.action_id for item in result.actions],
        "observation_ids": [item.observation_id for item in result.observations],
    }


def _config_validation_payload(config: AppConfig) -> dict[str, str]:
    return {
        "status": "valid",
        "method": config.policy.method.value,
    }


def _error_contract(exc: Exception) -> _ErrorContract:
    """Classify without serializing exception text or provider payloads."""

    if isinstance(exc, RuntimeConfigError):
        return _CONFIG_ERROR
    if isinstance(exc, AssemblyError):
        return _ASSEMBLY_ERROR
    if isinstance(exc, RunLeaseUnavailable):
        return _LEASE_UNAVAILABLE
    if isinstance(exc, RunLeaseLost):
        return _LEASE_LOST
    if isinstance(exc, BatchExecutionInterrupted):
        return _BATCH_INTERRUPTED
    if isinstance(exc, ProviderAccountingError):
        return _PROVIDER_ACCOUNTING_ERROR
    if isinstance(exc, ProviderError):
        return _PROVIDER_ERROR
    if isinstance(
        exc,
        (
            StructuredProposalParseError,
            MemoryRankingError,
            MemorySummaryError,
        ),
    ):
        return _MODEL_OUTPUT_ERROR
    if isinstance(exc, PlanningRoundFailed):
        return _PLANNING_ERROR
    if isinstance(exc, PersistenceConflict):
        return _PERSISTENCE_ERROR
    if isinstance(exc, TerminalBatchError):
        return _TERMINAL_BATCH_ERROR
    if isinstance(exc, ValueError):
        return _VALIDATION_ERROR
    return _INTERNAL_ERROR


def _exception_origin(exc: BaseException) -> str | None:
    """Name the innermost frame so a code can be located without its text."""

    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return None
    frame = frames[-1]
    return f"{Path(frame.filename).name}:{frame.lineno}"


def _exception_diagnosis(exc: BaseException) -> dict[str, Any]:
    """Describe the raise site by type and location, never by message.

    Classification alone leaves ``internal_error`` unactionable, and the
    exception text may carry a prompt or a provider payload.  A type name and a
    file:line are neither, so they are safe to publish and are exactly what an
    operator needs to find the raise site.
    """

    chain: list[dict[str, Any]] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(chain) < 5:
        seen.add(id(current))
        entry: dict[str, Any] = {"type": type(current).__qualname__}
        origin = _exception_origin(current)
        if origin is not None:
            entry["origin"] = origin
        chain.append(entry)
        if current.__cause__ is not None:
            current = current.__cause__
        elif current.__suppress_context__:
            current = None
        else:
            current = current.__context__
    return {"exception": chain[0]["type"], "chain": chain}


def _write_runtime_error(exc: Exception) -> int:
    contract = _error_contract(exc)
    payload: dict[str, Any] = {
        "error": {
            "code": contract.code,
            "message": contract.message,
            "diagnosis": _exception_diagnosis(exc),
        },
    }
    if isinstance(exc, BatchExecutionInterrupted):
        payload["error"]["action_ids"] = sorted(exc.errors)
        payload["error"]["action_errors"] = {
            action_id: type(error).__qualname__
            for action_id, error in sorted(exc.errors.items())
        }
    print(json.dumps(payload, sort_keys=True), file=sys.stderr)
    return contract.exit_code


def _campaign(arguments: argparse.Namespace, config: AppConfig) -> int:
    if config.campaign is None:
        raise RuntimeConfigError(
            "supervising a campaign requires a [campaign] table"
        )
    factory = SQLiteUnitOfWorkFactory(
        config.runtime.database_path,
        timeout=config.runtime.sqlite_timeout_seconds,
    )
    supervisor = CampaignSupervisor(
        unit_of_work=factory,
        lease_manager=factory.lease_manager(),
        invoker=SubprocessBatchInvoker(
            config_path=str(arguments.config),
            run_id=arguments.run_id,
            env_file=arguments.env_file,
        ),
        config=config.campaign,
        run_id=arguments.run_id,
        sleep=sleep,
        log=lambda message: print(message, flush=True),
    )
    outcome = supervisor.run()
    payload = {
        "run_id": arguments.run_id,
        "status": outcome.status.value,
        "reason": outcome.reason,
        "completed_processes": outcome.completed_processes,
    }
    if outcome.batch_id is not None:
        payload["batch_id"] = outcome.batch_id
    stream = sys.stdout if outcome.status is CampaignStatus.COMPLETED else sys.stderr
    print(json.dumps(payload, sort_keys=True), file=stream)
    return _CAMPAIGN_EXIT_CODES[outcome.status]


def _audit_campaign(arguments: argparse.Namespace, config: AppConfig) -> int:
    if config.campaign is None:
        raise RuntimeConfigError(
            "auditing a campaign requires a [campaign] table"
        )
    factory = SQLiteUnitOfWorkFactory(
        config.runtime.database_path,
        timeout=config.runtime.sqlite_timeout_seconds,
    )
    audit = audit_campaign(
        factory, run_id=arguments.run_id, config=config.campaign
    )
    payload = {
        "run_id": audit.run_id,
        "accepted": audit.accepted,
        "completed_processes": audit.completed_processes,
        "planning_exhausted_batches": audit.planning_exhausted_batches,
        "provider_calls": audit.provider_calls,
        "total_cost_usd": audit.total_cost_usd,
        "findings": list(audit.findings),
    }
    stream = sys.stdout if audit.accepted else sys.stderr
    print(json.dumps(payload, sort_keys=True), file=stream)
    return 0 if audit.accepted else 80


def _verify_embedding_accounting(
    arguments: argparse.Namespace, config: AppConfig
) -> int:
    ucb = config.policy.ucb
    if ucb is None:
        raise RuntimeConfigError(
            "embedding accounting belongs to a policy with a semantic encoder"
        )
    registry = _load_dependencies(config).providers
    reconciler = registry.embedding_accounting_reconciler(ucb.semantic.provider)
    if reconciler is None:
        raise RuntimeConfigError(
            "this configuration registers no embedding accounting lookup"
        )
    factory = SQLiteUnitOfWorkFactory(
        config.runtime.database_path,
        timeout=config.runtime.sqlite_timeout_seconds,
    )
    audit = audit_embedding_accounting(
        factory, run_id=arguments.run_id, lookup=reconciler.lookup
    )
    payload = {
        "run_id": audit.run_id,
        "accepted": audit.accepted,
        "embedding_calls": audit.embedding_calls,
        "confirmed_now": audit.confirmed_now,
        "confirmed_during_the_run": audit.confirmed_during_the_run,
        "awaiting_rows": list(audit.awaiting_rows),
        "findings": list(audit.findings),
    }
    stream = sys.stdout if audit.accepted else sys.stderr
    print(json.dumps(payload, sort_keys=True), file=stream)
    return 0 if audit.accepted else 80


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in {"setup", "doctor", "project"}:
        from agentpilot.host.cli import main as host_main

        return host_main(args)
    parser = _parser()
    arguments = parser.parse_args(args)
    try:
        config = load_app_config(arguments.config)
        if arguments.command == "validate-config":
            print(json.dumps(_config_validation_payload(config), sort_keys=True))
            return 0
        if arguments.command == "audit-campaign":
            return _audit_campaign(arguments, config)
        if arguments.env_file is not None:
            _load_env_file(arguments.env_file)
        if arguments.command == "campaign":
            return _campaign(arguments, config)
        if arguments.command == "verify-embedding-accounting":
            return _verify_embedding_accounting(arguments, config)
        factory = RuntimeFactory(config, _load_dependencies(config))
        if arguments.command == "run":
            runtime = factory.build(
                run_id=arguments.run_id, batch_id=arguments.batch_id
            )
            result = runtime.run(rounds=arguments.rounds)
        else:
            runtime = factory.build_for_resume(batch_id=arguments.batch_id)
            result = runtime.resume()
    except Exception as exc:
        return _write_runtime_error(exc)
    print(json.dumps(_result_payload(result), sort_keys=True))
    return 0


__all__ = ["main"]
